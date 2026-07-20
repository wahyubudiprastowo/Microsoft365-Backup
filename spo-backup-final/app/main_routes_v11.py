"""Compatibility v11 routes for tenant-aware backup listing and start flow."""
from flask import jsonify, request

from app.backup_registry import BackupRegistry
from app.tenant_manager import TenantManager
from app.workloads import BACKUP_ENABLED_WORKLOADS, filter_backup_workloads

SUPPORTED_WORKLOADS = set(BACKUP_ENABLED_WORKLOADS)


def register_v11_routes(app):
    registry = BackupRegistry()
    tm = TenantManager()

    @app.route("/api/v2/backups")
    def list_backups_v2():
        tenant_slug = request.args.get("tenant_slug", "").strip()
        workload = request.args.get("workload", "").strip()
        backups = registry.list_all(use_cache=request.args.get("refresh") != "1")
        if tenant_slug:
            backups = [item for item in backups if item["tenant_slug"] == tenant_slug]
        if workload:
            backups = [item for item in backups if item["workload"] == workload]
        return jsonify({"backups": backups, "total": len(backups)})

    @app.route("/api/v2/backups/<tenant_slug>/<workload>/<backup_name>", methods=["DELETE"])
    def delete_backup_v2(tenant_slug, workload, backup_name):
        result = registry.delete(tenant_slug, workload, backup_name)
        status = 200 if result.get("status") == "deleted" else 404
        return jsonify(result), status

    @app.route("/api/v2/backups/stats")
    def backup_stats_v2():
        return jsonify(registry.get_stats())

    @app.route("/api/v2/tenants/<tenant_slug>/history")
    def tenant_history_v2(tenant_slug):
        limit = int(request.args.get("limit", 20))
        history = registry.get_history(tenant_slug, limit=limit)
        return jsonify({"history": history, "total": len(history)})

    @app.route("/api/v2/backup/start", methods=["POST"])
    def start_backup_v2():
        from app.tasks import run_backup_task

        data = request.get_json(force=True, silent=True) or {}
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
