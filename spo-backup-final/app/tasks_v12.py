"""v12 additive helpers for per-tenant schedules and notifications."""
import copy
import logging

from celery.schedules import crontab

log = logging.getLogger("spo_backup")


def register_tenant_schedules(celery_app):
    """Register enabled tenant schedules into Celery beat."""
    from app.schedule_manager import ScheduleManager

    sm = ScheduleManager()
    try:
        sm.migrate_global_schedule_to_tenants()
    except Exception as e:
        log.warning(f"Tenant schedule migration skipped: {e}")

    schedules = sm.list_enabled_schedules()
    if not schedules:
        return []

    existing = dict(getattr(celery_app.conf, "beat_schedule", {}) or {})
    existing = {
        key: value
        for key, value in existing.items()
        if not key.startswith("scheduled-backup-")
    }

    registered = []
    for sched in schedules:
        parts = str(sched.get("cron_expression", "")).split()
        if len(parts) != 5:
            log.error(f"Invalid cron for {sched.get('tenant_name')}: {sched.get('cron_expression')}")
            continue
        task_name = f"scheduled-backup-{sched['tenant_slug']}"
        existing[task_name] = {
            "task": "app.tasks.run_backup_task",
            "schedule": crontab(
                minute=parts[0],
                hour=parts[1],
                day_of_month=parts[2],
                month_of_year=parts[3],
                day_of_week=parts[4],
            ),
            "kwargs": {
                "tenant_id": sched["tenant_id"],
                "workloads": sched.get("workloads", ["sharepoint"]),
            },
        }
        registered.append(task_name)

    if registered and "scheduled-backup" in existing:
        del existing["scheduled-backup"]

    celery_app.conf.beat_schedule = existing
    if registered:
        marker = "/tmp/.spo_tenant_schedules_logged"
        try:
            import os
            if not os.path.exists(marker):
                with open(marker, "w") as handle:
                    handle.write("\n".join(registered))
                log.info(f"Registered {len(registered)} tenant schedule(s)")
        except Exception:
            pass
    return registered


def send_tenant_notification(tenant_id: str, stats: dict):
    """Send backup notifications with tenant-specific recipient overrides."""
    from app.config_manager import load_config
    from app.notifier import NotificationDispatcher
    from app.schedule_manager import ScheduleManager

    cfg = copy.deepcopy(load_config())
    tenant_notif = ScheduleManager().get_notifications(tenant_id) if tenant_id else {}
    merged_cfg = _merge_notification_config(cfg, tenant_notif)
    return NotificationDispatcher(merged_cfg).send_all(stats)


def _merge_notification_config(global_cfg: dict, tenant_notif: dict) -> dict:
    merged = copy.deepcopy(global_cfg)
    notif = copy.deepcopy(merged.get("notification", {}))

    email_cfg = tenant_notif.get("email", {})
    if email_cfg.get("enabled") and email_cfg.get("recipients"):
        notif["enabled"] = True
        notif["email_to"] = email_cfg["recipients"]

    tg_cfg = tenant_notif.get("telegram", {})
    if tg_cfg.get("enabled") and tg_cfg.get("chat_ids"):
        target = copy.deepcopy(notif.get("telegram", {}))
        target["enabled"] = True
        target["chat_ids"] = tg_cfg["chat_ids"]
        notif["telegram"] = target

    teams_cfg = tenant_notif.get("teams", {})
    if teams_cfg.get("enabled") and teams_cfg.get("webhook_urls"):
        target = copy.deepcopy(notif.get("teams", {}))
        target["enabled"] = True
        target["webhook_urls"] = teams_cfg["webhook_urls"]
        notif["teams"] = target

    merged["notification"] = notif
    return merged
