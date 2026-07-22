"""v13 routes for multi-workload restore UI and API."""
from flask import jsonify, render_template, request

from app.operation_dispatcher import dispatch_next_queued_operation
from app.operation_queue import OperationQueue
from app.restore.sharepoint import SharePointRestore
from app.restore_manager_v2 import RestoreManagerV2
from app.tenant_manager import TenantManager


def register_v13_routes(app):
    mgr = RestoreManagerV2()
    tm = TenantManager()

    @app.route("/restore-v2")
    def restore_v2_page():
        return render_template("restore_v2.html")

    @app.route("/api/v2/restore/jobs", methods=["POST"])
    def create_restore_job_v13():
        data = request.get_json(force=True, silent=True) or {}
        try:
            job = mgr.create_job(data)
            queue_item = OperationQueue().enqueue(
                "restore",
                "restore_v2",
                {"job_id": job["id"]},
                f"Restore {job.get('workload', 'job')}",
                f"{job.get('tenant_name') or 'Unknown tenant'} · {job.get('source_backup') or ''}",
            )
            running_restore = next((item for item in mgr.list_jobs(limit=100) if item.get("status") == "running"), None)
            dispatched = None if running_restore else dispatch_next_queued_operation("restore")
            if dispatched and dispatched.get("task_id"):
                job = mgr.get_job(job["id"]) or job
                return jsonify({"status": "created", "job": job, "dispatched": True}), 201
            message = None
            if running_restore:
                message = "Another restore job is already running. This restore job has been queued."
            return jsonify({"status": "queued", "job": job, "queue_item": queue_item, "message": message}), 202
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/v2/restore/jobs", methods=["GET"])
    def list_restore_jobs_v13():
        mgr.recover_stale_queued_jobs(limit=int(request.args.get("limit", 50)) or 50)
        return jsonify({"jobs": mgr.list_jobs(limit=int(request.args.get("limit", 50)))})

    @app.route("/api/v2/restore/jobs/<job_id>", methods=["GET"])
    def get_restore_job_v13(job_id):
        job = mgr.get_job(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        return jsonify(job)

    @app.route("/api/v2/restore/jobs/<job_id>/cancel", methods=["POST"])
    def cancel_restore_job_v13(job_id):
        job = mgr.get_job(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        task_id = job.get("task_id")
        if not task_id and job.get("status") == "queued":
            from app.operation_queue import OperationQueue

            queue = OperationQueue()
            for item in queue.list("restore", limit=100):
                if (item.get("payload") or {}).get("job_id") == job_id:
                    queue.remove(item["id"], group="restore")
                    break
            mgr.update_job(job_id, {"status": "cancelled"})
            return jsonify({"status": "cancelled"})
        if task_id:
            try:
                from app.tasks import force_cancel_task
                force_cancel_task(task_id)
            except Exception:
                pass
        mgr.update_job(job_id, {"status": "cancelled"})
        return jsonify({"status": "cancelled"})

    @app.route("/api/v2/restore/jobs/<job_id>", methods=["DELETE"])
    def delete_restore_job_v13(job_id):
        if not mgr.delete_job(job_id):
            return jsonify({"error": "Cannot delete active job"}), 409
        return jsonify({"status": "deleted"})

    @app.route("/api/v2/restore/preview", methods=["POST"])
    def preview_restore_v13():
        data = request.get_json(force=True, silent=True) or {}
        try:
            return jsonify(mgr.dry_run(data))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/v2/restore/sharepoint/libraries", methods=["POST"])
    def list_sharepoint_target_libraries_v13():
        data = request.get_json(force=True, silent=True) or {}
        tenant_id = str(data.get("tenant_id") or "").strip()
        target_site_id = str(data.get("target_site_id") or "").strip()
        target_site_path = str(data.get("target_site_path") or "").strip()
        if not tenant_id:
            return jsonify({"error": "tenant_id is required"}), 400
        if not target_site_id and not target_site_path:
            return jsonify({"error": "target_site_path or target_site_id is required"}), 400

        tenant = tm.get_tenant(tenant_id, include_secret=True)
        if not tenant:
            return jsonify({"error": f"Tenant not found: {tenant_id}"}), 404

        try:
            restorer = SharePointRestore(
                tenant=tenant,
                backup_path="/tmp",
                target_site_id=target_site_id or None,
                target_site_path=target_site_path or None,
            )
            resolved_site_id = restorer._resolve_target_site()
            drives = restorer._get_target_drives(resolved_site_id)
            names = sorted(drives.keys())
            return jsonify({
                "target_site_id": resolved_site_id,
                "libraries": names,
                "default_library": "Documents" if "Documents" in drives else (names[0] if names else None),
                "message": "Target folder will be created automatically if it does not exist. Target library must already exist.",
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500
