"""Celery tasks v9.0 — Proper cancel + no duplicate cron log."""
import logging
import os
import json
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from celery import Celery
from celery.exceptions import Ignore
from celery.schedules import crontab
from celery.signals import worker_process_init

from app.config_manager import load_config
from app.backup_engine import BackupEngine, RestoreEngine
from app.notifier import NotificationDispatcher
from app.workloads import BACKUP_ENABLED_WORKLOADS, filter_backup_workloads

try:
    from app.uploader import upload_to_remote, test_remote_destination
    HAS_UPLOADER = True
except ImportError:
    HAS_UPLOADER = False

LOG_DIR = "/app/logs"
LOG_FILE = os.path.join(LOG_DIR, "spo_backup.log")
os.makedirs(LOG_DIR, exist_ok=True)


def setup_file_logger():
    logger = logging.getLogger("spo_backup")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=5)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    logger.propagate = False
    return logger


log = setup_file_logger()


def _write_workload_manifest(backup_path: str, workload_name: str, stats: dict, tenant: dict | None, layout: str):
    if not backup_path or not os.path.isdir(backup_path):
        return
    tenant_slug = "legacy-default"
    tenant_name = "Legacy SharePoint"
    tenant_id = None
    if tenant:
        from app.backup_registry import slugify_tenant

        tenant_slug = slugify_tenant(
            tenant.get("primary_domain")
            or tenant.get("sharepoint_host")
            or tenant.get("name")
        )
        tenant_name = tenant.get("name") or tenant_name
        tenant_id = tenant.get("id")

    payload = {
        "tenant_id": tenant_id,
        "tenant_name": tenant_name,
        "tenant_slug": tenant_slug,
        "workload": workload_name,
        "layout": layout,
        "backup_name": os.path.basename(backup_path.rstrip("/")),
        "backup_path": backup_path,
        "files_downloaded": stats.get("files_downloaded", 0),
        "files_skipped": stats.get("files_skipped", 0),
        "files_resumed": stats.get("files_resumed", 0),
        "bytes_downloaded": stats.get("bytes_downloaded", 0),
        "successful_sites": stats.get("successful_sites", 0),
        "total_sites": stats.get("total_sites", 0),
        "failed_sites": stats.get("failed_sites", []),
        "status": stats.get("status"),
        "start_time": stats.get("start_time"),
        "end_time": stats.get("end_time"),
        "written_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with open(os.path.join(backup_path, "_workload_manifest.json"), "w") as handle:
            json.dump(payload, handle, indent=2, default=str)
    except Exception as e:
        log.warning(f"Failed to write workload manifest for {backup_path}: {e}")


@worker_process_init.connect
def on_worker_start(**kwargs):
    setup_file_logger()


celery_app = Celery("spo_backup",
    broker="redis://redis:6379/0",
    backend="redis://redis:6379/1")

celery_app.conf.update(
    task_serializer="json", result_serializer="json", accept_content=["json"],
    timezone="Asia/Jakarta", enable_utc=True, task_track_started=True,
    result_expires=86400,
    broker_connection_retry_on_startup=True,
    # Long-running backup/download tasks should be redelivered after worker/broker loss.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_cancel_long_running_tasks_on_connection_loss=True,
    worker_prefetch_multiplier=1,
)

# ═══════════════════════════════════════════════════════════════
# ★ FIX #3: Setup schedule WITHOUT logging (was spamming log) ★
# ═══════════════════════════════════════════════════════════════
try:
    cfg = load_config()
    if cfg.get("schedule", {}).get("enabled"):
        parts = cfg["schedule"].get("cron_expression", "0 2 * * *").split()
        celery_app.conf.beat_schedule = {
            "scheduled-backup": {
                "task": "app.tasks.run_backup_task",
                "schedule": crontab(
                    minute=parts[0], hour=parts[1],
                    day_of_month=parts[2] if len(parts) > 2 else "*",
                    month_of_year=parts[3] if len(parts) > 3 else "*",
                    day_of_week=parts[4] if len(parts) > 4 else "*",
                ),
            },
        }
        # ★ Only log ONCE per worker startup, with marker file ★
        marker = "/tmp/.spo_schedule_logged"
        if not os.path.exists(marker):
            log.info(f"Backup schedule registered: {cfg['schedule']['cron_expression']}")
            try:
                with open(marker, "w") as f:
                    f.write(cfg['schedule']['cron_expression'])
            except Exception:
                pass
except Exception as e:
    log.error(f"Schedule setup failed: {e}")


@celery_app.task(bind=True, name="app.tasks.run_backup_task")
def run_backup_task(self, custom_root: str = None, tenant_id: str = None, workloads: list = None):
    """Run backup task. Auto-upload to remote destinations after success."""
    if tenant_id:
        try:
            from app.tenant_manager import TenantManager
            TenantManager().set_active_tenant(tenant_id)
        except Exception as e:
            log.warning(f"Failed to activate tenant {tenant_id}: {e}")

    config = load_config()
    requested_workloads = [str(item).strip().lower() for item in (workloads or []) if str(item).strip()]
    supported_workloads = filter_backup_workloads(requested_workloads)
    if requested_workloads and not supported_workloads:
        supported_names = ", ".join(sorted(BACKUP_ENABLED_WORKLOADS))
        raise ValueError(f"Current backup engine only supports workloads: {supported_names}")

    task_id = self.request.id
    log.info("=" * 60)
    log.info(f"BACKUP STARTED — Task: {task_id[:8]}")
    if tenant_id:
        log.info(f"  Tenant: {tenant_id}")
    if requested_workloads:
        log.info(f"  Workloads requested: {', '.join(requested_workloads)}")
    log.info("=" * 60)

    import redis as redis_lib
    r = redis_lib.Redis(host="redis", port=6379, db=2, decode_responses=True)
    r.setex("spo:current_backup_task", 86400, task_id)
    # ★ Reset control state to RUNNING when starting ★
    r.setex(f"spo:task:{task_id}:control", 86400, "running")

    def progress_cb(evt, data):
        if evt == "site_start":
            log.info(f"→ Backing up: {data.get('current_site', '?')}")
        elif evt == "site_done":
            site = data.get("site", "")
            if data.get("status") == "success":
                log.info(f"  ✅ '{site}' completed")
            else:
                log.error(f"  ❌ '{site}' failed: {data.get('error', '?')}")
        elif evt == "file_done":
            fname = data.get("current_file", "")
            if fname:
                log.info(f"  📥 {fname}")
        elif evt == "paused":
            log.warning(f"⏸  Paused")
        elif evt == "resumed":
            log.info(f"▶  Resumed")
        elif evt == "cancelled":
            log.warning(f"⛔ Cancelled")
        self.update_state(state="PROGRESS", meta={"event": evt, **data})

    engine = BackupEngine(config, progress_callback=progress_cb, task_id=task_id)

    active_tenant = None
    registry = None
    stats = {
        "total_sites": 0,
        "successful_sites": 0,
        "failed_sites": [],
        "files_downloaded": 0,
        "files_skipped": 0,
        "bytes_downloaded": 0,
        "errors": [],
        "start_time": datetime.now(timezone.utc),
        "end_time": None,
        "current_site": "",
        "cancelled": False,
        "status": "running",
        "workloads_completed": [],
        "backup_paths": {},
    }

    try:

        def merge_stats(source_stats, workload_name):
            stats["total_sites"] += source_stats.get("total_sites", 0) or 0
            stats["successful_sites"] += source_stats.get("successful_sites", 0) or 0
            stats["failed_sites"].extend(source_stats.get("failed_sites", []) or [])
            stats["files_downloaded"] += source_stats.get("files_downloaded", 0) or 0
            stats["files_skipped"] += source_stats.get("files_skipped", 0) or 0
            stats["bytes_downloaded"] += source_stats.get("bytes_downloaded", 0) or 0
            stats["errors"].extend(source_stats.get("errors", []) or [])
            if source_stats.get("cancelled"):
                stats["cancelled"] = True
            stats["workloads_completed"].append(workload_name)
            if source_stats.get("backup_path"):
                stats["backup_paths"][workload_name] = source_stats.get("backup_path")

        workloads_to_run = supported_workloads or ["sharepoint"]

        try:
            from app.tenant_manager import TenantManager

            active_tenant = TenantManager().get_active_tenant(include_secret=True)
        except Exception as e:
            log.warning(f"Failed to load active tenant context: {e}")

        if tenant_id or any(item in workloads_to_run for item in {"onedrive", "outlook", "teams"}):
            try:
                from app.backup_registry import BackupRegistry

                if active_tenant:
                    registry = BackupRegistry(config)
            except Exception as e:
                log.warning(f"Failed to load tenant-aware backup context: {e}")

        def resolve_workload_root(workload_name: str):
            if custom_root and len(workloads_to_run) == 1:
                return custom_root
            if registry and active_tenant:
                return str(registry.get_tenant_backup_root(active_tenant, workload_name))
            if workload_name == "sharepoint":
                return custom_root or config["backup"]["root_dir"]
            raise ValueError(f"No tenant-aware backup root available for workload '{workload_name}'")

        if "sharepoint" in workloads_to_run:
            sharepoint_root = resolve_workload_root("sharepoint")
            sharepoint_stats = engine.run_backup(custom_root=sharepoint_root)
            merge_stats(sharepoint_stats, "sharepoint")

        if not stats.get("cancelled"):
            graph_workload_specs = [
                ("onedrive", "OneDrive", "users_count", "app.workloads.onedrive", "OneDriveWorkload"),
                ("outlook", "Outlook", "mailbox_count", "app.workloads.outlook", "OutlookWorkload"),
                ("teams", "Teams", "teams_count", "app.workloads.teams", "TeamsWorkload"),
            ]

            for workload_name, label, total_key, module_name, class_name in graph_workload_specs:
                if stats.get("cancelled") or workload_name not in workloads_to_run:
                    continue
                if not active_tenant:
                    raise ValueError(f"No active tenant configured for {label} backup")

                workload_root = resolve_workload_root(workload_name)

                def workload_progress_cb(evt, data, current_workload=workload_name, current_label=label):
                    if evt == "target_start":
                        log.info(f"→ {current_label} backup: {data.get('target_name', '?')}")
                    elif evt == "target_done":
                        if data.get("status") == "success":
                            log.info(f"  ✅ {current_label} '{data.get('target_name', '?')}' completed")
                        else:
                            log.error(f"  ❌ {current_label} '{data.get('target_name', '?')}' failed: {data.get('error', '?')}")
                    elif evt == "file_done":
                        fname = data.get("current_file", "")
                        if fname:
                            log.info(f"  📥 {current_label}: {fname}")
                    self.update_state(state="PROGRESS", meta={"event": f"{current_workload}_{evt}", **data})

                workload_module = __import__(module_name, fromlist=[class_name])
                workload_cls = getattr(workload_module, class_name)
                workload_runner = workload_cls(
                    active_tenant,
                    backup_root=workload_root,
                    progress_callback=workload_progress_cb,
                    task_id=task_id,
                )
                workload_stats = workload_runner.backup()
                workload_stats.setdefault("total_sites", workload_stats.get(total_key, 0))
                workload_stats.setdefault("successful_sites", workload_stats.get("targets_processed", 0))
                workload_stats.setdefault("failed_sites", [])
                merge_stats(workload_stats, workload_name)

        stats["end_time"] = datetime.now(timezone.utc)
        for k in ("start_time", "end_time"):
            if stats.get(k):
                stats[k] = stats[k].isoformat()

        # ★ Check if cancelled — DON'T do post-processing if so ★
        if stats.get("cancelled"):
            stats["status"] = "cancelled"
            log.warning(f"⛔ Backup task {task_id[:8]} was CANCELLED")
            r.delete("spo:current_backup_task")
            r.delete(f"spo:task:{task_id}:control")
            return stats

        fatal_errors = [str(err) for err in stats.get("errors", []) if str(err).startswith("Fatal:")]
        task_failure = None
        if fatal_errors:
            stats["status"] = "failed"
            task_failure = " | ".join(fatal_errors[:3])
            log.error(f"BACKUP FAILED — fatal workload error(s): {task_failure}")
        else:
            stats["status"] = "success"

        log.info("=" * 60)
        log.info(f"BACKUP COMPLETED — {stats.get('successful_sites', 0)}/{stats.get('total_sites', 0)} sites")
        log.info(f"  Files DL : {stats.get('files_downloaded', 0)}")
        log.info(f"  Total Size: {stats.get('bytes_downloaded', 0) / 1024 / 1024:.2f} MB")
        log.info("=" * 60)

        for workload_name, backup_path in stats.get("backup_paths", {}).items():
            layout = "tenant-aware" if "/m365/" in str(backup_path).replace("\\", "/") else "legacy"
            _write_workload_manifest(backup_path, workload_name, stats, active_tenant, layout)

        # ★ Remote upload (if uploader available + has destinations + backup successful)
        if HAS_UPLOADER:
            remote_dests = config.get("backup", {}).get("remote_destinations", [])
            enabled_dests = [d for d in remote_dests if d.get("enabled", True)]
            if enabled_dests and stats.get("successful_sites", 0) > 0:
                upload_sources = []
                for workload_name, backup_path in stats.get("backup_paths", {}).items():
                    if backup_path and os.path.isdir(backup_path):
                        upload_sources.append((workload_name, backup_path))

                if not upload_sources and custom_root and os.path.isdir(custom_root):
                    upload_sources.append(("legacy", custom_root))

                if upload_sources:
                    log.info("=" * 60)
                    log.info(f"UPLOADING {len(upload_sources)} BACKUP SET(S) TO {len(enabled_dests)} REMOTE DESTINATION(S)")
                    log.info("=" * 60)
                    upload_results = []
                    for workload_name, local_backup_dir in upload_sources:
                        log.info(f"Source workload: {workload_name} — {local_backup_dir}")
                        for dest in enabled_dests:
                            proto = dest.get("protocol", "?").upper()
                            name = dest.get("name", "unnamed")
                            log.info(f"→ Uploading via {proto} to '{name}'...")
                            try:
                                result = upload_to_remote(dest, local_backup_dir)
                                log.info(f"  ✅ {name}: {result.get('uploaded', 0)} files, "
                                         f"{result.get('bytes', 0) / 1024 / 1024:.2f} MB")
                                upload_results.append({
                                    "workload": workload_name,
                                    "source": local_backup_dir,
                                    "name": name,
                                    "protocol": proto,
                                    "status": "success",
                                    "uploaded": result.get("uploaded", 0),
                                    "bytes": result.get("bytes", 0),
                                })
                            except Exception as e:
                                log.error(f"  ❌ {name} failed: {e}")
                                upload_results.append({
                                    "workload": workload_name,
                                    "source": local_backup_dir,
                                    "name": name,
                                    "protocol": proto,
                                    "status": "failed",
                                    "error": str(e),
                                })
                    stats["remote_uploads"] = upload_results

        try:
            from app.tasks_v12 import send_tenant_notification
            send_tenant_notification(tenant_id, stats)
        except Exception as e:
            log.warning(f"Tenant-aware notification failed: {e}")
            try:
                NotificationDispatcher(config).send_all(stats)
            except Exception as e2:
                log.error(f"Notification failed: {e2}")
                stats["notification_error"] = str(e2)

        r.delete("spo:current_backup_task")
        r.delete(f"spo:task:{task_id}:control")
        if task_failure:
            self.update_state(state="BACKUP_FAILED", meta=stats)
            raise Ignore()
        return stats

    except Ignore:
        raise
    except Exception as e:
        if engine and engine.stats.get("backup_path") and "sharepoint" not in stats.get("backup_paths", {}):
            stats.setdefault("backup_paths", {})["sharepoint"] = engine.stats.get("backup_path")
        if engine and engine.stats.get("resumed_existing_backup") is not None:
            stats["resumed_existing_backup"] = engine.stats.get("resumed_existing_backup")
        stats["end_time"] = datetime.now(timezone.utc).isoformat()
        if stats.get("status") not in {"cancelled", "success", "failed"}:
            stats["status"] = "interrupted"
        stats["errors"].append(f"Interrupted: {e}")
        for workload_name, backup_path in stats.get("backup_paths", {}).items():
            layout = "tenant-aware" if "/m365/" in str(backup_path).replace("\\", "/") else "legacy"
            _write_workload_manifest(backup_path, workload_name, stats, active_tenant, layout)
        log.error(f"BACKUP TASK FAILED: {e}", exc_info=True)
        r.delete("spo:current_backup_task")
        r.delete(f"spo:task:{task_id}:control")
        raise


@celery_app.task(bind=True, name="app.tasks.download_custom_url_task")
def download_custom_url_task(self, url: str, dest_dir: str = None):
    config = load_config()
    task_id = self.request.id
    log.info("=" * 60)
    log.info(f"CUSTOM DOWNLOAD — Task: {task_id[:8]}")
    log.info(f"  URL : {url}")
    log.info("=" * 60)

    import redis as redis_lib
    r = redis_lib.Redis(host="redis", port=6379, db=2, decode_responses=True)
    r.setex("spo:current_download_task", 86400, task_id)
    r.setex(f"spo:task:{task_id}:control", 86400, "running")

    def progress_cb(evt, data):
        if evt == "file_done":
            fname = data.get("current_file", "")
            if fname:
                log.info(f"  📥 {fname}")
        self.update_state(state="PROGRESS", meta={"event": evt, **data})

    engine = BackupEngine(config, progress_callback=progress_cb, task_id=task_id)
    try:
        result = engine.download_custom_url(url, dest_dir=dest_dir)
        log.info(f"DOWNLOAD COMPLETED — {result.get('downloaded', 0)} files")
        r.delete("spo:current_download_task")
        r.delete(f"spo:task:{task_id}:control")
        return result
    except Exception as e:
        log.error(f"DOWNLOAD FAILED: {e}", exc_info=True)
        r.delete("spo:current_download_task")
        r.delete(f"spo:task:{task_id}:control")
        raise


@celery_app.task(bind=True, name="app.tasks.run_restore_task")
def run_restore_task(self, backup_name, site_name, target_site_path=None, dry_run=False):
    log.info(f"RESTORE — {backup_name} / {site_name}")
    return RestoreEngine(load_config()).restore_site(backup_name, site_name, target_site_path, dry_run)


@celery_app.task(name="app.tasks.send_test_notification")
def send_test_notification(channel_name=None):
    log.info(f"TEST NOTIFICATION — {channel_name or 'all'}")
    return NotificationDispatcher(load_config()).send_test(channel_name)


@celery_app.task(name="app.tasks.test_remote_destination_task")
def test_remote_destination_task(dest_config):
    if not HAS_UPLOADER:
        return {"status": "error", "message": "Uploader module not available"}
    return test_remote_destination(dest_config)


# ★★★ NEW: Force cancel task helper ★★★
def force_cancel_task(task_id: str):
    """
    Properly cancel a task:
    1. Set Redis flag to 'cancelled'
    2. Revoke Celery task (terminate=True if needed)
    3. Clean up Redis keys
    """
    import redis as redis_lib
    r = redis_lib.Redis(host="redis", port=6379, db=2, decode_responses=True)

    # Step 1: Set cancel flag (engine will pick this up)
    r.setex(f"spo:task:{task_id}:control", 86400, "cancelled")
    log.warning(f"Task {task_id[:8]} marked as CANCELLED in Redis")

    # Step 2: Revoke Celery task — gives soft signal first, then SIGTERM
    try:
        celery_app.control.revoke(task_id, terminate=True, signal="SIGTERM")
        log.warning(f"Task {task_id[:8]} REVOKED via Celery control")
    except Exception as e:
        log.error(f"Failed to revoke task: {e}")

    # Step 3: Clean up current task tracking
    r.delete("spo:current_backup_task")
    r.delete("spo:current_download_task")


try:
    from app.tasks_v12 import register_tenant_schedules
    register_tenant_schedules(celery_app)
except Exception as e:
    log.error(f"v12 schedule registration failed: {e}")

try:
    from app.tasks_v13 import register_v13_tasks
    register_v13_tasks(celery_app)
except Exception as e:
    log.error(f"v13 tasks registration failed: {e}")
