"""Celery tasks v4.0 — pause/resume + custom destination."""
import logging
from datetime import datetime, timezone
from celery import Celery
from celery.schedules import crontab

from app.config_manager import load_config
from app.backup_engine import BackupEngine, RestoreEngine
from app.notifier import NotificationDispatcher

log = logging.getLogger("spo_backup")

celery_app = Celery("spo_backup",
    broker="redis://redis:6379/0",
    backend="redis://redis:6379/1")

celery_app.conf.update(
    task_serializer="json", result_serializer="json", accept_content=["json"],
    timezone="Asia/Jakarta", enable_utc=True, task_track_started=True,
    result_expires=86400,
)

try:
    cfg = load_config()
    if cfg.get("schedule", {}).get("enabled"):
        parts = cfg["schedule"].get("cron_expression", "0 2 * * *").split()
        celery_app.conf.beat_schedule = {
            "scheduled-backup": {
                "task": "app.tasks.run_backup_task",
                "schedule": crontab(
                    minute=parts[0], hour=parts[1],
                    day_of_week=parts[4] if len(parts) > 4 else "*",
                    day_of_month=parts[2] if len(parts) > 2 else "*",
                    month_of_year=parts[3] if len(parts) > 3 else "*",
                ),
            },
        }
except Exception:
    pass


@celery_app.task(bind=True, name="app.tasks.run_backup_task")
def run_backup_task(self, custom_root: str = None):
    """Run full backup. ★ Accepts custom_root parameter."""
    config = load_config()
    engine = BackupEngine(
        config,
        progress_callback=lambda evt, data: self.update_state(
            state="PROGRESS", meta={"event": evt, **data}
        ),
        task_id=self.request.id,  # ★ Pass task ID for control
    )
    stats = engine.run_backup(custom_root=custom_root)
    for k in ("start_time", "end_time"):
        if stats.get(k):
            stats[k] = stats[k].isoformat()
    try:
        NotificationDispatcher(config).send_all(stats)
    except Exception as e:
        stats["notification_error"] = str(e)
    return stats


@celery_app.task(bind=True, name="app.tasks.download_custom_url_task")
def download_custom_url_task(self, url: str, dest_dir: str = None):
    """★ Custom URL download with optional dest_dir."""
    config = load_config()
    engine = BackupEngine(
        config,
        progress_callback=lambda evt, data: self.update_state(
            state="PROGRESS", meta={"event": evt, **data}
        ),
        task_id=self.request.id,  # ★ Enable pause/resume
    )
    return engine.download_custom_url(url, dest_dir=dest_dir)


@celery_app.task(bind=True, name="app.tasks.run_restore_task")
def run_restore_task(self, backup_name, site_name, target_site_path=None, dry_run=False):
    config = load_config()
    return RestoreEngine(config).restore_site(backup_name, site_name, target_site_path, dry_run)


@celery_app.task(name="app.tasks.send_test_notification")
def send_test_notification(channel_name=None):
    config = load_config()
    return NotificationDispatcher(config).send_test(channel_name)
