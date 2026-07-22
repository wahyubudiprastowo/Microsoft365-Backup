"""Safe integration routes for multi-tenant and workload features."""
import json
import logging
from datetime import datetime
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request, redirect, url_for

from app.config_manager import load_config
from app.operation_dispatcher import dispatch_next_queued_operation
from app.operation_queue import OperationQueue
from app.restore_manager_v2 import RestoreManagerV2
from app.tenant_manager import REQUIRED_SCOPES, TenantManager
from app.workloads import WORKLOAD_META, get_workload
from app.backup_registry import slugify_tenant

log = logging.getLogger("spo_backup")

m365_bp = Blueprint("m365", __name__)
tm = TenantManager()
restore_v2_mgr = RestoreManagerV2()


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


def _normalize_target_selection(payload: dict | None) -> dict:
    payload = payload or {}
    mode = str(payload.get("mode") or "all").strip().lower()
    if mode not in {"all", "selected"}:
        mode = "all"
    selected_ids = []
    for item in payload.get("selected_ids", []) or []:
        value = str(item or "").strip()
        if value:
            selected_ids.append(value)
    if mode == "selected":
        deduped = []
        seen = set()
        for value in selected_ids:
            if value in seen:
                continue
            seen.add(value)
            deduped.append(value)
        selected_ids = deduped
    else:
        selected_ids = []
    return {
        "mode": mode,
        "selected_ids": selected_ids,
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


def _legacy_restore_notice() -> dict:
    return {
        "deprecated": True,
        "compatibility_mode": "legacy_restore_api",
        "recommended_ui": "/restore",
        "recommended_endpoint": "/api/v2/restore/jobs",
        "message": "Legacy restore API is now routed through the main Restore flow in compatibility mode.",
    }


def _resolve_legacy_site_backup_path(source_backup: str, source_site: str) -> Path:
    backup_root = Path(load_config()["backup"]["root_dir"]) / source_backup
    if not backup_root.exists() or not backup_root.is_dir():
        raise ValueError(f"Backup not found: {source_backup}")

    direct = backup_root / source_site.replace(" ", "_")
    if direct.exists() and direct.is_dir():
        return direct.resolve()

    normalized = source_site.strip().lower()
    for site_dir in backup_root.iterdir():
        if not site_dir.is_dir() or site_dir.name.startswith("."):
            continue
        if site_dir.name.lower() == normalized:
            return site_dir.resolve()
        meta_file = site_dir / "_backup_metadata.json"
        if not meta_file.exists():
            continue
        try:
            meta = json.load(open(meta_file))
        except Exception:
            continue
        display_name = str(meta.get("site_name") or "").strip().lower()
        if display_name == normalized:
            return site_dir.resolve()

    raise ValueError(f"Source site not found in backup '{source_backup}': {source_site}")


def _build_legacy_restore_v2_payload(data: dict, active_tenant: dict) -> dict:
    source_backup = str(data.get("source_backup") or data.get("backup_name") or "").strip()
    source_site = str(data.get("source_site") or data.get("site_name") or "").strip()
    if not source_backup or not source_site:
        raise ValueError("source_backup and source_site are required")

    site_backup_path = _resolve_legacy_site_backup_path(source_backup, source_site)
    target_site_path = str(
        data.get("target_site")
        or data.get("target_site_path")
        or data.get("target_location")
        or ""
    ).strip()

    if not target_site_path:
        meta_file = site_backup_path / "_backup_metadata.json"
        if meta_file.exists():
            try:
                meta = json.load(open(meta_file))
                target_site_path = str(meta.get("site_path") or "").strip()
            except Exception:
                target_site_path = ""

    if not target_site_path:
        raise ValueError("target_site or target_site_path is required for legacy restore compatibility mode")

    return {
        "tenant_id": active_tenant["id"],
        "tenant_name": active_tenant.get("name", ""),
        "workload": "sharepoint",
        "backup_path": str(site_backup_path),
        "source_backup": source_backup,
        "mode": str(data.get("mode") or "merge").strip() or "merge",
        "target_site_path": target_site_path,
        "target_library_name": str(data.get("target_library_name") or "").strip() or None,
        "target_folder_path": str(data.get("target_folder_path") or "").strip() or None,
    }


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
    meta = WORKLOAD_META.get(wtype)
    if not meta:
        return jsonify({"error": "Unknown workload"}), 404
    try:
        workload = get_workload(wtype, active)
        selection = workload.get_target_selection()
        targets = workload.list_targets()
        if targets and isinstance(targets, list) and targets[0].get("error"):
            err = _classify_workload_error(targets[0]["error"])
            return jsonify({
                "targets": [],
                "error": err["message"],
                "error_type": err["error_type"],
                "error_detail": targets[0]["error"],
                "required_scopes": meta.get("required_scopes", []),
                "workload": wtype,
                "selection": selection,
                "supports_target_selection": bool(meta.get("supports_target_selection")),
            }), err["status_code"]
        return jsonify({
            "targets": targets,
            "workload": wtype,
            "selection": selection,
            "supports_target_selection": bool(meta.get("supports_target_selection")),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@m365_bp.route("/api/workloads/<wtype>/toggle", methods=["POST"])
def api_toggle_workload(wtype):
    active = tm.get_active_tenant(include_secret=True)
    if not active:
        return jsonify({"error": "No active tenant"}), 400
    if wtype not in WORKLOAD_META:
        return jsonify({"error": "Unknown workload"}), 404
    enabled = list(active.get("workloads_enabled", []))
    if wtype in enabled:
        enabled.remove(wtype)
    else:
        enabled.append(wtype)
    tm.update_tenant(active["id"], {"workloads_enabled": enabled})
    return jsonify({"status": "toggled", "enabled": enabled})


@m365_bp.route("/api/workloads/<wtype>/selection", methods=["POST"])
def api_save_workload_selection(wtype):
    active = tm.get_active_tenant(include_secret=True)
    if not active:
        return jsonify({"error": "No active tenant"}), 400
    meta = WORKLOAD_META.get(wtype)
    if not meta:
        return jsonify({"error": "Unknown workload"}), 404
    if not meta.get("supports_target_selection"):
        return jsonify({
            "error": "This workload uses a different scope control surface.",
            "manage_href": meta.get("manage_href"),
            "manage_label": meta.get("manage_label"),
        }), 400

    selection = _normalize_target_selection(request.get_json(force=True, silent=True) or {})
    if selection["mode"] == "selected" and not selection["selected_ids"]:
        return jsonify({"error": "Select at least one target or switch the mode back to all targets."}), 400

    selection_map = dict(active.get("workload_target_selection", {}) or {})
    selection_map[wtype] = selection
    tm.update_tenant(active["id"], {"workload_target_selection": selection_map})

    return jsonify({
        "status": "saved",
        "workload": wtype,
        "selection": selection,
        "tenant_id": active.get("id"),
    })


@m365_bp.route("/api/restore/jobs", methods=["GET"])
def api_list_restore_jobs():
    notice = _legacy_restore_notice()
    return jsonify({
        "jobs": restore_v2_mgr.list_jobs(limit=int(request.args.get("limit", 50))),
        **notice,
    })


@m365_bp.route("/api/restore/jobs", methods=["POST"])
def api_create_restore_job():
    active = tm.get_active_tenant(include_secret=False)
    if not active:
        return jsonify({"error": "No active tenant"}), 400
    try:
        payload = _build_legacy_restore_v2_payload(request.json or {}, active)
        job = restore_v2_mgr.create_job(payload)
        queue_item = OperationQueue().enqueue(
            "restore",
            "restore_v2",
            {"job_id": job["id"]},
            "Restore sharepoint",
            f"{active.get('name') or 'Unknown tenant'} · {payload['source_backup']}",
        )
        running_restore = next(
            (item for item in restore_v2_mgr.list_jobs(limit=100) if item.get("status") == "running"),
            None,
        )
        dispatched = None if running_restore else dispatch_next_queued_operation("restore")
        notice = _legacy_restore_notice()
        if dispatched and dispatched.get("task_id"):
            job = restore_v2_mgr.get_job(job["id"]) or job
            return jsonify({
                "status": "created",
                "job": job,
                **notice,
            }), 201
        return jsonify({
            "status": "queued",
            "job": job,
            "queue_item": queue_item,
            **notice,
        }), 202
    except ValueError as e:
        return jsonify({"error": str(e), **_legacy_restore_notice()}), 400
    except Exception as e:
        log.error(f"Failed to queue restore job: {e}")
        return jsonify({"error": str(e), **_legacy_restore_notice()}), 500


@m365_bp.route("/api/restore/jobs/<job_id>", methods=["GET"])
def api_get_restore_job(job_id):
    job = restore_v2_mgr.get_job(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    return jsonify({
        **job,
        **_legacy_restore_notice(),
    })


@m365_bp.route("/api/restore/jobs/<job_id>", methods=["DELETE"])
def api_delete_restore_job(job_id):
    if not restore_v2_mgr.delete_job(job_id):
        return jsonify({
            "error": "Cannot delete active job",
            **_legacy_restore_notice(),
        }), 409
    return jsonify({"status": "deleted", **_legacy_restore_notice()})


def register_m365_routes(app):
    if "m365" not in app.blueprints:
        app.register_blueprint(m365_bp)
