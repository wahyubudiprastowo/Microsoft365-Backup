"""SPO Backup v4.0 — Flask with pause/resume + custom dest endpoints."""
import os
import json
import logging
from datetime import datetime, timezone

from flask import Flask, render_template, request, jsonify
from celery.result import AsyncResult

from app.config_manager import load_config, save_config, add_site, remove_site, toggle_site
from app.backup_engine import RestoreEngine, BackupEngine
from app.task_control import TaskController  # ★ NEW

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-key")
log = logging.getLogger("spo_backup")
logging.basicConfig(level=logging.INFO)


def get_celery():
    from app.tasks import (celery_app, run_backup_task, run_restore_task,
                           send_test_notification, download_custom_url_task)
    return celery_app, run_backup_task, run_restore_task, send_test_notification, download_custom_url_task


def _read_logs(n):
    p = "/app/logs/spo_backup.log"
    if os.path.exists(p):
        with open(p) as f:
            return f.readlines()[-n:]
    return []


# ── PAGES ─────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    config = load_config()
    try: backups = RestoreEngine(config).list_backups()
    except: backups = []
    return render_template("dashboard.html", config=config, backups=backups, logs=_read_logs(30))


@app.route("/sites")
def sites_page():
    return render_template("sites.html", config=load_config())


@app.route("/download")
def download_page():
    return render_template("download.html", config=load_config())


@app.route("/backups")
def backups_page():
    config = load_config()
    try: backups = RestoreEngine(config).list_backups()
    except: backups = []
    return render_template("backups.html", config=config, backups=backups)


@app.route("/restore")
def restore_page():
    config = load_config()
    try: backups = RestoreEngine(config).list_backups()
    except: backups = []
    return render_template("restore.html", config=config, backups=backups)


@app.route("/settings")
def settings_page():
    return render_template("settings.html", config=load_config())


@app.route("/logs")
def logs_page():
    return render_template("logs.html", logs=_read_logs(200))


# ── API ───────────────────────────────────────────────────────

@app.route("/api/health")
def api_health():
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat()})


# Config
@app.route("/api/config", methods=["GET"])
def api_get_config():
    c = load_config()
    safe = json.loads(json.dumps(c))
    if safe.get("azure_ad", {}).get("client_secret"):
        safe["azure_ad"]["client_secret"] = "***MASKED***"
    if safe.get("notification", {}).get("smtp", {}).get("password"):
        safe["notification"]["smtp"]["password"] = "***MASKED***"
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


# ★★★ NEW: Browse server directories (for picker UI) ★★★
@app.route("/api/browse")
def api_browse():
    """List directories on the server for path picker."""
    path = request.args.get("path", "/backup/sharepoint")
    # Security: restrict to /backup, /volume, /tmp
    allowed_roots = ["/backup", "/volume1", "/volume2", "/volume3", "/tmp", "/data"]
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
                items.append({
                    "name": item,
                    "path": full,
                    "is_dir": True,
                })
        return jsonify({"path": abs_path, "items": items,
                       "parent": os.path.dirname(abs_path) if abs_path != "/" else None})
    except PermissionError:
        return jsonify({"error": "Permission denied"}), 403


@app.route("/api/browse/mkdir", methods=["POST"])
def api_mkdir():
    """Create a new directory."""
    data = request.json
    path = data.get("path", "").strip()
    allowed_roots = ["/backup", "/volume1", "/volume2", "/volume3", "/tmp", "/data"]
    abs_path = os.path.abspath(path)
    if not any(abs_path.startswith(r) for r in allowed_roots):
        return jsonify({"error": "Path not allowed"}), 403
    try:
        os.makedirs(abs_path, exist_ok=True)
        return jsonify({"status": "created", "path": abs_path})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# Backup (with custom_root)
@app.route("/api/backup/start", methods=["POST"])
def api_start_backup():
    data = request.json or {}
    custom_root = data.get("custom_root")  # ★ NEW
    _, run_backup, _, _, _ = get_celery()
    task = run_backup.delay(custom_root=custom_root)
    return jsonify({"status": "started", "task_id": task.id})


@app.route("/api/backup/status/<tid>")
def api_backup_status(tid):
    capp, _, _, _, _ = get_celery()
    r = AsyncResult(tid, app=capp)
    res = {"task_id": tid, "state": r.state}
    if r.state == "PROGRESS":
        res["meta"] = r.info
        res["control_state"] = TaskController.get_state(tid)  # ★ NEW
    elif r.state == "SUCCESS":
        res["result"] = r.result
    elif r.state == "FAILURE":
        res["error"] = str(r.result)
    return jsonify(res)


@app.route("/api/backups", methods=["GET"])
def api_list_backups():
    return jsonify(RestoreEngine(load_config()).list_backups())


@app.route("/api/backups/<name>", methods=["GET"])
def api_backup_contents(name):
    return jsonify(RestoreEngine(load_config()).list_backup_contents(name))


@app.route("/api/backups/<name>", methods=["DELETE"])
def api_delete_backup(name):
    return jsonify(RestoreEngine(load_config()).delete_backup(name))


# ★★★ Custom URL Download (with dest_dir) ★★★
@app.route("/api/download/url", methods=["POST"])
def api_download_custom_url():
    data = request.json or {}
    url = data.get("url", "").strip()
    dest_dir = data.get("dest_dir", "").strip() or None  # ★ NEW
    if not url:
        return jsonify({"error": "URL required"}), 400
    if "sharepoint.com" not in url:
        return jsonify({"error": "Not a valid SharePoint URL"}), 400
    _, _, _, _, download_task = get_celery()
    task = download_task.delay(url, dest_dir=dest_dir)
    return jsonify({"status": "started", "task_id": task.id, "url": url, "dest_dir": dest_dir})


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
    capp, _, _, _, _ = get_celery()
    r = AsyncResult(tid, app=capp)
    res = {"task_id": tid, "state": r.state}
    if r.state == "PROGRESS":
        res["meta"] = r.info
        res["control_state"] = TaskController.get_state(tid)  # ★ NEW
    elif r.state == "SUCCESS":
        res["result"] = r.result
    elif r.state == "FAILURE":
        res["error"] = str(r.result)
    return jsonify(res)


# ★★★ NEW: Task Control Endpoints ★★★
@app.route("/api/task/<tid>/pause", methods=["POST"])
def api_pause_task(tid):
    """Pause a running task."""
    TaskController.pause(tid)
    return jsonify({"status": "paused", "task_id": tid})


@app.route("/api/task/<tid>/resume", methods=["POST"])
def api_resume_task(tid):
    """Resume a paused task."""
    TaskController.resume(tid)
    return jsonify({"status": "resumed", "task_id": tid})


@app.route("/api/task/<tid>/cancel", methods=["POST"])
def api_cancel_task(tid):
    """Cancel a running/paused task."""
    TaskController.cancel(tid)
    return jsonify({"status": "cancelled", "task_id": tid})


@app.route("/api/task/<tid>/control")
def api_task_control(tid):
    """Get current control state."""
    return jsonify({"task_id": tid, "state": TaskController.get_state(tid)})


# Restore
@app.route("/api/restore/site", methods=["POST"])
def api_restore():
    data = request.json
    _, _, restore_task, _, _ = get_celery()
    task = restore_task.delay(
        data["backup_name"], data["site_name"],
        data.get("target_site_path"), data.get("dry_run", False),
    )
    return jsonify({"status": "started", "task_id": task.id})


# Notification
@app.route("/api/notification/test", methods=["POST"])
def api_test_notif():
    data = request.json or {}
    _, _, _, test_task, _ = get_celery()
    task = test_task.delay(data.get("channel"))
    return jsonify({"status": "test_sent", "task_id": task.id})


# Logs
@app.route("/api/logs")
def api_logs():
    return jsonify({"logs": _read_logs(int(request.args.get("lines", 100)))})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
