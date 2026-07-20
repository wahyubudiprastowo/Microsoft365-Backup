"""Safe integration routes for multi-tenant and workload features."""
import logging
from datetime import datetime
from pathlib import Path

from celery.result import AsyncResult
from flask import Blueprint, jsonify, render_template, request, redirect, url_for

from app.config_manager import load_config
from app.restore_manager import RestoreManager
from app.tenant_manager import REQUIRED_SCOPES, TenantManager
from app.workloads import WORKLOAD_META, get_workload
from app.backup_registry import slugify_tenant

log = logging.getLogger("spo_backup")

m365_bp = Blueprint("m365", __name__)
tm = TenantManager()
rm = RestoreManager()


def _with_tenant_slug(tenant: dict | None):
    if not tenant:
        return tenant
    enriched = dict(tenant)
    enriched["tenant_slug"] = slugify_tenant(
        tenant.get("primary_domain")
        or tenant.get("sharepoint_host")
        or tenant.get("name")
    )
    return enriched


def _classify_workload_error(raw_error: str) -> dict:
    text = str(raw_error or "")
    lower = text.lower()
    if "403" in text or "forbidden" in lower:
        return {
            "error_type": "permission_denied",
            "message": "Tenant app permissions or admin consent are not sufficient for this workload.",
            "status_code": 403,
        }
    if "auth failed" in lower or "unauthorized" in lower or "401" in text:
        return {
            "error_type": "auth_failed",
            "message": "Authentication with Microsoft Graph failed for the active tenant.",
            "status_code": 401,
        }
    return {
        "error_type": "discovery_failed",
        "message": "Target discovery failed for this workload.",
        "status_code": 502,
    }


def _list_backups():
    root = Path(load_config()["backup"]["root_dir"])
    backups = []
    if not root.exists():
        return backups
    for entry in sorted(root.iterdir(), reverse=True):
        if entry.is_dir() and (entry.name.startswith("backup_") or entry.name.startswith("custom_")):
            backups.append({"name": entry.name, "date": datetime.fromtimestamp(entry.stat().st_mtime).strftime("%Y-%m-%d %H:%M")})
    return backups


@m365_bp.route("/tenants")
def tenants_page():
    return render_template(
        "tenants.html",
        tenants=[_with_tenant_slug(t) for t in tm.list_tenants()],
        active_tenant=_with_tenant_slug(tm.get_active_tenant(include_secret=False)),
        required_scopes=REQUIRED_SCOPES,
    )


@m365_bp.route("/workloads")
def workloads_page():
    return render_template("workloads.html", active_tenant=_with_tenant_slug(tm.get_active_tenant(include_secret=False)), workload_meta=WORKLOAD_META)


@m365_bp.route("/restore-jobs")
def restore_jobs_page():
    return redirect(url_for("restore_v2_page"))


@m365_bp.route("/api/tenants", methods=["GET"])
def api_list_tenants():
    active = _with_tenant_slug(tm.get_active_tenant(include_secret=False))
    return jsonify({
        "tenants": [_with_tenant_slug(t) for t in tm.list_tenants()],
        "active_id": active["id"] if active else None,
        "required_scopes": REQUIRED_SCOPES,
    })


@m365_bp.route("/api/tenants", methods=["POST"])
def api_add_tenant():
    try:
        tenant = tm.add_tenant(request.json or {})
        return jsonify({"status": "added", "tenant": _with_tenant_slug(tenant)})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        log.error(f"Add tenant failed: {e}")
        return jsonify({"error": str(e)}), 500


@m365_bp.route("/api/tenants/<tid>", methods=["PUT"])
def api_update_tenant(tid):
    tenant = tm.update_tenant(tid, request.json or {})
    if not tenant:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"status": "updated", "tenant": _with_tenant_slug(tenant)})


@m365_bp.route("/api/tenants/<tid>", methods=["DELETE"])
def api_delete_tenant(tid):
    if not tm.delete_tenant(tid):
        return jsonify({"error": "Not found"}), 404
    return jsonify({"status": "deleted"})


@m365_bp.route("/api/tenants/<tid>/activate", methods=["POST"])
def api_activate_tenant(tid):
    if not tm.set_active_tenant(tid):
        return jsonify({"error": "Not found"}), 404
    return jsonify({
        "status": "active",
        "tenant_id": tid,
        "tenant": _with_tenant_slug(tm.get_tenant(tid, include_secret=False)),
    })


@m365_bp.route("/api/tenants/<tid>/test", methods=["POST"])
def api_test_tenant(tid):
    return jsonify(tm.test_tenant(tenant_id=tid))


@m365_bp.route("/api/tenants/test-config", methods=["POST"])
def api_test_tenant_config():
    return jsonify(tm.test_tenant(tenant_data=request.json or {}))


@m365_bp.route("/api/tenants/active", methods=["GET"])
def api_active_tenant():
    return jsonify(_with_tenant_slug(tm.get_active_tenant(include_secret=False)) or {})


@m365_bp.route("/api/workloads")
def api_workloads():
    return jsonify({"workloads": WORKLOAD_META, "active_tenant": _with_tenant_slug(tm.get_active_tenant(include_secret=False))})


@m365_bp.route("/api/workloads/<wtype>/targets")
def api_workload_targets(wtype):
    active = tm.get_active_tenant(include_secret=True)
    if not active:
        return jsonify({"error": "No active tenant"}), 400
    try:
        targets = get_workload(wtype, active).list_targets()
        if targets and isinstance(targets, list) and targets[0].get("error"):
            err = _classify_workload_error(targets[0]["error"])
            return jsonify({
                "targets": [],
                "error": err["message"],
                "error_type": err["error_type"],
                "error_detail": targets[0]["error"],
                "required_scopes": WORKLOAD_META.get(wtype, {}).get("required_scopes", []),
                "workload": wtype,
            }), err["status_code"]
        return jsonify({"targets": targets, "workload": wtype})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@m365_bp.route("/api/workloads/<wtype>/toggle", methods=["POST"])
def api_toggle_workload(wtype):
    active = tm.get_active_tenant(include_secret=True)
    if not active:
        return jsonify({"error": "No active tenant"}), 400
    enabled = list(active.get("workloads_enabled", []))
    if wtype in enabled:
        enabled.remove(wtype)
    else:
        enabled.append(wtype)
    tm.update_tenant(active["id"], {"workloads_enabled": enabled})
    return jsonify({"status": "toggled", "enabled": enabled})


@m365_bp.route("/api/restore/jobs", methods=["GET"])
def api_list_restore_jobs():
    return jsonify({"jobs": rm.list_jobs(limit=50)})


@m365_bp.route("/api/restore/jobs", methods=["POST"])
def api_create_restore_job():
    active = tm.get_active_tenant(include_secret=False)
    if not active:
        return jsonify({"error": "No active tenant"}), 400
    data = request.json or {}
    source_backup = data.get("source_backup", "").strip()
    source_site = data.get("source_site", "").strip()
    target_site = data.get("target_site", "").strip()
    if not source_backup or not source_site or not target_site:
        return jsonify({"error": "source_backup, source_site, and target_site are required"}), 400
    job = rm.create_job(
        tenant_id=active["id"],
        tenant_name=active["name"],
        workload=data.get("workload", "sharepoint"),
        source_backup=source_backup,
        source_site=source_site,
        target_site=target_site,
        target_location=data.get("target_location", "").strip(),
        mode=data.get("mode", "merge"),
    )
    try:
        from app.tasks_m365 import execute_restore_job
        task = execute_restore_job.delay(job.id)
        rm.update_job(job.id, task_id=task.id, status="queued")
    except Exception as e:
        log.error(f"Failed to queue restore job: {e}")
        rm.update_job(job.id, status="failed", errors=[str(e)])
        return jsonify({"error": str(e)}), 500
    return jsonify({"status": "created", "job_id": job.id})


@m365_bp.route("/api/restore/jobs/<job_id>", methods=["GET"])
def api_get_restore_job(job_id):
    job = rm.get_job(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    if job.get("task_id"):
        try:
            from app.tasks import celery_app
            result = AsyncResult(job["task_id"], app=celery_app)
            job["task_state"] = result.state
        except Exception:
            pass
    return jsonify(job)


@m365_bp.route("/api/restore/jobs/<job_id>", methods=["DELETE"])
def api_delete_restore_job(job_id):
    if not rm.delete_job(job_id):
        return jsonify({"error": "Not found"}), 404
    return jsonify({"status": "deleted"})


def register_m365_routes(app):
    if "m365" not in app.blueprints:
        app.register_blueprint(m365_bp)
