"""Safe integration routes for multi-tenant and workload features."""
import json
import logging
from datetime import datetime
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request, redirect, url_for

from app.config_manager import load_config, save_config, update_config
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


def _normalize_site_path(value: str) -> str:
    return str(value or "").strip().strip("/")


def _format_human_size(size_bytes):
    try:
        size = float(size_bytes or 0)
    except (TypeError, ValueError):
        size = 0.0
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    unit_idx = 0
    while size >= 1024 and unit_idx < len(units) - 1:
        size /= 1024
        unit_idx += 1
    return f"{int(size)} B" if unit_idx == 0 else f"{size:.1f} {units[unit_idx]}"


def _build_sharepoint_estimate_result(payload: dict, estimate: dict) -> tuple[dict, dict]:
    graph_id = str(payload.get("graph_id") or "").strip() or estimate.get("site_id")
    site_path = _normalize_site_path(payload.get("path", "") or estimate.get("path", ""))
    size_bytes = estimate.get("size_bytes")
    size_human = _format_human_size(size_bytes)
    cache_payload = {
        "size_bytes": size_bytes,
        "size_human": size_human,
        "confidence": estimate.get("confidence"),
        "updated_at": estimate.get("updated_at"),
        "drives_count": estimate.get("drives_count"),
        "drives_with_quota": estimate.get("drives_with_quota"),
        "size_method": estimate.get("size_method"),
        "raw_drives_used_sum_bytes": estimate.get("raw_drives_used_sum_bytes"),
        "site_id": estimate.get("site_id"),
        "site_url": estimate.get("site_url"),
    }
    result = {
        "status": "estimated",
        "target_id": str(payload.get("id") or "").strip(),
        "graph_id": graph_id,
        "path": site_path,
        "size_bytes": size_bytes,
        "size_human": size_human,
        "size_confidence": estimate.get("confidence"),
        "size_updated_at": estimate.get("updated_at"),
        "size_method": estimate.get("size_method"),
        "drives_count": estimate.get("drives_count"),
    }
    return result, cache_payload


def _persist_sharepoint_estimate(active: dict, result: dict, cache_payload: dict) -> dict:
    site_path = _normalize_site_path(result.get("path", ""))
    def mutate(config):
        for site in config.get("sites", []) or []:
            if _normalize_site_path(site.get("path", "")) == site_path:
                site["size_estimate"] = dict(cache_payload)
                result["cached_to_sites"] = True
                return
        for tenant in config.get("tenants", []) or []:
            if tenant.get("id") != active["id"]:
                continue
            target_cache = dict(tenant.get("sharepoint_target_size_cache") or {})
            for key in (result.get("target_id"), result.get("graph_id"), site_path):
                if key:
                    target_cache[key] = dict(cache_payload)
            if len(target_cache) > 5000:
                target_cache = dict(list(target_cache.items())[-5000:])
            tenant["sharepoint_target_size_cache"] = target_cache
            result["cached_to_tenant"] = True
            return
    update_config(mutate)
    return result


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
    configured_root = Path(load_config()["backup"]["root_dir"]).resolve()
    backup_root = (configured_root / source_backup).resolve()
    if backup_root.parent != configured_root:
        raise ValueError("Invalid backup name")
    if not backup_root.exists() or not backup_root.is_dir():
        raise ValueError(f"Backup not found: {source_backup}")

    normalized = source_site.strip().lower()
    exact_folder = []
    matches = []
    for site_dir in backup_root.iterdir():
        if not site_dir.is_dir() or site_dir.name.startswith("."):
            continue
        resolved_site = site_dir.resolve()
        if resolved_site.parent != backup_root:
            continue
        if site_dir.name.lower() == normalized or site_dir.name.lower() == normalized.replace(" ", "_"):
            exact_folder.append(resolved_site)
        meta_file = site_dir / "_backup_metadata.json"
        if not meta_file.exists():
            continue
        try:
            meta = json.load(open(meta_file))
        except Exception:
            continue
        display_name = str(meta.get("site_name") or "").strip().lower()
        site_path = str(meta.get("site_path") or "").strip().strip("/").lower()
        site_id = str(meta.get("site_id") or "").strip().lower()
        if normalized in {display_name, site_path, site_id}:
            matches.append(resolved_site)

    if len(set(exact_folder)) == 1 and (not matches or len(set(matches)) == 1):
        return exact_folder[0]
    if len(set(matches)) == 1 and not exact_folder:
        return matches[0]
    if exact_folder or matches:
        raise ValueError("Source site is ambiguous; select a unique site path, site ID, or folder name")

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
    from app.workloads.sharepoint import SharePointWorkload

    sharepoint_site_selection = [
        SharePointWorkload.target_id_from_path(site.get("path", ""))
        for site in (load_config().get("sites", []) or [])
        if site.get("enabled")
    ]
    return render_template(
        "workloads.html",
        active_tenant=_with_tenant_slug(tm.get_active_tenant(include_secret=False)),
        workload_meta=WORKLOAD_META,
        sharepoint_site_selection=sharepoint_site_selection,
    )


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
    try:
        tenant = tm.update_tenant(tid, request.json or {})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
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


@m365_bp.route("/api/workloads/sharepoint/target-estimate", methods=["POST"])
def api_sharepoint_target_estimate():
    active = tm.get_active_tenant(include_secret=True)
    if not active:
        return jsonify({"error": "No active tenant"}), 400

    payload = request.get_json(force=True, silent=True) or {}
    graph_id = str(payload.get("graph_id") or "").strip()
    site_path = _normalize_site_path(payload.get("path", ""))
    if not graph_id and "path" not in payload:
        return jsonify({"error": "Target graph_id or path is required"}), 400

    try:
        workload = get_workload("sharepoint", active)
        estimate = workload.estimate_target_size(graph_id=graph_id, site_path=site_path)
        result, cache_payload = _build_sharepoint_estimate_result(payload, estimate)
        _persist_sharepoint_estimate(active, result, cache_payload)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@m365_bp.route("/api/workloads/sharepoint/target-estimates", methods=["POST"])
def api_sharepoint_target_estimates():
    active = tm.get_active_tenant(include_secret=True)
    if not active:
        return jsonify({"error": "No active tenant"}), 400

    payload = request.get_json(force=True, silent=True) or {}
    targets = payload.get("targets") or []
    if not isinstance(targets, list) or not targets:
        return jsonify({"error": "Target list is required"}), 400
    if len(targets) > 50 or any(not isinstance(item, dict) for item in targets):
        return jsonify({"error": "At most 50 target objects are allowed per request"}), 400

    try:
        from app.tasks import estimate_sharepoint_targets_task
        task = estimate_sharepoint_targets_task.apply_async(
            args=[active["id"], targets], queue="estimates"
        )
        return jsonify({"status": "queued", "task_id": task.id, "total": len(targets)}), 202
    except Exception as e:
        return jsonify({"error": str(e)}), 502


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

    if wtype == "sharepoint":
        workload = get_workload(wtype, active)
        targets = workload.list_targets()
        if targets and isinstance(targets, list) and targets[0].get("error"):
            err = _classify_workload_error(targets[0]["error"])
            return jsonify({"error": err["message"], "error_detail": targets[0]["error"]}), err["status_code"]

        target_map = {
            str(item.get("id") or "").strip(): item
            for item in (targets or [])
            if str(item.get("id") or "").strip()
        }
        selected_targets = [target_map[item] for item in selection["selected_ids"] if item in target_map]
        if not selected_targets:
            return jsonify({"error": "None of the selected SharePoint targets could be resolved from discovery results."}), 400

        config = load_config()
        sites = list(config.get("sites", []) or [])
        index_by_path = {
            _normalize_site_path(site.get("path", "")): idx
            for idx, site in enumerate(sites)
        }

        added = 0
        reenabled = 0
        updated = 0
        for target in selected_targets:
            site_path = _normalize_site_path(target.get("path", ""))
            site_name = str(target.get("name") or site_path or "Root Site").strip()
            size_estimate = {}
            if target.get("size_bytes") is not None or target.get("size_human"):
                size_estimate = {
                    "size_bytes": target.get("size_bytes"),
                    "size_human": target.get("size_human"),
                    "confidence": target.get("size_confidence"),
                    "updated_at": target.get("size_updated_at"),
                    "source": "workload_target_cache",
                }
            existing_idx = index_by_path.get(site_path)
            if existing_idx is None:
                new_site = {
                    "name": site_name,
                    "path": site_path,
                    "enabled": True,
                }
                if size_estimate:
                    new_site["size_estimate"] = size_estimate
                sites.append(new_site)
                index_by_path[site_path] = len(sites) - 1
                added += 1
                continue

            site_entry = dict(sites[existing_idx] or {})
            if site_entry.get("name") != site_name:
                site_entry["name"] = site_name
                updated += 1
            if not site_entry.get("enabled"):
                site_entry["enabled"] = True
                reenabled += 1
            if size_estimate and site_entry.get("size_estimate") != size_estimate:
                site_entry["size_estimate"] = size_estimate
                updated += 1
            sites[existing_idx] = site_entry

        config["sites"] = sites
        save_config(config)

        synced_selection = workload.get_target_selection()
        return jsonify({
            "status": "saved",
            "workload": wtype,
            "selection": synced_selection,
            "tenant_id": active.get("id"),
            "site_sync": {
                "added": added,
                "reenabled": reenabled,
                "updated": updated,
                "total_selected": len(selected_targets),
            },
            "manage_href": "/sites",
        })

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
