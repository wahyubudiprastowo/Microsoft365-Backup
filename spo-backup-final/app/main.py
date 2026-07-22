"""Microsoft 365 Backup v9.0 — Robust cancel/delete + async backup list."""
import os
import json
import logging
import shutil
from urllib.parse import urlparse
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, render_template, request, jsonify, redirect, url_for
from celery.result import AsyncResult

from app.config_manager import load_config, save_config, add_site, remove_site, toggle_site
from app.backup_engine import RestoreEngine, BackupEngine
from app.main_routes import register_m365_routes
from app.main_routes_v11 import register_v11_routes
from app.main_routes_v12 import register_v12_routes
from app.main_routes_v13 import register_v13_routes
from app.task_control import TaskController
from app.operation_queue import OperationQueue

from logging.handlers import RotatingFileHandler
LOG_FILE = "/app/logs/spo_backup.log"
os.makedirs("/app/logs", exist_ok=True)

log = logging.getLogger("spo_backup")
log.setLevel(logging.INFO)
if not log.handlers:
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=5)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    log.addHandler(ch)
log.propagate = False

import redis as redis_lib
_redis = redis_lib.Redis(host="redis", port=6379, db=2, decode_responses=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-key")
register_m365_routes(app)
register_v11_routes(app)
register_v12_routes(app)
register_v13_routes(app)

_restore_engine_cache = None


def _normalize_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _validate_remote_destination(dest, existing_password=""):
    if not isinstance(dest, dict):
        raise ValueError("Destination payload must be an object")
    name = str(dest.get("name", "")).strip()
    protocol = str(dest.get("protocol", "")).strip().lower()
    config = dest.get("config") or {}
    if not name:
        raise ValueError("Remote destination name is required")
    if protocol not in {"smb", "ftp", "sftp", "webdav"}:
        raise ValueError("Unsupported remote protocol. Use smb, ftp, sftp, or webdav")
    if not isinstance(config, dict):
        raise ValueError(f"Remote destination '{name}' has invalid config payload")

    remote_path = str(config.get("remote_path", "") or "/").strip()
    if not remote_path:
        raise ValueError(f"Remote destination '{name}' requires remote_path")

    password = config.get("password", "")
    if password == "***MASKED***":
        password = existing_password

    normalized = {
        "name": name,
        "protocol": protocol,
        "enabled": _normalize_bool(dest.get("enabled", True)),
        "config": {
            "username": str(config.get("username", "") or "").strip(),
            "password": password,
            "remote_path": remote_path,
        },
    }

    if protocol == "webdav":
        raw_url = str(config.get("url", "") or "").strip()
        if not raw_url:
            raise ValueError(f"WebDAV destination '{name}' requires URL")
        parsed = urlparse(raw_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"WebDAV destination '{name}' must use a valid http/https URL")
        normalized["config"]["url"] = raw_url
        if not normalized["config"]["username"]:
            raise ValueError(f"WebDAV destination '{name}' requires username")
        if not normalized["config"]["password"]:
            raise ValueError(f"WebDAV destination '{name}' requires password")
        return normalized

    server = str(config.get("server", "") or "").strip()
    if not server:
        raise ValueError(f"Remote destination '{name}' requires server/host")
    normalized["config"]["server"] = server

    port = config.get("port")
    if port in (None, ""):
        defaults = {"smb": 445, "ftp": 21, "sftp": 22}
        port = defaults[protocol]
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ValueError(f"Remote destination '{name}' has invalid port")
    if port < 1 or port > 65535:
        raise ValueError(f"Remote destination '{name}' port must be between 1 and 65535")
    normalized["config"]["port"] = port

    if protocol == "smb":
        share = str(config.get("share", "") or "").strip()
        if not share:
            raise ValueError(f"SMB destination '{name}' requires share name")
        if not normalized["config"]["username"]:
            raise ValueError(f"SMB destination '{name}' requires username")
        if not normalized["config"]["password"]:
            raise ValueError(f"SMB destination '{name}' requires password")
        normalized["config"]["share"] = share
        normalized["config"]["domain"] = str(config.get("domain", "") or "WORKGROUP").strip() or "WORKGROUP"
    elif protocol == "ftp":
        if not normalized["config"]["username"]:
            raise ValueError(f"FTP destination '{name}' requires username")
        if not normalized["config"]["password"]:
            raise ValueError(f"FTP destination '{name}' requires password")
        normalized["config"]["use_tls"] = _normalize_bool(config.get("use_tls", False))
    elif protocol == "sftp":
        key_path = str(config.get("private_key_path", "") or "").strip()
        if not normalized["config"]["username"]:
            raise ValueError(f"SFTP destination '{name}' requires username")
        if not normalized["config"]["password"] and not key_path:
            raise ValueError(f"SFTP destination '{name}' requires password or private key path")
        if key_path:
            normalized["config"]["private_key_path"] = key_path

    return normalized


def _validate_notification_config(cfg, channel=None):
    notif = cfg.get("notification", {}) or {}
    selected = (channel or "").strip().lower()
    if selected and selected not in {"email", "telegram", "teams"}:
        raise ValueError("Unknown notification channel")

    def wants(name):
        return selected in {"", name}

    if wants("email"):
        method = str(notif.get("method", "") or "").strip().lower()
        email_from = str(notif.get("email_from", "") or "").strip()
        email_to = [str(item).strip() for item in notif.get("email_to", []) if str(item).strip()]
        if not method:
            raise ValueError("Notification email method is required")
        if method not in {"graph", "smtp"}:
            raise ValueError("Notification email method must be graph or smtp")
        if not email_from:
            raise ValueError("Notification email_from is required")
        if not email_to:
            raise ValueError("At least one notification email_to recipient is required")
        if method == "smtp":
            smtp = notif.get("smtp", {}) or {}
            if not str(smtp.get("server", "") or "").strip():
                raise ValueError("SMTP server is required for email notification test")
            try:
                port = int(smtp.get("port"))
            except (TypeError, ValueError):
                raise ValueError("SMTP port is invalid")
            if port < 1 or port > 65535:
                raise ValueError("SMTP port must be between 1 and 65535")
            if not str(smtp.get("username", "") or "").strip():
                raise ValueError("SMTP username is required for email notification test")
            if not str(smtp.get("password", "") or "").strip():
                raise ValueError("SMTP password is required for email notification test")

    if wants("telegram"):
        tg = notif.get("telegram", {}) or {}
        if not str(tg.get("bot_token", "") or "").strip():
            raise ValueError("Telegram bot token is required")
        chat_ids = [str(item).strip() for item in tg.get("chat_ids", []) if str(item).strip()]
        if not chat_ids:
            raise ValueError("At least one Telegram chat ID is required")

    if wants("teams"):
        teams = notif.get("teams", {}) or {}
        hooks = [str(item).strip() for item in teams.get("webhook_urls", []) if str(item).strip()]
        if not hooks:
            raise ValueError("At least one Microsoft Teams webhook URL is required")
        for hook in hooks:
            parsed = urlparse(hook)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("Microsoft Teams webhook URL must be a valid http/https URL")


def _resolve_tracked_task(task_type: str):
    key_map = {
        "backup": "spo:current_backup_task",
        "download": "spo:current_download_task",
    }
    task_name_map = {
        "backup": "app.tasks.run_backup_task",
        "download": "app.tasks.download_custom_url_task",
    }
    redis_key = key_map.get(task_type)
    if not redis_key:
        return None

    def discover_fallback_task():
        task_name = task_name_map.get(task_type)
        if not task_name:
            return None
        discovered = _discover_worker_task(task_name)
        if not discovered:
            return None
        discovered_task_id = discovered.get("id")
        if not discovered_task_id:
            return None
        try:
            _redis.setex(redis_key, 86400, discovered_task_id)
        except redis_lib.RedisError as e:
            log.warning(f"Failed to restore tracked {task_type} task in Redis: {e}")
        return discovered_task_id

    try:
        task_id = _redis.get(redis_key)
    except redis_lib.RedisError as e:
        log.warning(f"Failed to read tracked {task_type} task from Redis: {e}")
        return discover_fallback_task()
    if not task_id:
        return discover_fallback_task()

    capp, _, _, _, _, _, _ = get_celery()
    result = AsyncResult(task_id, app=capp)

    try:
        state = result.state
        info = result.info
    except Exception:
        try:
            _redis.delete(redis_key)
        except redis_lib.RedisError:
            pass
        TaskController.cleanup(task_id)
        return discover_fallback_task()

    if state in {"SUCCESS", "FAILURE", "REVOKED", "BACKUP_FAILED"}:
        try:
            _redis.delete(redis_key)
        except redis_lib.RedisError:
            pass
        TaskController.cleanup(task_id)
        return discover_fallback_task()

    if state == "PENDING" and not info:
        worker_active = _is_worker_task_active(task_id)
        if worker_active is not False:
            return task_id
        try:
            _redis.delete(redis_key)
        except redis_lib.RedisError:
            pass
        TaskController.cleanup(task_id)
        return discover_fallback_task()

    if state in {"PROGRESS", "STARTED"}:
        worker_active = _is_worker_task_active(task_id)
        if worker_active is not False:
            return task_id
        try:
            _redis.delete(redis_key)
        except redis_lib.RedisError:
            pass
        TaskController.cleanup(task_id)
        return discover_fallback_task()

    return task_id


def _list_worker_tasks(task_name: str):
    try:
        capp, _, _, _, _, _, _ = get_celery()
        inspector = capp.control.inspect(timeout=1.5)
        snapshots = [
            ("active", inspector.active() or {}, 3),
            ("reserved", inspector.reserved() or {}, 2),
            ("scheduled", inspector.scheduled() or {}, 1),
        ]
        candidates = {}
        for kind, payload, priority in snapshots:
            for worker_tasks in payload.values():
                for raw_item in worker_tasks or []:
                    item = raw_item.get("request", raw_item) if isinstance(raw_item, dict) and "request" in raw_item else raw_item
                    if not isinstance(item, dict):
                        continue
                    if item.get("name") != task_name and item.get("type") != task_name:
                        continue
                    task_id = item.get("id")
                    if not task_id:
                        continue
                    score = (priority, float(item.get("time_start") or 0))
                    current = candidates.get(task_id)
                    if not current or score > current["score"]:
                        normalized = dict(item)
                        normalized["_queue_kind"] = kind
                        normalized["_queue_priority"] = priority
                        candidates[task_id] = {"score": score, "item": normalized}
        return [
            entry["item"]
            for entry in sorted(candidates.values(), key=lambda entry: entry["score"], reverse=True)
        ]
    except Exception as e:
        log.warning(f"Failed to inspect worker task '{task_name}': {e}")
        return []


def _discover_worker_task(task_name: str):
    tasks = _list_worker_tasks(task_name)
    return tasks[0] if tasks else None


def _is_worker_task_active(task_id: str):
    try:
        capp, _, _, _, _, _, _ = get_celery()
        inspector = capp.control.inspect(timeout=1.0)
        active = inspector.active() or {}
        for worker_tasks in active.values():
            for item in worker_tasks or []:
                if item.get("id") == task_id:
                    return True
    except Exception as e:
        log.warning(f"Failed to inspect active worker tasks: {e}")
        return None
    return False


def _calculate_backup_size(path: str) -> int:
    total = 0
    if not path or not os.path.isdir(path):
        return total
    for root, _, files in os.walk(path):
        for filename in files:
            if filename.endswith(".tmp"):
                continue
            try:
                total += os.path.getsize(os.path.join(root, filename))
            except OSError:
                pass
    return total


def get_active_task_overlay(task_type: str, prefer_disk_size: bool = False):
    task_id = _resolve_tracked_task(task_type)
    if not task_id:
        return None

    capp, _, _, _, _, _, _ = get_celery()
    result = AsyncResult(task_id, app=capp)
    try:
        state = result.state
        info = result.info
    except Exception as e:
        log.warning(f"Failed to read active {task_type} task snapshot for {task_id[:8]}: {e}")
        return {"task_id": task_id, "state": "UNKNOWN", "error": str(e)}

    snapshot = {
        "task_id": task_id,
        "state": state,
        "control_state": TaskController.get_state(task_id),
    }
    if isinstance(info, dict):
        snapshot["meta"] = info

    backup_path = None
    live_size = 0
    if task_type == "backup":
        backup_path = (snapshot.get("meta") or {}).get("backup_path")
    elif task_type == "download":
        backup_path = (snapshot.get("meta") or {}).get("dest")

    meta = snapshot.get("meta") or {}
    live_size = int(meta.get("bytes_done") or meta.get("bytes_downloaded") or 0)
    if backup_path:
        if prefer_disk_size:
            live_size = max(live_size, _calculate_backup_size(backup_path))
    snapshot["live_size_bytes"] = live_size
    snapshot["live_size_human"] = _human_size(live_size)
    return snapshot


def get_active_backup_guard():
    worker_tasks = _list_worker_tasks("app.tasks.run_backup_task")
    if not worker_tasks:
        active_backup = get_active_task_overlay("backup")
        if not active_backup:
            return None
        state = str(active_backup.get("state") or "").upper()
        if state in {"SUCCESS", "FAILURE", "REVOKED", "BACKUP_FAILED", "UNKNOWN"}:
            return None
        return {
            "task_id": active_backup.get("task_id"),
            "state": state or "PROGRESS",
            "control_state": active_backup.get("control_state", "running"),
            "meta": active_backup.get("meta") or {},
            "live_size_bytes": active_backup.get("live_size_bytes", 0),
            "live_size_human": active_backup.get("live_size_human", "0 B"),
            "queue_kind": "tracked",
            "active_task_ids": [active_backup.get("task_id")],
        }

    capp, _, _, _, _, _, _ = get_celery()
    active_items = []
    for item in worker_tasks:
        task_id = item.get("id")
        if not task_id:
            continue
        control_state = TaskController.get_state(task_id)
        queue_kind = item.get("_queue_kind") or "active"
        if control_state == "cancelled" and queue_kind != "active":
            continue
        try:
            result = AsyncResult(task_id, app=capp)
            state = str(result.state or "").upper()
            info = result.info if isinstance(result.info, dict) else {}
        except Exception:
            state = "UNKNOWN"
            info = {}
        if state in {"SUCCESS", "FAILURE", "REVOKED", "BACKUP_FAILED"} and queue_kind != "active":
            continue
        active_items.append({
            "task_id": task_id,
            "state": state or "PENDING",
            "control_state": control_state,
            "queue_kind": queue_kind,
            "meta": info,
        })

    if not active_items:
        return None

    primary = active_items[0]
    live_size = int(primary.get("meta", {}).get("bytes_done") or primary.get("meta", {}).get("bytes_downloaded") or 0)
    return {
        "task_id": primary.get("task_id"),
        "state": primary.get("state"),
        "control_state": primary.get("control_state"),
        "queue_kind": primary.get("queue_kind"),
        "meta": primary.get("meta") or {},
        "live_size_bytes": live_size,
        "live_size_human": _human_size(live_size),
        "active_task_ids": [item["task_id"] for item in active_items],
    }


def get_active_download_guard():
    active_download = get_active_task_overlay("download")
    if not active_download:
        return None
    state = str(active_download.get("state") or "").upper()
    if state in {"SUCCESS", "FAILURE", "REVOKED", "BACKUP_FAILED", "UNKNOWN"}:
        return None
    return {
        "task_id": active_download.get("task_id"),
        "state": state or "PROGRESS",
        "control_state": active_download.get("control_state", "running"),
        "meta": active_download.get("meta") or {},
        "live_size_bytes": active_download.get("live_size_bytes", 0),
        "live_size_human": active_download.get("live_size_human", "0 B"),
    }


def get_tasks_overview():
    from app.restore_manager_v2 import RestoreManagerV2

    queue = OperationQueue()
    restore_mgr = RestoreManagerV2()
    restore_mgr.recover_stale_queued_jobs(limit=100)
    restore_jobs = restore_mgr.list_jobs(limit=100)
    running_restore = next((job for job in restore_jobs if job.get("status") == "running"), None)
    queued_restore_jobs = [job for job in restore_jobs if job.get("status") == "queued" and not job.get("task_id")]

    return {
        "running": {
            "backup": get_active_backup_guard(),
            "download": get_active_download_guard(),
            "restore": running_restore,
        },
        "queued": {
            "backup": queue.list("backup", limit=20),
            "download": queue.list("download", limit=20),
            "restore": [
                {
                    "id": job["id"],
                    "group": "restore",
                    "operation": "restore_v2",
                    "title": f"Restore {job.get('workload', 'restore')}",
                    "detail": f"{job.get('tenant_name') or 'Unknown tenant'} · {job.get('source_backup') or ''}",
                    "status": "queued",
                    "created_at": job.get("created_at"),
                }
                for job in queued_restore_jobs
            ],
        },
    }


def maybe_dispatch_queued_operations():
    from app.operation_dispatcher import dispatch_next_queued_operation

    dispatched = []
    queue = OperationQueue()
    if not get_active_backup_guard() and queue.length("backup"):
        result = dispatch_next_queued_operation("backup")
        if result and not result.get("error") and result.get("status") != "busy":
            dispatched.append(result)
    if not get_active_download_guard() and queue.length("download"):
        result = dispatch_next_queued_operation("download")
        if result and not result.get("error") and result.get("status") != "busy":
            dispatched.append(result)
    try:
        from app.restore_manager_v2 import RestoreManagerV2

        restore_mgr = RestoreManagerV2()
        restore_mgr.recover_stale_queued_jobs(limit=100)
        running_restore = next((item for item in restore_mgr.list_jobs(limit=100) if item.get("status") == "running"), None)
        if not running_restore and queue.length("restore"):
            result = dispatch_next_queued_operation("restore")
            if result and not result.get("error") and result.get("status") != "busy":
                dispatched.append(result)
    except Exception:
        pass
    return dispatched

def get_restore_engine():
    global _restore_engine_cache
    config = load_config()
    if _restore_engine_cache is None:
        _restore_engine_cache = RestoreEngine(config)
    else:
        _restore_engine_cache.config = config
    return _restore_engine_cache


def get_celery():
    from app.tasks import (celery_app, run_backup_task, run_restore_task,
                           send_test_notification, download_custom_url_task,
                           test_remote_destination_task, force_cancel_task)
    return (celery_app, run_backup_task, run_restore_task, send_test_notification,
            download_custom_url_task, test_remote_destination_task, force_cancel_task)


def _read_logs(n):
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            return f.readlines()[-n:]
    return []


# ═══════════════════════════════════════════════════════════════
# ★ FIX #2: Fast list_backups WITHOUT scanning all files ★
# ═══════════════════════════════════════════════════════════════
def fast_list_backups():
    """Lightweight version — only reads folder names + mtime, NO disk scan."""
    config = load_config()
    root = Path(config["backup"]["root_dir"])
    if not root.exists():
        return []
    backups = []
    for d in sorted(root.iterdir(), reverse=True):
        if d.is_dir() and (d.name.startswith("backup_") or d.name.startswith("custom_")):
            try:
                # Try to use cached size first
                size = 0
                size_file = d / "_size_cache.json"
                if size_file.exists():
                    try:
                        size = json.load(open(size_file)).get("size_bytes", 0)
                    except Exception:
                        pass
                # Don't compute size if no cache — let it be 0 (faster)
                type_ = "custom" if d.name.startswith("custom_") else "scheduled"
                backups.append({
                    "name": d.name, "type": type_,
                    "date": datetime.fromtimestamp(d.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "size_bytes": size,
                    "size_human": _human_size(size),
                    "sites": [],  # Skip subfolder listing for speed
                    "site_count": 0,
                })
            except Exception as e:
                log.warning(f"Skip backup {d.name}: {e}")
    return backups


def _human_size(b):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def build_dashboard_summary(config=None):
    from app.backup_registry import BackupRegistry
    from app.schedule_manager import ScheduleManager, DEFAULT_SCHEDULE
    from app.tenant_manager import TenantManager

    config = config or load_config()
    sites = config.get("sites", []) or []
    enabled_sites = [site for site in sites if site.get("enabled")]

    tenant_mgr = TenantManager()
    active_tenant = tenant_mgr.get_active_tenant(include_secret=False)

    schedule_mgr = ScheduleManager()
    if active_tenant:
        active_schedule = schedule_mgr.get_schedule(active_tenant.get("id"))
        schedule_scope = "Per-Tenant Schedule"
        schedule_owner = active_tenant.get("name") or "Active tenant"
        schedule_workloads = active_tenant.get("workloads_enabled", ["sharepoint"]) or ["sharepoint"]
    else:
        active_schedule = dict(DEFAULT_SCHEDULE)
        active_schedule.update(config.get("schedule", {}) or {})
        schedule_scope = "Legacy Global Schedule"
        schedule_owner = "Global legacy flow"
        schedule_workloads = ["sharepoint"]

    registry = BackupRegistry(config)
    backup_stats = registry.get_stats()

    return {
        "sites_total": len(sites),
        "sites_enabled": len(enabled_sites),
        "backups_total": int(backup_stats.get("total_backups", 0) or 0),
        "active_tenant": active_tenant,
        "schedule": {
            "cron_expression": str(active_schedule.get("cron_expression") or DEFAULT_SCHEDULE["cron_expression"]),
            "enabled": bool(active_schedule.get("enabled")),
            "timezone": str(active_schedule.get("timezone") or DEFAULT_SCHEDULE["timezone"]),
            "scope": schedule_scope,
            "owner": schedule_owner,
            "workloads": [str(item).strip().lower() for item in schedule_workloads if str(item).strip()],
        },
    }


# ── PAGES ────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    config = load_config()
    summary = build_dashboard_summary(config)
    # ★ Use fast list for dashboard — only top 10
    backups = fast_list_backups()[:10]
    active_backup = get_active_task_overlay("backup")
    if active_backup:
        active_meta = active_backup.get("meta") or {}
        active_path = active_meta.get("backup_path", "")
        active_name = os.path.basename(active_path.rstrip("/")) if active_path else ""
        live_size = int(active_backup.get("live_size_bytes") or 0)
        for item in backups:
            if item.get("name") == active_name and live_size > int(item.get("size_bytes") or 0):
                item["size_bytes"] = live_size
                item["size_human"] = _human_size(live_size)
                break
    return render_template("dashboard.html", config=config, backups=backups, logs=_read_logs(30), dashboard_summary=summary)


@app.route("/sites")
def sites_page():
    return render_template("sites.html", config=load_config())


@app.route("/download")
def download_page():
    return render_template("download.html", config=load_config())


@app.route("/backups")
def backups_page():
    """★ FAST page — no disk scan, just folder list. Sizes loaded via AJAX."""
    config = load_config()
    backups = fast_list_backups()
    return render_template("backups.html", config=config, backups=backups)


@app.route("/restore")
def restore_page():
    return redirect(url_for("restore_v2_page"))


@app.route("/settings")
def settings_page():
    return render_template("settings.html", config=load_config())


@app.route("/schedules")
def schedules_page():
    return render_template("tenant_schedule.html", config=load_config())


@app.route("/logs")
def logs_page():
    return render_template("logs.html", logs=_read_logs(200))


# ── API ──────────────────────────────────────────────────────────

@app.route("/api/health")
def api_health():
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat()})


@app.route("/api/dashboard/summary")
def api_dashboard_summary():
    return jsonify(build_dashboard_summary())


@app.route("/api/task/active")
def api_active_task():
    try:
        maybe_dispatch_queued_operations()
        backup_snapshot = get_active_task_overlay("backup")
        download_snapshot = get_active_task_overlay("download")
        return jsonify({
            "backup_task_id": backup_snapshot.get("task_id") if backup_snapshot else None,
            "download_task_id": download_snapshot.get("task_id") if download_snapshot else None,
            "backup": backup_snapshot,
            "download": download_snapshot,
        })
    except redis_lib.RedisError as e:
        log.warning(f"/api/task/active degraded because Redis is unavailable: {e}")
        return jsonify({
            "backup_task_id": None,
            "download_task_id": None,
            "degraded": True,
            "warning": "Task tracking is temporarily unavailable because Redis is unreachable.",
        })


@app.route("/api/tasks/overview")
def api_tasks_overview():
    maybe_dispatch_queued_operations()
    return jsonify(get_tasks_overview())


# Config
@app.route("/api/config", methods=["GET"])
def api_get_config():
    c = load_config()
    safe = json.loads(json.dumps(c))
    if safe.get("azure_ad", {}).get("client_secret"):
        safe["azure_ad"]["client_secret"] = "***MASKED***"
    if safe.get("notification", {}).get("smtp", {}).get("password"):
        safe["notification"]["smtp"]["password"] = "***MASKED***"
    for dest in safe.get("backup", {}).get("remote_destinations", []):
        if dest.get("config", {}).get("password"):
            dest["config"]["password"] = "***MASKED***"
    return jsonify(safe)


@app.route("/api/config", methods=["POST"])
def api_save_config():
    try:
        n = request.json
        c = load_config()
        if n.get("azure_ad", {}).get("client_secret") == "***MASKED***":
            n["azure_ad"]["client_secret"] = c["azure_ad"]["client_secret"]
        if n.get("notification", {}).get("smtp", {}).get("password") == "***MASKED***":
            n["notification"]["smtp"]["password"] = c["notification"]["smtp"]["password"]
        save_config(n)
        return jsonify({"status": "saved"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/schedule", methods=["POST"])
def api_save_schedule():
    try:
        data = request.json
        c = load_config()
        c["schedule"] = {
            "enabled": data.get("enabled", True),
            "cron_expression": data.get("cron_expression", "0 2 * * *").strip(),
            "timezone": data.get("timezone", "Asia/Jakarta"),
        }
        if len(c["schedule"]["cron_expression"].split()) != 5:
            return jsonify({"error": "Cron must have 5 parts"}), 400
        save_config(c)
        # ★ Clear marker so next worker boot will log the new schedule
        try:
            os.remove("/tmp/.spo_schedule_logged")
        except Exception:
            pass
        return jsonify({"status": "saved", "schedule": c["schedule"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# Remote destinations
@app.route("/api/remote-destinations", methods=["GET"])
def api_get_remote_dests():
    c = load_config()
    dests = c.get("backup", {}).get("remote_destinations", [])
    safe_dests = json.loads(json.dumps(dests))
    for d in safe_dests:
        if d.get("config", {}).get("password"):
            d["config"]["password"] = "***MASKED***"
    return jsonify({"destinations": safe_dests})


@app.route("/api/remote-destinations", methods=["POST"])
def api_save_remote_dests():
    try:
        data = request.json or {}
        c = load_config()
        if "backup" not in c:
            c["backup"] = {}
        new_dests = data.get("destinations", [])
        if not isinstance(new_dests, list):
            return jsonify({"error": "destinations must be an array"}), 400
        old_dests = c.get("backup", {}).get("remote_destinations", [])
        normalized = []
        seen_names = set()
        for i, d in enumerate(new_dests):
            old_password = ""
            if i < len(old_dests):
                old_password = old_dests[i].get("config", {}).get("password", "")
            entry = _validate_remote_destination(d, existing_password=old_password)
            key = entry["name"].strip().lower()
            if key in seen_names:
                return jsonify({"error": f"Duplicate remote destination name: {entry['name']}"}), 400
            seen_names.add(key)
            normalized.append(entry)
        c["backup"]["remote_destinations"] = normalized
        save_config(c)
        return jsonify({"status": "saved", "count": len(normalized)})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/remote-destinations/test", methods=["POST"])
def api_test_remote_dest():
    try:
        data = request.json or {}
        c = load_config()
        existing = c.get("backup", {}).get("remote_destinations", [])
        existing_password = ""
        for d in existing:
            if d.get("name") == data.get("name"):
                existing_password = d.get("config", {}).get("password", "")
                break
        data = _validate_remote_destination(data, existing_password=existing_password)
        _, _, _, _, _, test_remote_task, _ = get_celery()
        task = test_remote_task.delay(data)
        return jsonify({"status": "testing", "task_id": task.id})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/remote-destinations/test/status/<tid>")
def api_test_remote_status(tid):
    capp, _, _, _, _, _, _ = get_celery()
    r = AsyncResult(tid, app=capp)
    res = {"state": r.state}
    if r.state == "SUCCESS":
        res["result"] = r.result
    elif r.state == "FAILURE":
        res["error"] = str(r.result)
    return jsonify(res)


# Sites
@app.route("/api/sites", methods=["GET"])
def api_get_sites():
    return jsonify(load_config().get("sites", []))


@app.route("/api/sites", methods=["POST"])
def api_add_site():
    d = request.json
    if not d.get("name", "").strip():
        return jsonify({"error": "Name required"}), 400
    add_site(d["name"].strip(), d.get("path", "").strip(), d.get("enabled", True))
    return jsonify({"status": "added"})


@app.route("/api/sites/<int:i>", methods=["DELETE"])
def api_del_site(i):
    return jsonify({"status": "deleted"}) if remove_site(i) else (jsonify({"error": "Invalid"}), 400)


@app.route("/api/sites/<int:i>/toggle", methods=["POST"])
def api_tog_site(i):
    return jsonify({"status": "toggled"}) if toggle_site(i) else (jsonify({"error": "Invalid"}), 400)


# Directory browser
@app.route("/api/browse")
def api_browse():
    path = request.args.get("path", "/backup/sharepoint")
    allowed_roots = ["/backup", "/tmp", "/data", "/mnt"]
    abs_path = os.path.abspath(path)
    if not any(abs_path.startswith(r) for r in allowed_roots):
        return jsonify({"error": "Path not allowed"}), 403
    if not os.path.exists(abs_path):
        return jsonify({"error": "Path not found"}), 404
    try:
        items = []
        for item in sorted(os.listdir(abs_path)):
            full = os.path.join(abs_path, item)
            if os.path.isdir(full) and not item.startswith("."):
                items.append({"name": item, "path": full, "is_dir": True})
        return jsonify({"path": abs_path, "items": items,
                       "parent": os.path.dirname(abs_path) if abs_path != "/" else None})
    except PermissionError:
        return jsonify({"error": "Permission denied"}), 403


@app.route("/api/browse/mkdir", methods=["POST"])
def api_mkdir():
    data = request.json
    path = data.get("path", "").strip()
    allowed_roots = ["/backup", "/tmp", "/data", "/mnt"]
    abs_path = os.path.abspath(path)
    if not any(abs_path.startswith(r) for r in allowed_roots):
        return jsonify({"error": "Path not allowed"}), 403
    try:
        os.makedirs(abs_path, exist_ok=True)
        return jsonify({"status": "created", "path": abs_path})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# Backup
@app.route("/api/backup/start", methods=["POST"])
def api_start_backup():
    data = request.json or {}
    active_backup = get_active_backup_guard()
    if active_backup:
        queue_item = OperationQueue().enqueue(
            "backup",
            "legacy_backup",
            {"custom_root": data.get("custom_root")},
            "Legacy SharePoint Backup",
            f"Queued while task {active_backup['task_id'][:8]} is still running.",
        )
        return jsonify({
            "status": "queued",
            "queue_item": queue_item,
            "active_backup": active_backup,
            "message": "Another legacy SharePoint backup is still running. This request has been queued.",
        }), 202
    _, run_backup, _, _, _, _, _ = get_celery()
    task = run_backup.delay(custom_root=data.get("custom_root"))
    log.info(f"Backup triggered: {task.id[:8]}")
    if _restore_engine_cache:
        _restore_engine_cache.invalidate_cache()
    return jsonify({"status": "started", "task_id": task.id})


@app.route("/api/backup/status/<tid>")
def api_backup_status(tid):
    capp, _, _, _, _, _, _ = get_celery()
    r = AsyncResult(tid, app=capp)
    try:
        state = r.state
        info = r.info
    except Exception as e:
        log.warning(f"Failed to decode task metadata for {tid[:8]}: {e}")
        return jsonify({
            "task_id": tid,
            "state": "UNKNOWN",
            "error": f"Task metadata could not be decoded: {e}",
        })

    res = {"task_id": tid, "state": state}
    if state == "PENDING":
        control_state = TaskController.get_state(tid)
        worker_active = _is_worker_task_active(tid)
        if control_state in {"running", "paused", "cancelled"} and worker_active is not False:
            res["state"] = "PROGRESS"
            res["control_state"] = control_state
            res["meta"] = info if isinstance(info, dict) else {}
            if worker_active is None:
                res["tracking_warning"] = "Worker activity could not be confirmed, but task state is still being preserved."
            return jsonify(res)
        if control_state in {"running", "paused", "cancelled"}:
            res["state"] = "UNKNOWN"
            res["control_state"] = "stale"
            res["error"] = "Tracked backup task is no longer active. Please start a new backup."
            try:
                if _redis.get("spo:current_backup_task") == tid:
                    _redis.delete("spo:current_backup_task")
            except redis_lib.RedisError:
                pass
            TaskController.cleanup(tid)
        return jsonify(res)
    if state in {"PROGRESS", "STARTED"}:
        worker_active = _is_worker_task_active(tid)
        if worker_active is False:
            res["state"] = "UNKNOWN"
            res["control_state"] = "stale"
            res["error"] = "Tracked backup task is no longer running on the worker."
            try:
                if _redis.get("spo:current_backup_task") == tid:
                    _redis.delete("spo:current_backup_task")
            except redis_lib.RedisError:
                pass
            TaskController.cleanup(tid)
            return jsonify(res)
        res["state"] = "PROGRESS"
        res["meta"] = info if isinstance(info, dict) else {}
        res["control_state"] = TaskController.get_state(tid)
        if worker_active is None:
            res["tracking_warning"] = "Worker activity could not be confirmed, using task backend progress."
    elif state == "BACKUP_FAILED":
        res["meta"] = info
        res["control_state"] = "failed"
        res["error"] = "Backup finished with fatal workload errors"
    elif state == "SUCCESS":
        res["result"] = r.result
        if isinstance(r.result, dict):
            if r.result.get("cancelled"):
                res["control_state"] = "cancelled"
            else:
                res["control_state"] = "completed"
    elif state == "FAILURE":
        res["control_state"] = "failed"
        res["error"] = str(r.result)
    elif state == "REVOKED":
        res["control_state"] = "cancelled"
        res["error"] = "Task was cancelled"
    return jsonify(res)


# ★ FAST backup list — fast version
@app.route("/api/backups", methods=["GET"])
def api_list_backups():
    return jsonify(fast_list_backups())


# ★★★ NEW: Lazy size computation for individual backup ★★★
@app.route("/api/backups/<name>/size")
def api_backup_size(name):
    """Compute size on-demand for a single backup (called when user expands row)."""
    config = load_config()
    backup_path = Path(config["backup"]["root_dir"]) / name
    if not backup_path.exists() or not backup_path.is_dir():
        return jsonify({"error": "Not found"}), 404
    
    # Try cache first
    size_file = backup_path / "_size_cache.json"
    if size_file.exists():
        try:
            data = json.load(open(size_file))
            return jsonify({"size_bytes": data.get("size_bytes", 0),
                           "size_human": _human_size(data.get("size_bytes", 0)),
                           "cached": True})
        except Exception:
            pass
    
    # Compute & cache
    total = 0
    try:
        for entry in os.scandir(backup_path):
            if entry.is_file():
                total += entry.stat().st_size
            elif entry.is_dir():
                for root, dirs, files in os.walk(entry.path):
                    for f in files:
                        try:
                            total += os.path.getsize(os.path.join(root, f))
                        except OSError:
                            pass
        # Save cache
        try:
            with open(size_file, "w") as f:
                json.dump({"size_bytes": total, "computed_at": datetime.now().isoformat()}, f)
        except Exception:
            pass
        return jsonify({"size_bytes": total, "size_human": _human_size(total), "cached": False})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/backups/<name>", methods=["GET"])
def api_backup_contents(name):
    return jsonify(get_restore_engine().list_backup_contents(name))


# ═══════════════════════════════════════════════════════════════
# ★ FIX #4: Robust DELETE backup with proper error handling ★
# ═══════════════════════════════════════════════════════════════
@app.route("/api/backups/<name>", methods=["DELETE"])
def api_delete_backup(name):
    """Delete backup with PROPER error handling + logging."""
    config = load_config()
    # Validate name format (security)
    if not (name.startswith("backup_") or name.startswith("custom_")):
        log.error(f"Delete rejected — invalid name: {name}")
        return jsonify({"error": "Invalid backup name (must start with 'backup_' or 'custom_')"}), 400
    
    backup_path = Path(config["backup"]["root_dir"]) / name
    
    if not backup_path.exists():
        log.warning(f"Delete failed — not found: {backup_path}")
        return jsonify({"error": f"Backup not found: {name}"}), 404
    
    if not backup_path.is_dir():
        log.error(f"Delete failed — not a directory: {backup_path}")
        return jsonify({"error": "Not a directory"}), 400
    
    # Calculate size before delete (for log)
    try:
        size = sum(f.stat().st_size for f in backup_path.rglob("*") if f.is_file())
        size_mb = size / 1024 / 1024
    except Exception:
        size_mb = 0
    
    # Actually delete
    try:
        log.info(f"Deleting backup: {name} ({size_mb:.2f} MB)")
        shutil.rmtree(backup_path)
        log.info(f"✅ Backup deleted: {name}")
        
        # Invalidate cache
        if _restore_engine_cache:
            _restore_engine_cache.invalidate_cache()
        
        return jsonify({"status": "deleted", "backup": name, "freed_mb": round(size_mb, 2)})
    except PermissionError as e:
        log.error(f"Delete failed — permission denied: {e}")
        return jsonify({"error": f"Permission denied: {e}"}), 500
    except OSError as e:
        log.error(f"Delete failed — OS error: {e}")
        return jsonify({"error": f"OS error: {e}"}), 500
    except Exception as e:
        log.error(f"Delete failed — unknown: {e}", exc_info=True)
        return jsonify({"error": f"Unknown error: {e}"}), 500


# ★★★ NEW: Bulk delete empty backups (0 bytes) ★★★
@app.route("/api/backups/cleanup-empty", methods=["POST"])
def api_cleanup_empty():
    """Delete all empty/failed backup folders (0 MB)."""
    config = load_config()
    root = Path(config["backup"]["root_dir"])
    deleted = []
    errors = []
    
    for d in root.iterdir():
        if d.is_dir() and (d.name.startswith("backup_") or d.name.startswith("custom_")):
            try:
                # Check size
                total = 0
                for entry in os.scandir(d):
                    if entry.is_file():
                        total += entry.stat().st_size
                    elif entry.is_dir():
                        for root_, dirs, files in os.walk(entry.path):
                            for f in files:
                                if not f.startswith("_"):
                                    try:
                                        total += os.path.getsize(os.path.join(root_, f))
                                    except OSError:
                                        pass
                if total == 0:
                    shutil.rmtree(d)
                    deleted.append(d.name)
                    log.info(f"Cleaned empty backup: {d.name}")
            except Exception as e:
                errors.append({"backup": d.name, "error": str(e)})
    
    if _restore_engine_cache:
        _restore_engine_cache.invalidate_cache()
    
    return jsonify({"status": "done", "deleted": deleted, "count": len(deleted), "errors": errors})


# Custom URL download
@app.route("/api/download/url", methods=["POST"])
def api_download_custom_url():
    data = request.json or {}
    url = data.get("url", "").strip()
    dest_dir = data.get("dest_dir", "").strip() or None
    if not url:
        return jsonify({"error": "URL required"}), 400
    if "sharepoint.com" not in url:
        return jsonify({"error": "Not a valid SharePoint URL"}), 400
    active_download = get_active_download_guard()
    if active_download:
        queue_item = OperationQueue().enqueue(
            "download",
            "download_custom_url",
            {"url": url, "dest_dir": dest_dir},
            "Custom SharePoint Download",
            dest_dir or url,
        )
        return jsonify({
            "status": "queued",
            "queue_item": queue_item,
            "active_download": active_download,
            "message": "Another custom download is still running. This download has been queued.",
        }), 202
    _, _, _, _, download_task, _, _ = get_celery()
    task = download_task.delay(url, dest_dir=dest_dir)
    return jsonify({"status": "started", "task_id": task.id})


@app.route("/api/queue/<item_id>", methods=["DELETE"])
def api_delete_queue_item(item_id):
    item = OperationQueue().get(item_id)
    if not item:
        return jsonify({"error": "Queue item not found"}), 404
    if item.get("group") == "restore":
        job_id = (item.get("payload") or {}).get("job_id")
        if job_id:
            try:
                from app.restore_manager_v2 import RestoreManagerV2

                RestoreManagerV2().update_job(job_id, {"status": "cancelled"})
            except Exception:
                pass
    OperationQueue().remove(item_id, group=item.get("group"))
    return jsonify({"status": "deleted", "item_id": item_id})


@app.route("/api/download/parse", methods=["POST"])
def api_parse_url():
    data = request.json or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400
    try:
        return jsonify({"status": "ok", "parsed": BackupEngine(load_config()).parse_sharepoint_url(url)})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/download/status/<tid>")
def api_download_status(tid):
    capp, _, _, _, _, _, _ = get_celery()
    r = AsyncResult(tid, app=capp)
    try:
        state = r.state
        info = r.info
    except Exception as e:
        log.warning(f"Failed to decode download task metadata for {tid[:8]}: {e}")
        return jsonify({
            "task_id": tid,
            "state": "UNKNOWN",
            "error": f"Task metadata could not be decoded: {e}",
        })

    res = {"task_id": tid, "state": state}
    if state == "PENDING":
        control_state = TaskController.get_state(tid)
        worker_active = _is_worker_task_active(tid)
        if control_state in {"running", "paused", "cancelled"} and worker_active is not False:
            res["state"] = "PROGRESS"
            res["control_state"] = control_state
            res["meta"] = info if isinstance(info, dict) else {}
            if worker_active is None:
                res["tracking_warning"] = "Worker activity could not be confirmed, but task state is still being preserved."
            return jsonify(res)
        if control_state in {"running", "paused", "cancelled"}:
            res["state"] = "UNKNOWN"
            res["control_state"] = "stale"
            res["error"] = "Tracked download task is no longer active. Please restart the download."
            try:
                if _redis.get("spo:current_download_task") == tid:
                    _redis.delete("spo:current_download_task")
            except redis_lib.RedisError:
                pass
            TaskController.cleanup(tid)
        return jsonify(res)
    if state in {"PROGRESS", "STARTED"}:
        worker_active = _is_worker_task_active(tid)
        if worker_active is False:
            res["state"] = "UNKNOWN"
            res["control_state"] = "stale"
            res["error"] = "Tracked download task is no longer running on the worker."
            try:
                if _redis.get("spo:current_download_task") == tid:
                    _redis.delete("spo:current_download_task")
            except redis_lib.RedisError:
                pass
            TaskController.cleanup(tid)
            return jsonify(res)
        res["state"] = "PROGRESS"
        res["meta"] = info if isinstance(info, dict) else {}
        res["control_state"] = TaskController.get_state(tid)
        if worker_active is None:
            res["tracking_warning"] = "Worker activity could not be confirmed, using task backend progress."
    elif state == "SUCCESS":
        res["result"] = r.result
        if isinstance(r.result, dict):
            if r.result.get("cancelled"):
                res["control_state"] = "cancelled"
            else:
                res["control_state"] = "completed"
    elif state == "FAILURE":
        res["control_state"] = "failed"
        res["error"] = str(r.result)
    elif state == "REVOKED":
        res["control_state"] = "cancelled"
        res["error"] = "Task was cancelled"
    return jsonify(res)


# ═══════════════════════════════════════════════════════════════
# ★ FIX #1: PROPER cancel (Redis + Celery revoke + cleanup) ★
# ═══════════════════════════════════════════════════════════════
@app.route("/api/task/<tid>/pause", methods=["POST"])
def api_pause_task(tid):
    TaskController.pause(tid)
    log.warning(f"Task {tid[:8]} paused by user")
    return jsonify({"status": "paused", "task_id": tid})


@app.route("/api/task/<tid>/resume", methods=["POST"])
def api_resume_task(tid):
    TaskController.resume(tid)
    log.info(f"Task {tid[:8]} resumed by user")
    return jsonify({"status": "resumed", "task_id": tid})


@app.route("/api/task/<tid>/cancel", methods=["POST"])
def api_cancel_task(tid):
    """★ ROBUST cancel — uses force_cancel_task which does:
       1. Set Redis flag
       2. Revoke Celery task (terminate)
       3. Clean up tracking keys"""
    _, _, _, _, _, _, force_cancel = get_celery()
    force_cancel(tid)
    log.warning(f"Task {tid[:8]} FORCE CANCELLED by user")
    return jsonify({"status": "cancelled", "task_id": tid, "method": "force_cancel"})


@app.route("/api/task/<tid>/control")
def api_task_control(tid):
    return jsonify({"task_id": tid, "state": TaskController.get_state(tid)})


# Restore
@app.route("/api/restore/site", methods=["POST"])
def api_restore():
    from app.main_routes import _build_legacy_restore_v2_payload, _legacy_restore_notice, tm
    from app.operation_dispatcher import dispatch_next_queued_operation
    from app.operation_queue import OperationQueue
    from app.restore_manager_v2 import RestoreManagerV2

    data = request.json or {}
    active = tm.get_active_tenant(include_secret=False)
    if not active:
        return jsonify({"error": "No active tenant", **_legacy_restore_notice()}), 400
    try:
        payload = _build_legacy_restore_v2_payload(data, active)
        mgr = RestoreManagerV2()
        if data.get("dry_run"):
            preview = mgr.dry_run(payload)
            return jsonify({
                "status": "preview",
                "preview": preview,
                **_legacy_restore_notice(),
            })

        job = mgr.create_job(payload)
        queue_item = OperationQueue().enqueue(
            "restore",
            "restore_v2",
            {"job_id": job["id"]},
            "Restore sharepoint",
            f"{active.get('name') or 'Unknown tenant'} · {payload['source_backup']}",
        )
        running_restore = next((item for item in mgr.list_jobs(limit=100) if item.get("status") == "running"), None)
        dispatched = None if running_restore else dispatch_next_queued_operation("restore")
        notice = _legacy_restore_notice()
        if dispatched and dispatched.get("task_id"):
            job = mgr.get_job(job["id"]) or job
            return jsonify({
                "status": "created",
                "task_id": job.get("task_id"),
                "job": job,
                **notice,
            }), 201
        return jsonify({
            "status": "queued",
            "job": job,
            "queue_item": queue_item,
            "message": "Another restore job is already running. This restore job has been queued." if running_restore else None,
            **notice,
        }), 202
    except ValueError as e:
        return jsonify({"error": str(e), **_legacy_restore_notice()}), 400
    except Exception as e:
        log.error(f"Legacy restore compatibility request failed: {e}")
        return jsonify({"error": str(e), **_legacy_restore_notice()}), 500


# Notification
@app.route("/api/notification/test", methods=["POST"])
def api_test_notif():
    data = request.json or {}
    try:
        _validate_notification_config(load_config(), data.get("channel"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    _, _, _, test_task, _, _, _ = get_celery()
    task = test_task.delay(data.get("channel"))
    return jsonify({"status": "test_sent", "task_id": task.id})


@app.route("/api/notification/test/status/<tid>")
def api_test_status(tid):
    capp, _, _, _, _, _, _ = get_celery()
    r = AsyncResult(tid, app=capp)
    res = {"state": r.state}
    if r.state == "SUCCESS":
        res["results"] = r.result
    return jsonify(res)


# Logs
@app.route("/api/logs")
def api_logs():
    return jsonify({"logs": _read_logs(int(request.args.get("lines", 100)))})


# Clear logs
@app.route("/api/logs/clear", methods=["POST"])
def api_clear_logs():
    """Clear log file."""
    try:
        with open(LOG_FILE, "w") as f:
            f.write("")
        log.info("Logs cleared by user")
        return jsonify({"status": "cleared"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
