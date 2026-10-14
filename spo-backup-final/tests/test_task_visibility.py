import unittest
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app import main as web
from app import tasks
from app.restore_manager_v2 import RestoreManagerV2


class TaskVisibilityTests(unittest.TestCase):
    def setUp(self):
        web.app.testing = True

    def tearDown(self):
        web.app.testing = False

    def test_marker_is_recreated_and_renewed_without_stealing_another_task(self):
        redis_client = MagicMock()
        state = tasks._new_progress_publish_state()
        redis_client.get.return_value = None

        with patch.object(tasks.time, "monotonic", return_value=100):
            tasks._refresh_task_tracking(redis_client, "backup", "task-1", state)
        redis_client.setex.assert_called_once_with("spo:current_backup_task", 86400, "task-1")

        redis_client.reset_mock()
        redis_client.get.return_value = "task-2"
        with patch.object(tasks.time, "monotonic", return_value=131):
            tasks._refresh_task_tracking(redis_client, "backup", "task-1", state)
        redis_client.setex.assert_not_called()

    def test_missing_marker_is_recovered_from_active_worker(self):
        inspector = MagicMock()
        inspector.active.return_value = {
            "worker-1": [{"id": "task-1", "name": "app.tasks.run_backup_task"}]
        }
        celery_app = SimpleNamespace(control=SimpleNamespace(inspect=lambda **kwargs: inspector))
        web._active_discovery_next_at.clear()

        with patch.object(web._redis, "get", return_value=None), \
             patch.object(web._redis, "setex") as setex, \
             patch.object(web, "get_celery", return_value=(celery_app,) + (None,) * 6):
            self.assertEqual(web._get_tracked_task_id("backup"), "task-1")
        setex.assert_called_once_with("spo:current_backup_task", 86400, "task-1")

    def test_finished_snapshot_is_not_reported_as_progress(self):
        result = SimpleNamespace(state="SUCCESS", info={}, result={"status": "success"})
        with patch.object(web, "_read_cached_task_snapshot", return_value={"state": "SUCCESS", "meta": {}}), \
             patch.object(web, "get_celery", return_value=(None,) * 7), \
             patch.object(web, "AsyncResult", return_value=result), \
             patch.object(web.TaskController, "get_state", return_value="running"):
            response = web.app.test_client().get("/api/backup/status/task-1")
        self.assertEqual(response.json["state"], "SUCCESS")

    def test_pending_task_without_control_marker_stays_pending(self):
        result = SimpleNamespace(state="PENDING", info=None, result=None)
        with patch.object(web, "_read_cached_task_snapshot", return_value=None), \
             patch.object(web, "get_celery", return_value=(None,) * 7), \
             patch.object(web, "AsyncResult", return_value=result), \
             patch.object(web._redis, "get", return_value=None):
            response = web.app.test_client().get("/api/backup/status/task-1")
        self.assertEqual(response.json["state"], "PENDING")

    def test_download_terminal_snapshot_is_not_reported_as_progress(self):
        result = SimpleNamespace(state="SUCCESS", info={}, result={"status": "success"})
        with patch.object(web, "_read_cached_task_snapshot", return_value={"state": "SUCCESS", "meta": {}}), \
             patch.object(web, "get_celery", return_value=(None,) * 7), \
             patch.object(web, "AsyncResult", return_value=result), \
             patch.object(web.TaskController, "get_state", return_value="running"):
            response = web.app.test_client().get("/api/download/status/task-1")
        self.assertEqual(response.json["state"], "SUCCESS")

    def test_restore_listing_does_not_inspect_workers_without_queued_jobs(self):
        manager = RestoreManagerV2()
        with patch.object(manager, "list_jobs", return_value=[{"status": "completed"}]), \
             patch.object(manager, "_list_restore_task_ids") as inspect_tasks:
            self.assertEqual(manager.recover_stale_queued_jobs(), [])
        inspect_tasks.assert_not_called()

    def test_config_response_masks_nested_secrets(self):
        config = {
            "azure_ad": {"client_secret": "azure-secret"},
            "sharepoint": {}, "schedule": {}, "sites": [],
            "notification": {"telegram": {"bot_token": "telegram-secret"}},
            "tenants": [{"id": "tenant-1", "client_secret": "tenant-secret"}],
        }
        with patch.object(web, "load_config", return_value=config):
            response = web.app.test_client().get("/api/config")
        self.assertEqual(response.json["tenants"][0]["client_secret"], "***MASKED***")
        self.assertEqual(response.json["notification"]["telegram"]["bot_token"], "***MASKED***")

    def test_config_save_preserves_masked_secrets(self):
        config = {
            "azure_ad": {"client_secret": "azure-secret"},
            "sharepoint": {}, "schedule": {}, "sites": [],
            "notification": {"telegram": {"bot_token": "telegram-secret"}},
            "tenants": [{"id": "tenant-1", "client_secret": "tenant-secret"}],
            "backup": {"remote_destinations": [{"name": "remote-1", "config": {"password": "remote-secret"}}]},
        }
        masked = {
            "azure_ad": {"client_secret": "***MASKED***"},
            "sharepoint": {}, "schedule": {}, "sites": [],
            "notification": {"telegram": {"bot_token": "***MASKED***"}},
            "tenants": [{"id": "tenant-1", "client_secret": "***MASKED***"}],
            "backup": {"remote_destinations": [{"name": "remote-1", "config": {"password": "***MASKED***"}}]},
        }
        masked["_config_revision"] = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        saved = {}
        def update(mutator):
            data = json.loads(json.dumps(config))
            mutator(data)
            saved.update(data)
        with patch.object(web, "load_config", return_value=config), \
             patch.object(web, "update_config", side_effect=update):
            response = web.app.test_client().post("/api/config", json=masked)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(saved["tenants"][0]["client_secret"], "tenant-secret")
        self.assertEqual(saved["notification"]["telegram"]["bot_token"], "telegram-secret")
        self.assertEqual(saved["backup"]["remote_destinations"][0]["config"]["password"], "remote-secret")

    def test_empty_queue_does_not_inspect_workers(self):
        with patch.object(web, "OperationQueue") as queue_cls, \
             patch("app.restore_manager_v2.RestoreManagerV2") as restore_cls, \
             patch.object(web, "get_active_backup_guard") as backup_guard, \
             patch.object(web, "get_active_download_guard") as download_guard:
            queue_cls.return_value.length.return_value = 0
            restore_cls.return_value.list_jobs.return_value = []
            self.assertEqual(web.maybe_dispatch_queued_operations(force=True), [])
        backup_guard.assert_not_called()
        download_guard.assert_not_called()
        restore_cls.return_value.recover_stale_queued_jobs.assert_not_called()


if __name__ == "__main__":
    unittest.main()
