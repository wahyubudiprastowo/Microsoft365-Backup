"""v13 routes for multi-workload restore UI and API."""
from flask import jsonify, render_template, request

from app.restore_manager_v2 import RestoreManagerV2


def register_v13_routes(app):
    mgr = RestoreManagerV2()

    @app.route("/restore-v2")
    def restore_v2_page():
        return render_template("restore_v2.html")

    @app.route("/api/v2/restore/jobs", methods=["POST"])
    def create_restore_job_v13():
        data = request.get_json(force=True, silent=True) or {}
        try:
            job = mgr.create_job(data)
            from app.tasks import celery_app
            celery_app.send_task("app.tasks.execute_restore_job_v2", args=[job["id"]])
            return jsonify({"status": "created", "job": job}), 201
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/v2/restore/jobs", methods=["GET"])
    def list_restore_jobs_v13():
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
