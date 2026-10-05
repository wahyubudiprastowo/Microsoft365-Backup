import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app import main as web
from app.backup_engine import BackupEngine, ProgressTracker
from app.restore_manager_v2 import RestoreManagerV2
from app.restore.sharepoint import SharePointRestore
from app.restore.onedrive import OneDriveRestore
from app import tasks


class AuditPatchTests(unittest.TestCase):
    def test_admin_auth_is_required_except_health(self):
        web.app.testing = False
        try:
            with patch.dict(web.os.environ, {"SPO_ADMIN_USERNAME": "admin", "SPO_ADMIN_PASSWORD": "test-secret"}):
                client = web.app.test_client()
                self.assertEqual(client.get("/api/health").status_code, 200)
                self.assertEqual(client.get("/api/config").status_code, 401)
                token = base64.b64encode(b"admin:test-secret").decode()
                headers = {"Authorization": f"Basic {token}"}
                with patch.object(web, "load_config", return_value={"azure_ad": {}}):
                    self.assertEqual(client.get("/api/config", headers=headers).status_code, 200)
                self.assertEqual(
                    client.post("/api/sites", headers={**headers, "Origin": "https://evil.example"}, json={}).status_code,
                    403,
                )
        finally:
            web.app.testing = False

    def test_site_estimate_refresh_is_queued(self):
        web.app.testing = True
        try:
            with patch.object(web, "load_config", return_value={"sites": [{"path": "sites/one"}]}), \
                 patch("app.tenant_manager.TenantManager.get_active_tenant", return_value={"id": "tenant-1"}), \
                 patch.object(web._redis, "get", return_value=None), \
                 patch.object(web._redis, "set", return_value=True), \
                 patch.object(tasks.estimate_sharepoint_targets_task, "apply_async") as enqueue:
                response = web.app.test_client().post("/api/sites/estimates/refresh")
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json["total"], 1)
            self.assertEqual(enqueue.call_args.kwargs["queue"], "estimates")
        finally:
            web.app.testing = False

    def test_restore_status_distinguishes_partial_and_failure(self):
        classify = RestoreManagerV2._result_status
        self.assertEqual(classify({"items_processed": 2, "items_failed": 1}), "partial")
        self.assertEqual(classify({"items_processed": 0, "items_failed": 1}), "failed")
        self.assertEqual(classify({"errors": ["Fatal: target missing"]}), "failed")
        self.assertEqual(classify({"items_processed": 2, "items_failed": 0}), "completed")

    def test_site_identity_reuses_only_matching_legacy_folder(self):
        engine = BackupEngine.__new__(BackupEngine)
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / "Same_Name"
            legacy.mkdir()
            (legacy / "_backup_metadata.json").write_text(json.dumps({"site_id": "site-A", "site_path": "sites/a"}))
            path_a, reused = engine._site_backup_dir(tmp, "Same Name", "site-A", "sites/a")
            self.assertEqual(path_a, str(legacy))
            self.assertTrue(reused)
            path_b, reused_b = engine._site_backup_dir(tmp, "Same Name", "site-B", "sites/b")
            self.assertNotEqual(path_a, path_b)
            self.assertFalse(reused_b)
            self.assertEqual(Path(path_b).parent, Path(tmp))

    def test_download_path_cannot_escape_site_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                BackupEngine._safe_destination(tmp, "Documents", "../../outside", "file.txt")

    def test_serial_download_rejects_incomplete_file(self):
        engine = BackupEngine.__new__(BackupEngine)
        engine.progress = ProgressTracker()
        engine._check_control = lambda: None
        engine._emit = lambda *args: None
        engine._headers = lambda: {}

        def response():
            item = MagicMock()
            item.status_code = 200
            item.headers = {"Content-Length": "10"}
            item.iter_content.return_value = [b"data"]
            item.raise_for_status.return_value = None
            return item

        engine.session = MagicMock()
        engine.session.get.side_effect = lambda *args, **kwargs: response()
        with tempfile.TemporaryDirectory() as tmp, patch("app.backup_engine.time.sleep"), patch("app.backup_engine.compute_backoff_delay", return_value=0):
            destination = str(Path(tmp) / "file.bin")
            with self.assertRaises(Exception):
                engine._download("https://example.test/file", destination, 10, auth_required=False)
            self.assertFalse(Path(destination).exists())

    def test_same_size_changed_file_is_replaced(self):
        engine = BackupEngine.__new__(BackupEngine)
        engine.progress = ProgressTracker()
        engine._check_control = lambda: None
        engine._emit = lambda *args: None
        engine._headers = lambda: {}
        response = MagicMock()
        response.status_code = 200
        response.headers = {"Content-Length": "4"}
        response.iter_content.return_value = [b"new!"]
        engine.session = MagicMock()
        engine.session.get.return_value = response
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "file.bin"
            destination.write_bytes(b"old!")
            result = engine._download("https://example.test/file", str(destination), 4,
                                      auth_required=False, source_identity="drive:item:new-etag")
            self.assertFalse(result["skipped"])
            self.assertEqual(destination.read_bytes(), b"new!")
            self.assertFalse(Path(str(destination) + ".tmp.meta").exists())

    def test_parallel_ranges_write_exact_file(self):
        data = b"abcdefghij" * 1800000
        engine = BackupEngine.__new__(BackupEngine)
        engine.progress = ProgressTracker()
        engine.progress.file_start("file.bin", len(data))
        engine._check_control = lambda: None
        engine._emit = lambda *args: None
        engine._headers = lambda: {}

        def get(url, headers, **kwargs):
            spec = headers["Range"].split("=")[1]
            start, end = (int(part) for part in spec.split("-"))
            item = MagicMock()
            item.status_code = 206
            item.headers = {"Content-Range": f"bytes {start}-{end}/{len(data)}"}
            item.iter_content.return_value = [data[start:end + 1]]
            return item

        engine.session = MagicMock()
        engine.session.get.side_effect = get
        with tempfile.TemporaryDirectory() as tmp, patch("app.backup_engine.build_retry_session") as factory:
            factory.return_value.get.side_effect = get
            destination = str(Path(tmp) / "file.bin")
            result = engine._download_parallel_ranges("https://example.test/file", destination, len(data), False)
            self.assertEqual(result["final_size"], len(data))
            self.assertEqual(Path(destination).read_bytes(), data)

    def test_restore_rejects_uploaded_size_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "file.bin"
            source.write_bytes(b"four")
            for cls in (SharePointRestore, OneDriveRestore):
                restore = cls.__new__(cls)
                restore.mode = "overwrite"
                restore.stats = {"items_processed": 0, "items_failed": 0, "bytes_uploaded": 0, "errors": []}
                restore._check_control = lambda: None
                restore.emit = lambda *args: None
                restore._put = lambda *args, **kwargs: {"size": 3}
                restore._upload_file(source, "drive-id", "")
                self.assertEqual(restore.stats["items_failed"], 1)
                self.assertEqual(restore.stats["items_processed"], 0)


if __name__ == "__main__":
    unittest.main()
