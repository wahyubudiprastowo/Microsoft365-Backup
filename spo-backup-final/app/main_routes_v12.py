"""v12 routes for per-tenant schedules and notifications."""
from flask import jsonify, request

from app.schedule_manager import ScheduleManager


def register_v12_routes(app):
    sm = ScheduleManager()

    @app.route("/api/v2/tenants/<tenant_id>/schedule", methods=["GET"])
    def get_tenant_schedule_v12(tenant_id):
        return jsonify(sm.get_schedule(tenant_id))

    @app.route("/api/v2/tenants/<tenant_id>/schedule", methods=["POST", "PUT"])
    def set_tenant_schedule_v12(tenant_id):
        data = request.get_json(force=True, silent=True) or {}
        try:
            if not sm.set_schedule(tenant_id, data):
                return jsonify({"error": "Tenant not found"}), 404
            return jsonify({"status": "saved", "schedule": sm.get_schedule(tenant_id)})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/v2/tenants/<tenant_id>/notifications", methods=["GET"])
    def get_tenant_notifications_v12(tenant_id):
        return jsonify(sm.get_notifications(tenant_id))

    @app.route("/api/v2/tenants/<tenant_id>/notifications", methods=["POST", "PUT"])
    def set_tenant_notifications_v12(tenant_id):
        data = request.get_json(force=True, silent=True) or {}
        try:
            if not sm.set_notifications(tenant_id, data):
                return jsonify({"error": "Tenant not found"}), 404
            return jsonify({"status": "saved", "notifications": sm.get_notifications(tenant_id)})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/v2/tenants/<tenant_id>/notifications/test", methods=["POST"])
    def test_tenant_notification_v12(tenant_id):
        from app.tasks_v12 import send_tenant_notification
        from app.tenant_manager import TenantManager

        tenant = TenantManager().get_tenant(tenant_id)
        if not tenant:
            return jsonify({"error": "Tenant not found"}), 404

        test_stats = {
            "task_id": "test-notification",
            "tenant_id": tenant_id,
            "tenant_name": tenant.get("name"),
            "successful_sites": 1,
            "total_sites": 1,
            "files_downloaded": 42,
            "files_skipped": 7,
            "bytes_downloaded": 12345678,
            "errors": [],
            "failed_sites": [],
            "start_time": "2026-07-08T12:00:00+00:00",
            "end_time": "2026-07-08T12:00:15+00:00",
        }
        try:
            result = send_tenant_notification(tenant_id, test_stats)
            failures = [item for item in (result or []) if not item.get("success")]
            successes = [item for item in (result or []) if item.get("success")]
            status = "sent"
            if failures and successes:
                status = "partial"
            elif failures and not successes:
                status = "failed"
            return jsonify({
                "status": status,
                "result": result,
                "summary": {
                    "channels_ok": len(successes),
                    "channels_failed": len(failures),
                    "channels_total": len(result or []),
                }
            }), (500 if status == "failed" else 200)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/v2/schedules")
    def list_all_schedules_v12():
        return jsonify({"schedules": sm.list_enabled_schedules()})

    @app.route("/api/v2/schedules/reload", methods=["POST"])
    def reload_schedules_v12():
        try:
            from app.tasks import celery_app
            from app.tasks_v12 import register_tenant_schedules

            registered = register_tenant_schedules(celery_app)
            return jsonify({
                "status": "reload_staged",
                "note": "Schedule config reloaded in web process. Restart celery-beat to fully apply updated schedules.",
                "pending_restart": True,
                "active_schedules": registered,
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500
