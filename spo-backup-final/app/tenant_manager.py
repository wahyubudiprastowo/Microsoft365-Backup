"""Compatibility tenant manager for legacy single-tenant and v10 multi-tenant configs."""
import json
import logging
import uuid
import re
import base64
import binascii
from datetime import datetime, timezone

import msal
import requests

from app.config_manager import load_config, save_config
from app.http_utils import build_retry_session

log = logging.getLogger("spo_backup")

DEFAULT_WORKLOADS_ENABLED = ["sharepoint"]

REQUIRED_SCOPES = [
    "Sites.Read.All",
    "Sites.FullControl.All",
    "Mail.Read",
    "Calendars.Read",
    "Contacts.Read",
    "Files.Read.All",
    "Team.ReadBasic.All",
    "Channel.ReadBasic.All",
    "ChannelMessage.Read.All",
    "ChannelSettings.Read.All",
    "TeamMember.Read.All",
    "User.Read.All",
    "Group.Read.All",
]


class TenantManager:
    LEGACY_TENANT_ID = "legacy-default"

    @staticmethod
    def _validate_fields(data, existing=None):
        normalized = {}
        for field in ("name", "primary_domain", "sharepoint_host", "tenant_id", "client_id", "object_id"):
            if field in data:
                value = data[field]
                if not isinstance(value, str):
                    raise ValueError(f"{field} must be text")
                normalized[field] = value.strip()
        for field in ("name", "primary_domain", "sharepoint_host", "tenant_id", "client_id"):
            if field in normalized and not normalized[field]:
                raise ValueError(f"{field} is required")
        for field in ("tenant_id", "client_id", "object_id"):
            if normalized.get(field):
                try:
                    uuid.UUID(normalized[field])
                except ValueError as exc:
                    raise ValueError(f"{field} must be a UUID") from exc
        host_pattern = r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?"
        for field in ("primary_domain", "sharepoint_host"):
            if normalized.get(field) and (not re.fullmatch(host_pattern, normalized[field]) or "." not in normalized[field]):
                raise ValueError(f"{field} must be a DNS hostname")
        if "workloads_enabled" in data:
            workloads = data["workloads_enabled"]
            if not isinstance(workloads, list) or any(value not in {"sharepoint", "onedrive", "outlook", "teams"} for value in workloads):
                raise ValueError("workloads_enabled contains an unsupported workload")
        if "workload_target_selection" in data and not isinstance(data["workload_target_selection"], dict):
            raise ValueError("workload_target_selection must be an object")
        if "client_secret" in data and not isinstance(data["client_secret"], str):
            raise ValueError("client_secret must be text")
        return normalized

    def _load(self):
        return self._normalize_config(load_config())

    def _save(self, cfg):
        save_config(self._sync_active_legacy_fields(cfg))

    def _build_legacy_tenant(self, cfg):
        azure = cfg.get("azure_ad", {})
        sharepoint = cfg.get("sharepoint", {})
        if not azure.get("tenant_id") or not azure.get("client_id"):
            return None
        return {
            "id": self.LEGACY_TENANT_ID,
            "name": azure.get("app_display_name") or sharepoint.get("host") or "Default Tenant",
            "primary_domain": sharepoint.get("host", ""),
            "sharepoint_host": sharepoint.get("host", ""),
            "tenant_id": azure.get("tenant_id", ""),
            "client_id": azure.get("client_id", ""),
            "client_secret": azure.get("client_secret", ""),
            "object_id": azure.get("object_id", ""),
            "workloads_enabled": list(DEFAULT_WORKLOADS_ENABLED),
            "workload_target_selection": {},
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_tested": None,
            "last_status": "untested",
        }

    def _normalize_config(self, cfg):
        cfg.setdefault("tenants", [])
        if not cfg["tenants"]:
            legacy = self._build_legacy_tenant(cfg)
            if legacy:
                cfg["tenants"] = [legacy]
                cfg["active_tenant_id"] = legacy["id"]
        for tenant in cfg.get("tenants", []):
            tenant.setdefault("id", str(uuid.uuid4()))
            tenant.setdefault("name", "Unnamed Tenant")
            tenant.setdefault("primary_domain", tenant.get("sharepoint_host", ""))
            tenant.setdefault("sharepoint_host", tenant.get("primary_domain", ""))
            tenant.setdefault("client_secret", "")
            tenant.setdefault("object_id", "")
            tenant.setdefault("workloads_enabled", list(DEFAULT_WORKLOADS_ENABLED))
            tenant.setdefault("workload_target_selection", {})
            tenant.setdefault("created_at", datetime.now(timezone.utc).isoformat())
            tenant.setdefault("last_tested", None)
            tenant.setdefault("last_status", "untested")
        if cfg.get("tenants") and not cfg.get("active_tenant_id"):
            cfg["active_tenant_id"] = cfg["tenants"][0]["id"]
        return cfg

    def _sync_active_legacy_fields(self, cfg):
        cfg = self._normalize_config(cfg)
        active = self.get_active_tenant(cfg=cfg, include_secret=True)
        if not active:
            return cfg
        cfg.setdefault("azure_ad", {})
        cfg.setdefault("sharepoint", {})
        cfg["azure_ad"]["tenant_id"] = active.get("tenant_id", "")
        cfg["azure_ad"]["client_id"] = active.get("client_id", "")
        cfg["azure_ad"]["client_secret"] = active.get("client_secret", "")
        cfg["azure_ad"]["object_id"] = active.get("object_id", "")
        cfg["azure_ad"]["app_display_name"] = active.get("name", "")
        cfg["sharepoint"]["host"] = active.get("sharepoint_host", "")
        return cfg

    def _mask(self, tenant):
        safe = json.loads(json.dumps(tenant))
        if safe.get("client_secret"):
            safe["client_secret"] = "***MASKED***"
        return safe

    def list_tenants(self, include_secrets=False):
        tenants = self._load().get("tenants", [])
        return tenants if include_secrets else [self._mask(t) for t in tenants]

    def get_tenant(self, tenant_id, include_secret=True):
        for tenant in self._load().get("tenants", []):
            if tenant.get("id") == tenant_id:
                return tenant if include_secret else self._mask(tenant)
        return None

    def get_active_tenant(self, cfg=None, include_secret=True):
        cfg = self._normalize_config(cfg or self._load())
        active_id = cfg.get("active_tenant_id")
        for tenant in cfg.get("tenants", []):
            if tenant.get("id") == active_id:
                return tenant if include_secret else self._mask(tenant)
        if cfg.get("tenants"):
            tenant = cfg["tenants"][0]
            return tenant if include_secret else self._mask(tenant)
        return None

    def set_active_tenant(self, tenant_id):
        cfg = self._load()
        if not any(t.get("id") == tenant_id for t in cfg.get("tenants", [])):
            return False
        cfg["active_tenant_id"] = tenant_id
        self._save(cfg)
        log.info(f"Active tenant set to: {tenant_id}")
        return True

    def add_tenant(self, data):
        cfg = self._load()
        normalized = self._validate_fields(data)
        required = ["name", "primary_domain", "sharepoint_host", "tenant_id", "client_id", "client_secret"]
        for field in required:
            if not data.get(field):
                raise ValueError(f"Missing required field: {field}")
        for tenant in cfg.get("tenants", []):
            if str(tenant.get("tenant_id") or "").lower() == normalized["tenant_id"].lower():
                raise ValueError(f"Tenant already exists: {data['tenant_id']}")
        new_tenant = {
            "id": str(uuid.uuid4()),
            "name": data["name"].strip(),
            "primary_domain": data["primary_domain"].strip(),
            "sharepoint_host": data["sharepoint_host"].strip(),
            "tenant_id": data["tenant_id"].strip(),
            "client_id": data["client_id"].strip(),
            "client_secret": data["client_secret"].strip(),
            "object_id": data.get("object_id", "").strip(),
            "workloads_enabled": data.get("workloads_enabled", list(DEFAULT_WORKLOADS_ENABLED)),
            "workload_target_selection": data.get("workload_target_selection", {}),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_tested": None,
            "last_status": "untested",
        }
        cfg["tenants"].append(new_tenant)
        cfg["active_tenant_id"] = new_tenant["id"]
        self._save(cfg)
        return self._mask(new_tenant)

    def update_tenant(self, tenant_id, data):
        cfg = self._load()
        normalized = self._validate_fields(data)
        proposed_tenant_id = normalized.get("tenant_id")
        if proposed_tenant_id and any(
            tenant.get("id") != tenant_id and str(tenant.get("tenant_id") or "").lower() == proposed_tenant_id.lower()
            for tenant in cfg.get("tenants", [])
        ):
            raise ValueError("Tenant ID is already configured")
        for idx, tenant in enumerate(cfg.get("tenants", [])):
            if tenant.get("id") != tenant_id:
                continue
            for field in [
                "name", "primary_domain", "sharepoint_host", "tenant_id",
                "client_id", "object_id", "workloads_enabled", "workload_target_selection",
                "sharepoint_target_size_cache",
            ]:
                if field in data:
                    cfg["tenants"][idx][field] = normalized.get(field, data[field])
            if data.get("client_secret") and data["client_secret"] != "***MASKED***":
                cfg["tenants"][idx]["client_secret"] = data["client_secret"]
            self._save(cfg)
            return self._mask(cfg["tenants"][idx])
        return None

    def delete_tenant(self, tenant_id):
        cfg = self._load()
        before = len(cfg.get("tenants", []))
        cfg["tenants"] = [t for t in cfg.get("tenants", []) if t.get("id") != tenant_id]
        if len(cfg["tenants"]) == before:
            return False
        if cfg.get("active_tenant_id") == tenant_id:
            cfg["active_tenant_id"] = cfg["tenants"][0]["id"] if cfg["tenants"] else None
        self._save(cfg)
        return True

    def test_tenant(self, tenant_id=None, tenant_data=None):
        tenant_data = tenant_data or self.get_tenant(tenant_id)
        if not tenant_data:
            return {"status": "error", "message": "Tenant not found"}
        try:
            app = msal.ConfidentialClientApplication(
                tenant_data["client_id"],
                authority=f"https://login.microsoftonline.com/{tenant_data['tenant_id']}",
                client_credential=tenant_data["client_secret"],
            )
            result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
            if "access_token" not in result:
                return {"status": "error", "message": result.get("error_description", "Auth failed"), "details": result}
            token = result["access_token"]
            try:
                segment = token.split(".")[1]
                claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
                granted_roles = set(claims.get("roles") or [])
            except (IndexError, ValueError, binascii.Error, AttributeError):
                granted_roles = set()
            headers = {"Authorization": f"Bearer {token}"}
            session = build_retry_session()
            tests = {}
            org = session.get("https://graph.microsoft.com/v1.0/organization", headers=headers, timeout=(10, 20))
            tests["organization"] = {"status": org.status_code, "ok": org.status_code == 200}
            sp_host = tenant_data.get("sharepoint_host", "")
            if sp_host:
                sp = session.get(f"https://graph.microsoft.com/v1.0/sites/{sp_host}", headers=headers, timeout=(10, 20))
                tests["sharepoint"] = {"status": sp.status_code, "ok": sp.status_code == 200}
            users = session.get("https://graph.microsoft.com/v1.0/users?$top=1", headers=headers, timeout=(10, 20))
            tests["users"] = {"status": users.status_code, "ok": users.status_code == 200}
            from app.workloads import WORKLOAD_META

            required = set()
            for workload in tenant_data.get("workloads_enabled", DEFAULT_WORKLOADS_ENABLED):
                required.update(WORKLOAD_META.get(workload, {}).get("required_scopes", []))
            missing_roles = sorted(required - granted_roles)
            tests["workload_permissions"] = {
                "ok": not missing_roles,
                "granted_roles": sorted(granted_roles),
                "missing_roles": missing_roles,
                "note": "Token roles are a preflight check; workload API access still requires a real operation test.",
            }
            all_ok = all(t.get("ok", False) for t in tests.values())
            if tenant_id:
                cfg = self._load()
                for tenant in cfg.get("tenants", []):
                    if tenant.get("id") == tenant_id:
                        tenant["last_tested"] = datetime.now(timezone.utc).isoformat()
                        tenant["last_status"] = "connected" if all_ok else "disconnected"
                        self._save(cfg)
                        break
            warning_bits = []
            if tests.get("users", {}).get("status") == 403:
                warning_bits.append("Graph user discovery is blocked. Verify admin consent for User.Read.All / Files.Read.All / Mail.Read scopes.")
            if tests.get("sharepoint", {}).get("status") == 403:
                warning_bits.append("SharePoint site discovery is blocked. Verify Sites.Read.All / Sites.FullControl.All consent.")
            if missing_roles:
                warning_bits.append("Missing Graph application roles for enabled workloads: " + ", ".join(missing_roles))
            return {
                "status": "ok" if all_ok else "warning",
                "message": "All tests passed" if all_ok else "Some tests failed",
                "details": tests,
                "warnings": warning_bits,
            }
        except Exception as e:
            log.error(f"Tenant test failed: {e}")
            return {"status": "error", "message": str(e)}
