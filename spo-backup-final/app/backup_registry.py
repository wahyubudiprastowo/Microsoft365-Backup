"""Unified backup registry for legacy flat and tenant-aware backup layouts."""
import json
import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

from app.config_manager import load_config
from app.tenant_manager import TenantManager

log = logging.getLogger("spo_backup")


def slugify_tenant(value: str) -> str:
    value = (value or "").strip().lower()
    chars = []
    last_dash = False
    for ch in value:
        if ch.isalnum():
            chars.append(ch)
            last_dash = False
            continue
        if not last_dash:
            chars.append("-")
            last_dash = True
    return "".join(chars).strip("-") or "default-tenant"


class BackupRegistry:
    """Scan and manage backups across both supported disk layouts."""

    def __init__(self, config=None):
        self.config = config or load_config()
        self.legacy_root = Path(self.config["backup"]["root_dir"])
        self.tenant_root = self.legacy_root / "m365"
        self._cache = {}
        self._tenant_manager = TenantManager()

    def list_all(
        self,
        use_cache: bool = True,
        collapse_projects: bool = True,
        prefer_backup_path: str | None = None,
        allow_size_scan: bool = True,
    ) -> list:
        cache_key = f"{'all_collapsed' if collapse_projects else 'all_raw'}:{'scan' if allow_size_scan else 'fast'}"
        if use_cache and not prefer_backup_path and cache_key in self._cache:
            cached, ts = self._cache[cache_key]
            if time.time() - ts < 15 and self._cache_entries_exist(cached):
                return cached

        results = []
        results.extend(self._list_legacy(allow_size_scan=allow_size_scan))
        results.extend(self._list_tenant_aware(allow_size_scan=allow_size_scan))
        results.sort(key=lambda item: item["date"], reverse=True)
        if collapse_projects:
            results = self._collapse_projects(results, prefer_backup_path=prefer_backup_path)
        if not prefer_backup_path:
            self._cache[cache_key] = (results, time.time())
        return results

    def _collapse_projects(self, backups: list, prefer_backup_path: str | None = None) -> list:
        grouped = {}
        order = []
        for item in backups:
            key = self._project_group_key(item)
            if key not in grouped:
                grouped[key] = []
                order.append(key)
            grouped[key].append(item)

        collapsed = []
        for key in order:
            entries = grouped[key]
            primary = None
            if prefer_backup_path:
                for candidate in entries:
                    if candidate.get("backup_path") == prefer_backup_path:
                        primary = dict(candidate)
                        break
            if not primary:
                primary = dict(sorted(entries, key=self._project_display_sort_key, reverse=True)[0])
            latest_entry = sorted(entries, key=self._project_latest_sort_key, reverse=True)[0]
            primary["project_versions_count"] = len(entries)
            primary["project_hidden_versions_count"] = max(0, len(entries) - 1)
            primary["project_latest_backup_name"] = latest_entry.get("backup_name")
            primary["project_latest_date"] = latest_entry.get("date")
            primary["project_latest_status"] = latest_entry.get("status")
            if len(entries) > 1:
                primary["summary"] = f"{primary.get('summary') or 'Backup project'} · merged view of {len(entries)} runs"
            collapsed.append(primary)

        collapsed.sort(key=lambda item: item.get("project_latest_date") or item.get("date") or "", reverse=True)
        return collapsed

    def _project_group_key(self, item: dict) -> tuple:
        tenant_slug = str(item.get("tenant_slug") or "").strip()
        workload = str(item.get("workload") or "").strip().lower()
        targets = [str(value).strip().lower() for value in (item.get("target_names_preview") or []) if str(value).strip()]
        if targets:
            target_key = "|".join(sorted(targets))
        else:
            target_key = str(item.get("summary") or "").strip().lower()
        if not target_key:
            target_key = str(item.get("backup_name") or "").strip().lower()
        return (tenant_slug, workload, target_key)

    def _project_display_sort_key(self, item: dict) -> tuple:
        status_priority = {
            "running": 9,
            "paused": 8,
            "success": 7,
            "completed": 7,
            "partial": 6,
            "interrupted": 5,
            "failed": 4,
            "cancelled": 3,
            "unknown": 0,
        }
        status = str(item.get("status") or "unknown").lower()
        return (
            status_priority.get(status, 0),
            int(item.get("size_bytes") or 0),
            int(item.get("files_count") or 0),
            item.get("date") or "",
        )

    def _project_latest_sort_key(self, item: dict) -> tuple:
        return (
            item.get("date") or "",
            int(item.get("size_bytes") or 0),
            int(item.get("files_count") or 0),
        )

    def _list_legacy(self, allow_size_scan: bool = True) -> list:
        backups = []
        if not self.legacy_root.exists():
            return backups

        for entry in self.legacy_root.iterdir():
            if not entry.is_dir():
                continue
            if entry.name == "m365" or entry.name.startswith("."):
                continue
            if not (entry.name.startswith("backup_") or entry.name.startswith("custom_")):
                continue
            owner = self._resolve_legacy_owner(entry)
            backups.append(
                self._describe_backup(
                    entry,
                    tenant_slug=owner["tenant_slug"],
                    tenant_name=owner["tenant_name"],
                    tenant_id=owner.get("tenant_id"),
                    tenant_attribution=owner.get("tenant_attribution", "unknown"),
                    workload="sharepoint",
                    layout="legacy",
                    allow_size_scan=allow_size_scan,
                )
            )
        return backups

    def _resolve_legacy_owner(self, backup_dir: Path) -> dict:
        manifest_candidates = [
            backup_dir / "_workload_manifest.json",
            backup_dir / "_backup_metadata.json",
        ]
        for manifest in manifest_candidates:
            if not manifest.exists():
                continue
            try:
                data = json.load(open(manifest))
            except Exception:
                continue
            tenant_slug = str(data.get("tenant_slug") or "").strip()
            tenant_name = str(data.get("tenant_name") or "").strip()
            tenant_id = str(data.get("tenant_id") or "").strip() or None
            if tenant_slug:
                return {
                    "tenant_slug": tenant_slug,
                    "tenant_name": tenant_name or tenant_slug.replace("-", " ").title(),
                    "tenant_id": tenant_id,
                    "tenant_attribution": "manifest",
                }

        legacy_tenant = self._tenant_manager.get_tenant(TenantManager.LEGACY_TENANT_ID, include_secret=False)
        if not legacy_tenant:
            legacy_tenant = self._tenant_manager.get_active_tenant(include_secret=False)
        if legacy_tenant:
            tenant_slug = slugify_tenant(
                legacy_tenant.get("primary_domain")
                or legacy_tenant.get("sharepoint_host")
                or legacy_tenant.get("name")
            )
            if tenant_slug:
                return {
                    "tenant_slug": tenant_slug,
                    "tenant_name": legacy_tenant.get("name") or tenant_slug.replace("-", " ").title(),
                    "tenant_id": legacy_tenant.get("id"),
                    "tenant_attribution": "config-legacy",
                }

        return {
            "tenant_slug": "legacy-default",
            "tenant_name": "Legacy SharePoint",
            "tenant_id": TenantManager.LEGACY_TENANT_ID,
            "tenant_attribution": "unknown",
        }

    def _list_tenant_aware(self, allow_size_scan: bool = True) -> list:
        results = []
        if not self.tenant_root.exists():
            return results

        for tenant_dir in self.tenant_root.iterdir():
            if not tenant_dir.is_dir() or tenant_dir.name.startswith("."):
                continue
            for workload_dir in tenant_dir.iterdir():
                if not workload_dir.is_dir() or workload_dir.name.startswith("."):
                    continue
                for backup_dir in workload_dir.iterdir():
                    if not backup_dir.is_dir() or not backup_dir.name.startswith("backup_"):
                        continue
                    results.append(
                        self._describe_backup(
                            backup_dir,
                            tenant_slug=tenant_dir.name,
                            tenant_name=self._guess_tenant_name(tenant_dir.name),
                            workload=workload_dir.name,
                            layout="tenant-aware",
                            allow_size_scan=allow_size_scan,
                        )
                    )
        return results

    def _describe_backup(
        self,
        backup_dir: Path,
        tenant_slug: str,
        tenant_name: str,
        workload: str,
        layout: str,
        tenant_id: str | None = None,
        tenant_attribution: str = "manifest",
        allow_size_scan: bool = True,
    ) -> dict:
        size_bytes = self._read_or_compute_size(backup_dir, allow_scan=allow_size_scan)
        files_count = 0
        targets_count = 0
        status = None
        manifest = self._load_backup_manifest(backup_dir)
        if manifest:
            tenant_slug = manifest.get("tenant_slug", tenant_slug) or tenant_slug
            tenant_name = manifest.get("tenant_name", tenant_name)
            tenant_id = manifest.get("tenant_id", tenant_id) or tenant_id
            status = manifest.get("status") or status
            files_count = (
                manifest.get("files_downloaded")
                or manifest.get("total_files")
                or files_count
            )
            targets_count = (
                manifest.get("sites_count")
                or manifest.get("users_count")
                or manifest.get("mailbox_count")
                or manifest.get("targets_processed")
                or targets_count
            )

        if not status:
            if size_bytes > 0:
                status = "interrupted"
            else:
                status = "unknown"

        if not tenant_id:
            guessed = self._guess_tenant_record(tenant_slug)
            if guessed:
                tenant_id = guessed.get("id") or tenant_id
                tenant_name = guessed.get("name") or tenant_name

        target_names = self._list_root_target_names(backup_dir)
        summary = self._build_backup_summary(workload, manifest, target_names, targets_count)
        recovery = self._classify_recovery(
            backup_dir=backup_dir,
            layout=layout,
            workload=workload,
            status=status,
            size_bytes=size_bytes,
            files_count=files_count,
            targets_count=targets_count,
            target_names=target_names,
            manifest=manifest,
            tenant_id=tenant_id,
        )

        return {
            "tenant_id": tenant_id,
            "tenant_slug": tenant_slug,
            "tenant_name": tenant_name,
            "tenant_attribution": tenant_attribution,
            "workload": workload,
            "backup_name": backup_dir.name,
            "backup_path": str(backup_dir),
            "layout": layout,
            "date": datetime.fromtimestamp(backup_dir.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "size_bytes": size_bytes,
            "size_human": self._format_size(size_bytes),
            "files_count": files_count or 0,
            "targets_count": targets_count or 0,
            "status": status,
            "target_names_preview": target_names[:3],
            "target_names_more": max(0, len(target_names) - 3),
            "summary": summary,
            "resume_supported": recovery["resume_supported"],
            "recovery_state": recovery["state"],
            "recovery_label": recovery["label"],
            "recovery_hint": recovery["hint"],
            "resume_action_kind": recovery["action_kind"],
            "resume_action_label": recovery["action_label"],
        }

    def _load_backup_manifest(self, backup_dir: Path) -> dict | None:
        for manifest_file in (
            backup_dir / "_workload_manifest.json",
            backup_dir / "_backup_metadata.json",
        ):
            if not manifest_file.exists():
                continue
            try:
                return json.load(open(manifest_file))
            except Exception:
                continue
        return None

    def _has_backup_payload(self, backup_dir: Path) -> bool:
        try:
            for entry in backup_dir.iterdir():
                if entry.name.startswith("."):
                    continue
                if entry.name.startswith("_"):
                    if entry.name.endswith(".tmp"):
                        return True
                    continue
                return True
        except Exception:
            return False
        return False

    def _classify_recovery(
        self,
        backup_dir: Path,
        layout: str,
        workload: str,
        status: str,
        size_bytes: int,
        files_count: int,
        targets_count: int,
        target_names: list[str],
        manifest: dict | None,
        tenant_id: str | None,
    ) -> dict:
        manifest = manifest or {}
        state = str(status or "unknown").strip().lower()
        has_checkpoint = any([
            size_bytes > 0,
            int(files_count or 0) > 0,
            int(targets_count or 0) > 0,
            bool(target_names),
            bool(manifest.get("resumed_existing_backup")),
            self._has_backup_payload(backup_dir),
        ])

        action_kind = None
        action_label = None
        if layout == "legacy" and workload == "sharepoint":
            action_kind = "legacy"
            action_label = "Resume Legacy Backup"
        elif tenant_id:
            action_kind = "tenant-workload"
            action_label = "Resume Workload"

        if state in {"success", "completed"}:
            return {
                "state": "completed",
                "label": "completed",
                "hint": "Completed successfully. No resume is needed.",
                "resume_supported": False,
                "action_kind": None,
                "action_label": None,
            }

        if state == "running":
            return {
                "state": "active",
                "label": "active task",
                "hint": "This backup project is currently active. Use the live progress panel to pause, resume, or cancel it.",
                "resume_supported": False,
                "action_kind": None,
                "action_label": None,
            }

        if state == "paused":
            return {
                "state": "paused",
                "label": "paused",
                "hint": "This backup is paused. Use the live progress panel Resume button to continue the current task.",
                "resume_supported": True,
                "action_kind": None,
                "action_label": None,
            }

        if state == "partial":
            return {
                "state": "rerun_recommended",
                "label": "partial - retry recommended",
                "hint": "Some targets finished and some failed. Re-run the same workload to retry missing targets; existing files will be reused when possible.",
                "resume_supported": bool(action_kind),
                "action_kind": action_kind,
                "action_label": "Retry Workload" if action_kind else None,
            }

        if state in {"interrupted", "cancelled"}:
            return {
                "state": "resume_available" if has_checkpoint else "rerun_recommended",
                "label": "resume available" if has_checkpoint else "restart recommended",
                "hint": (
                    "A checkpoint already exists in this backup folder. Re-running the same flow will reuse the existing project directory and continue from saved files or .tmp chunks when supported."
                    if has_checkpoint else
                    "The run stopped before a usable checkpoint was detected. Start the same flow again to rebuild the project cleanly."
                ),
                "resume_supported": bool(action_kind),
                "action_kind": action_kind,
                "action_label": "Resume Project" if has_checkpoint and action_kind else ("Re-run Workload" if action_kind else None),
            }

        if state == "failed":
            return {
                "state": "resume_available" if has_checkpoint else "failed_hard",
                "label": "resume after fix" if has_checkpoint else "hard failed",
                "hint": (
                    "This run failed, but checkpoint data already exists. Fix the underlying permission, credential, or connectivity issue, then run the same flow again to continue from the saved project folder."
                    if has_checkpoint else
                    "This run failed before a reusable checkpoint was created. Fix the root cause first, then re-run the backup from the beginning."
                ),
                "resume_supported": bool(action_kind),
                "action_kind": action_kind,
                "action_label": "Resume After Fix" if has_checkpoint and action_kind else ("Re-run After Fix" if action_kind else None),
            }

        if has_checkpoint:
            return {
                "state": "resume_available",
                "label": "resume possible",
                "hint": "Checkpoint data already exists in this folder. Re-running the same flow should reuse the saved project content.",
                "resume_supported": bool(action_kind),
                "action_kind": action_kind,
                "action_label": "Resume Project" if action_kind else None,
            }

        return {
            "state": "unknown",
            "label": "unknown state",
            "hint": "This backup does not yet have enough metadata to determine whether it is resumable.",
            "resume_supported": False,
            "action_kind": None,
            "action_label": None,
        }

    def _list_root_target_names(self, backup_dir: Path) -> list[str]:
        names = []
        try:
            for entry in sorted(backup_dir.iterdir(), key=lambda item: item.name.lower()):
                if entry.name.startswith(".") or entry.name.startswith("_"):
                    continue
                if entry.is_dir():
                    names.append(entry.name)
        except Exception:
            pass
        return names

    def _build_backup_summary(self, workload: str, manifest: dict | None, target_names: list[str], targets_count: int) -> str:
        if workload == "sharepoint":
            if target_names:
                if len(target_names) == 1:
                    return f"Site: {target_names[0]}"
                return f"Sites: {', '.join(target_names[:3])}" + (f" +{len(target_names) - 3} more" if len(target_names) > 3 else "")
            if targets_count:
                return f"{targets_count} site target(s)"
            return "SharePoint backup folder"

        if workload == "onedrive":
            if targets_count:
                return f"Users in scope: {targets_count}"
            if target_names:
                return f"Users: {', '.join(target_names[:3])}" + (f" +{len(target_names) - 3} more" if len(target_names) > 3 else "")
            return "OneDrive target backup"

        if workload == "outlook":
            if targets_count:
                return f"Mailboxes in scope: {targets_count}"
            if target_names:
                return f"Mailboxes: {', '.join(target_names[:3])}" + (f" +{len(target_names) - 3} more" if len(target_names) > 3 else "")
            return "Outlook mailbox backup"

        if workload == "teams":
            if targets_count:
                return f"Teams in scope: {targets_count}"
            if target_names:
                return f"Teams: {', '.join(target_names[:3])}" + (f" +{len(target_names) - 3} more" if len(target_names) > 3 else "")
            return "Teams export backup"

        if target_names:
            return ", ".join(target_names[:3]) + (f" +{len(target_names) - 3} more" if len(target_names) > 3 else "")
        if targets_count:
            return f"Targets: {targets_count}"
        return "Backup folder"

    def _guess_tenant_name(self, tenant_slug: str) -> str:
        tenant = self._guess_tenant_record(tenant_slug)
        if tenant:
            return tenant.get("name", tenant_slug)
        return tenant_slug.replace("-", " ").title()

    def _guess_tenant_record(self, tenant_slug: str) -> dict | None:
        for tenant in self._tenant_manager.list_tenants(include_secrets=False):
            slug = slugify_tenant(
                tenant.get("primary_domain")
                or tenant.get("sharepoint_host")
                or tenant.get("name")
            )
            if slug == tenant_slug:
                return tenant
        return None

    def _manifest_size_hint(self, backup_dir: Path) -> int:
        for manifest_name in ("_workload_manifest.json", "_backup_metadata.json"):
            manifest_path = backup_dir / manifest_name
            if not manifest_path.exists():
                continue
            try:
                payload = json.load(open(manifest_path))
            except Exception:
                continue
            for key in ("bytes_downloaded", "size_bytes", "total_size_bytes"):
                try:
                    value = int(payload.get(key) or 0)
                except (TypeError, ValueError):
                    value = 0
                if value > 0:
                    return value
        return 0

    def _read_or_compute_size(self, backup_dir: Path, allow_scan: bool = True) -> int:
        size_cache = backup_dir / "_size_cache.json"
        if size_cache.exists():
            try:
                cached = json.load(open(size_cache))
                return int(cached.get("size_bytes", 0))
            except Exception:
                pass

        hinted_size = self._manifest_size_hint(backup_dir)
        if hinted_size > 0:
            try:
                with open(size_cache, "w") as handle:
                    json.dump({"size_bytes": hinted_size, "computed_at": datetime.now().isoformat(), "source": "manifest"}, handle)
            except Exception:
                pass
            return hinted_size

        if not allow_scan:
            return 0

        size_bytes = self._calc_size(backup_dir)
        try:
            with open(size_cache, "w") as handle:
                json.dump({"size_bytes": size_bytes, "computed_at": datetime.now().isoformat(), "source": "scan"}, handle)
        except Exception:
            pass
        return size_bytes

    def _calc_size(self, path: Path) -> int:
        total = 0
        try:
            for entry in os.scandir(path):
                try:
                    if entry.is_file():
                        total += entry.stat().st_size
                    elif entry.is_dir() and not entry.name.startswith("."):
                        total += self._calc_size(Path(entry.path))
                except (OSError, PermissionError):
                    pass
        except (OSError, PermissionError):
            pass
        return total

    def _format_size(self, size_bytes: int) -> str:
        value = float(size_bytes)
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if value < 1024:
                return f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} PB"

    def get_tenant_backup_root(self, tenant: dict, workload: str = "sharepoint") -> Path:
        tenant_slug = slugify_tenant(
            tenant.get("primary_domain")
            or tenant.get("sharepoint_host")
            or tenant.get("name")
        )
        return self.tenant_root / tenant_slug / workload

    def resolve_backup_path(self, tenant_slug: str, workload: str, backup_name: str) -> Path | None:
        if not (backup_name.startswith("backup_") or backup_name.startswith("custom_")):
            return None
        candidates = [
            self.tenant_root / tenant_slug / workload / backup_name,
            self.legacy_root / backup_name,
        ]
        for path in candidates:
            if path.exists() and path.is_dir():
                return path.resolve()
        return None

    def get_backup(self, tenant_slug: str, workload: str, backup_name: str) -> dict | None:
        for item in self.list_all(use_cache=False, collapse_projects=False):
            if (
                item.get("tenant_slug") == tenant_slug
                and item.get("workload") == workload
                and item.get("backup_name") == backup_name
            ):
                return item
        return None

    def browse_backup(self, tenant_slug: str, workload: str, backup_name: str, relative_path: str = "") -> dict:
        backup_path = self.resolve_backup_path(tenant_slug, workload, backup_name)
        if not backup_path:
            raise FileNotFoundError("Backup not found")

        clean_relative = str(relative_path or "").strip().strip("/")
        current_path = (backup_path / clean_relative).resolve() if clean_relative else backup_path
        if current_path != backup_path and backup_path not in current_path.parents:
            raise ValueError("Invalid backup path")
        if not current_path.exists():
            raise FileNotFoundError("Path not found inside backup")
        if not current_path.is_dir():
            raise ValueError("Requested path is not a directory")

        entries = []
        for entry in sorted(current_path.iterdir(), key=lambda item: (item.is_file(), item.name.lower())):
            if entry.name.startswith("."):
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            child_relative = str(entry.relative_to(backup_path)).replace("\\", "/")
            item = {
                "name": entry.name,
                "relative_path": child_relative,
                "type": "directory" if entry.is_dir() else "file",
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            }
            if entry.is_dir():
                try:
                    item["children_count"] = sum(1 for child in entry.iterdir() if not child.name.startswith("."))
                except OSError:
                    item["children_count"] = None
            else:
                item["size_bytes"] = stat.st_size
                item["size_human"] = self._format_size(stat.st_size)
            entries.append(item)

        backup = self.get_backup(tenant_slug, workload, backup_name) or {
            "tenant_slug": tenant_slug,
            "workload": workload,
            "backup_name": backup_name,
            "backup_path": str(backup_path),
        }
        parent_path = None
        if clean_relative:
            parent_bits = clean_relative.split("/")[:-1]
            parent_path = "/".join(parent_bits)

        return {
            "backup": backup,
            "backup_root": str(backup_path),
            "current_path": clean_relative,
            "parent_path": parent_path,
            "entries": entries,
        }

    def delete(self, tenant_slug: str, workload: str, backup_name: str) -> dict:
        path = self.resolve_backup_path(tenant_slug, workload, backup_name)
        if path:
            shutil.rmtree(path)
            self.invalidate_cache()
            log.info(f"Deleted backup: {path}")
            return {"status": "deleted", "path": str(path), "layout": "legacy" if path.parent == self.legacy_root else "tenant-aware"}
        return {"error": "Not found"}

    def get_history(self, tenant_slug: str, limit: int = 20) -> list:
        history_dir = self.tenant_root / tenant_slug / ".history"
        if history_dir.exists():
            files = sorted(history_dir.glob("backup_*.json"), reverse=True)[:limit]
            results = []
            for item in files:
                try:
                    results.append(json.load(open(item)))
                except Exception:
                    pass
            if results:
                return results

        backups = [b for b in self.list_all(use_cache=False, collapse_projects=True) if b["tenant_slug"] == tenant_slug][:limit]
        return [
            {
                "tenant_slug": backup["tenant_slug"],
                "tenant_name": backup["tenant_name"],
                "workload": backup["workload"],
                "backup_name": backup["backup_name"],
                "date": backup["date"],
                "size_bytes": backup["size_bytes"],
                "size_human": backup["size_human"],
                "layout": backup["layout"],
            }
            for backup in backups
        ]

    def get_stats(self) -> dict:
        backups = self.list_all(collapse_projects=True)
        total_size = sum(item["size_bytes"] for item in backups)
        by_tenant = {}
        by_workload = {}
        for backup in backups:
            by_tenant[backup["tenant_slug"]] = by_tenant.get(backup["tenant_slug"], 0) + 1
            by_workload[backup["workload"]] = by_workload.get(backup["workload"], 0) + 1
        return {
            "total_backups": len(backups),
            "total_size_bytes": total_size,
            "total_size_human": self._format_size(total_size),
            "by_tenant": by_tenant,
            "by_workload": by_workload,
            "tenant_root": str(self.tenant_root),
            "legacy_root": str(self.legacy_root),
        }

    def invalidate_cache(self):
        self._cache.clear()

    def _cache_entries_exist(self, entries: list) -> bool:
        for item in entries:
            backup_path = item.get("backup_path")
            if not backup_path or not Path(backup_path).exists():
                return False
        return True
