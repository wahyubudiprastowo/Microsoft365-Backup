"""Per-tenant schedule and notification configuration manager."""
import copy
import logging

from app.backup_registry import slugify_tenant
from app.config_manager import load_config, save_config
from app.workloads import filter_backup_workloads

log = logging.getLogger("spo_backup")

DEFAULT_SCHEDULE = {
    "enabled": False,
    "cron_expression": "0 2 * * *",
    "timezone": "Asia/Jakarta",
}

DEFAULT_NOTIFICATIONS = {
    "email": {"enabled": False, "recipients": []},
    "telegram": {"enabled": False, "chat_ids": []},
    "teams": {"enabled": False, "webhook_urls": []},
}


class ScheduleManager:
    """Manage per-tenant schedules and notification recipients."""

    def _load(self):
        return load_config()

    def _save(self, cfg):
        save_config(cfg)

    def get_schedule(self, tenant_id: str) -> dict:
        cfg = self._load()
        for tenant in cfg.get("tenants", []):
            if tenant.get("id") == tenant_id:
                current = copy.deepcopy(DEFAULT_SCHEDULE)
                current.update(tenant.get("schedule", {}))
                return current
        return copy.deepcopy(DEFAULT_SCHEDULE)

    def set_schedule(self, tenant_id: str, schedule: dict) -> bool:
        cfg = self._load()
        for tenant in cfg.get("tenants", []):
            if tenant.get("id") != tenant_id:
                continue
            current = self.get_schedule(tenant_id)
            current.update(schedule or {})
            self._validate_cron(current.get("cron_expression", ""))
            tenant["schedule"] = current
            self._save(cfg)
            log.info(
                f"Schedule updated for tenant {tenant.get('name', tenant_id)}: "
                f"{current.get('cron_expression')} (enabled={current.get('enabled')})"
            )
            return True
        return False

    def get_notifications(self, tenant_id: str) -> dict:
        cfg = self._load()
        for tenant in cfg.get("tenants", []):
            if tenant.get("id") == tenant_id:
                current = copy.deepcopy(DEFAULT_NOTIFICATIONS)
                stored = tenant.get("notifications", {})
                for channel, values in stored.items():
                    if channel not in current:
                        current[channel] = {}
                    current[channel].update(values or {})
                return current
        return copy.deepcopy(DEFAULT_NOTIFICATIONS)

    def set_notifications(self, tenant_id: str, notifications: dict) -> bool:
        cfg = self._load()
        for tenant in cfg.get("tenants", []):
            if tenant.get("id") != tenant_id:
                continue
            current = self.get_notifications(tenant_id)
            for channel, values in (notifications or {}).items():
                if channel not in current:
                    current[channel] = {}
                current[channel].update(values or {})
            tenant["notifications"] = current
            self._save(cfg)
            log.info(f"Notifications updated for tenant {tenant.get('name', tenant_id)}")
            return True
        return False

    def list_enabled_schedules(self):
        cfg = self._load()
        result = []
        for tenant in cfg.get("tenants", []):
            sched = self.get_schedule(tenant.get("id"))
            if not sched.get("enabled"):
                continue
            workloads = filter_backup_workloads(tenant.get("workloads_enabled", ["sharepoint"]))
            if not workloads:
                workloads = ["sharepoint"]
            result.append({
                "tenant_id": tenant.get("id"),
                "tenant_name": tenant.get("name"),
                "tenant_slug": slugify_tenant(
                    tenant.get("primary_domain")
                    or tenant.get("sharepoint_host")
                    or tenant.get("name", "")
                ),
                "cron_expression": sched.get("cron_expression"),
                "timezone": sched.get("timezone", "Asia/Jakarta"),
                "workloads": workloads,
            })
        return result

    def migrate_global_schedule_to_tenants(self):
        cfg = self._load()
        global_sched = cfg.get("schedule", {})
        if not global_sched.get("enabled"):
            return False

        migrated = False
        for tenant in cfg.get("tenants", []):
            if "schedule" in tenant:
                continue
            tenant["schedule"] = {
                "enabled": global_sched.get("enabled", False),
                "cron_expression": global_sched.get("cron_expression", DEFAULT_SCHEDULE["cron_expression"]),
                "timezone": global_sched.get("timezone", DEFAULT_SCHEDULE["timezone"]),
            }
            migrated = True
            log.info(f"Migrated global schedule to tenant: {tenant.get('name')}")

        if migrated:
            self._save(cfg)
        return migrated

    def _validate_cron(self, expr: str):
        if not expr or len(str(expr).split()) != 5:
            raise ValueError(f"Invalid cron expression: '{expr}'. Must have 5 fields.")
