"""Compatibility v11 routes for tenant-aware backup listing and start flow."""
from flask import jsonify, request

from app.backup_registry import BackupRegistry
from app.operation_queue import OperationQueue
from app.tenant_manager import TenantManager
from app.workloads import BACKUP_ENABLED_WORKLOADS, filter_backup_workloads

SUPPORTED_WORKLOADS = set(BACKUP_ENABLED_WORKLOADS)


def register_v11_routes(app):
    registry = BackupRegistry()
    tm = TenantManager()

    def _apply_active_backup_overlay(backups: list) -> tuple[list, dict | None]:
        try:
            from app.main import get_active_task_overlay

            active_backup = get_active_task_overlay("backup", prefer_disk_size=True)
        except Exception:
            active_backup = None

        if not active_backup:
            return backups, None

        active_meta = active_backup.get("meta") or {}
        active_path = active_meta.get("backup_path")
        if not active_path:
            return backups, active_backup

        live_size = int(active_backup.get("live_size_bytes") or 0)
        for item in backups:
            if item.get("backup_path") != active_path:
                continue
            if live_size > int(item.get("size_bytes") or 0):
                item["size_bytes"] = live_size
                item["size_human"] = registry._format_size(live_size)
            item["status"] = "running"
            break
        return backups, active_backup

    @app.route("/api/v2/backups")
    def list_backups_v2():
        tenant_slug = request.args.get("tenant_slug", "").strip()
        workload = request.args.get("workload", "").strip()
        backups = registry.list_all(use_cache=request.args.get("refresh") != "1")
        backups, active_backup = _apply_active_backup_overlay(backups)
        if tenant_slug:
            backups = [item for item in backups if item["tenant_slug"] == tenant_slug]
        if workload:
            backups = [item for item in backups if item["workload"] == workload]
        return jsonify({
            "backups": backups,
            "total": len(backups),
            "active_backup": {
                "task_id": active_backup.get("task_id"),
                "backup_path": (active_backup.get("meta") or {}).get("backup_path"),
                "live_size_bytes": active_backup.get("live_size_bytes", 0),
                "live_size_human": active_backup.get("live_size_human", "0 B"),
            } if active_backup else None,
        })

    @app.route("/api/v2/backups/<tenant_slug>/<workload>/<backup_name>", methods=["DELETE"])
    def delete_backup_v2(tenant_slug, workload, backup_name):
        result = registry.delete(tenant_slug, workload, backup_name)
        status = 200 if result.get("status") == "deleted" else 404
        return jsonify(result), status

    @app.route("/api/v2/backups/<tenant_slug>/<workload>/<backup_name>")
    def get_backup_v2(tenant_slug, workload, backup_name):
        backup = registry.get_backup(tenant_slug, workload, backup_name)
        if not backup:
            return jsonify({"error": "Not found"}), 404
        return jsonify({"backup": backup})

    @app.route("/api/v2/backups/<tenant_slug>/<workload>/<backup_name>/contents")
    def browse_backup_v2(tenant_slug, workload, backup_name):
        try:
            payload = registry.browse_backup(
                tenant_slug,
                workload,
                backup_name,
                relative_path=request.args.get("path", ""),
            )
            return jsonify(payload)
        except FileNotFoundError as e:
            return jsonify({"error": str(e)}), 404
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/v2/backups/stats")
    def backup_stats_v2():
        stats = registry.get_stats()
        backups, active_backup = _apply_active_backup_overlay(registry.list_all(use_cache=False))
        if active_backup:
            stats["total_backups"] = len(backups)
            stats["total_size_bytes"] = sum(item["size_bytes"] for item in backups)
            stats["total_size_human"] = registry._format_size(stats["total_size_bytes"])
            stats["active_backup"] = {
                "task_id": active_backup.get("task_id"),
                "backup_path": (active_backup.get("meta") or {}).get("backup_path"),
                "live_size_bytes": active_backup.get("live_size_bytes", 0),
                "live_size_human": active_backup.get("live_size_human", "0 B"),
            }
        return jsonify(stats)

    @app.route("/api/v2/tenants/<tenant_slug>/history")
    def tenant_history_v2(tenant_slug):
        limit = int(request.args.get("limit", 20))
        history = registry.get_history(tenant_slug, limit=limit)
        return jsonify({"history": history, "total": len(history)})

    @app.route("/api/v2/backup/start", methods=["POST"])
    def start_backup_v2():
        from app.tasks import run_backup_task

        data = request.get_json(force=True, silent=True) or {}
        try:
            from app.main import get_active_backup_guard

            active_backup = get_active_backup_guard()
        except Exception:
            active_backup = None
        tenant_id = (data.get("tenant_id") or "").strip() or None
        workloads = data.get("workloads") or []
        custom_root = (data.get("custom_root") or "").strip() or None

        if tenant_id:
            tenant = tm.get_tenant(tenant_id)
            if not tenant:
                return jsonify({"error": "Tenant not found"}), 404
            tm.set_active_tenant(tenant_id)
        else:
            tenant = tm.get_active_tenant(include_secret=True)

        if not tenant:
            return jsonify({"error": "No active tenant configured"}), 400

        if not workloads:
            workloads = tenant.get("workloads_enabled", ["sharepoint"])
        workloads = [str(item).strip().lower() for item in workloads if str(item).strip()]
        supported = filter_backup_workloads(workloads)
        unsupported = [item for item in workloads if item not in SUPPORTED_WORKLOADS]

        if not supported:
            return jsonify({
                "error": "No supported workloads requested",
                "supported_workloads": sorted(SUPPORTED_WORKLOADS),
                "requested_workloads": workloads,
            }), 400

        if active_backup:
            queue_item = OperationQueue().enqueue(
                "backup",
                "tenant_backup",
                {
                    "custom_root": custom_root,
                    "tenant_id": tenant.get("id"),
                    "workloads": supported,
                },
                f"Tenant Backup · {tenant.get('name')}",
                ", ".join(supported),
            )
            return jsonify({
                "status": "queued",
                "queue_item": queue_item,
                "active_backup": active_backup,
                "tenant_id": tenant.get("id"),
                "tenant_name": tenant.get("name"),
                "workloads": supported,
                "message": "Another backup task is still running. This tenant backup has been queued.",
            }), 202

        task = run_backup_task.delay(
            custom_root=custom_root,
            tenant_id=tenant.get("id"),
            workloads=supported,
        )
        registry.invalidate_cache()
        return jsonify({
            "status": "started",
            "task_id": task.id,
            "tenant_id": tenant.get("id"),
            "tenant_name": tenant.get("name"),
            "tenant_slug": registry.get_tenant_backup_root(tenant, supported[0]).parent.name,
            "backup_root": custom_root or str(registry.get_tenant_backup_root(tenant, supported[0])),
            "workloads": supported,
            "warnings": [f"Workload not yet executed by current engine: {item}" for item in unsupported],
        })

    @app.route("/api/v2/browse/tenants")
    def browse_tenants_v2():
        backups = registry.list_all(use_cache=False)
        seen = {}
        for item in backups:
            seen[item["tenant_slug"]] = {"slug": item["tenant_slug"], "name": item["tenant_name"]}
        return jsonify({"tenants": list(seen.values())})

    @app.route("/api/v2/browse/<tenant_slug>/workloads")
    def browse_workloads_v2(tenant_slug):
        backups = registry.list_all(use_cache=False)
        counts = {}
        for item in backups:
            if item["tenant_slug"] != tenant_slug:
                continue
            counts[item["workload"]] = counts.get(item["workload"], 0) + 1
        return jsonify({
            "workloads": [
                {"name": name, "backup_count": count}
                for name, count in sorted(counts.items())
            ]
        })
