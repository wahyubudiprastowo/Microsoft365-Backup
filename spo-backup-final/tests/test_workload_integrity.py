import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from app.workloads.base import BaseWorkload
from app.workloads.sharepoint import SharePointWorkload
from app.workloads.teams import TeamsWorkload
from app.workloads.outlook import OutlookWorkload
from app.workloads.onedrive import OneDriveWorkload
from app.restore.onedrive import OneDriveRestore
from app.restore.outlook import OutlookRestore
from app.restore.teams import TeamsRestore
from app.restore_manager_v2 import RestoreManagerV2
from app import main as web


TENANT = {"id": "tenant-1", "name": "Test", "tenant_id": "test-tenant",
          "client_id": "test-client", "client_secret": "test-secret"}


def not_found():
    response = requests.Response()
    response.status_code = 404
    return requests.HTTPError("Not found", response=response)


class WorkloadIntegrityTests(unittest.TestCase):
    def test_stream_download_rejects_short_body(self):
        workload = BaseWorkload(TENANT)
        workload._check_control = lambda: None
        workload.session = MagicMock()
        response = MagicMock(status_code=200, headers={"Content-Length": "9"})
        response.iter_content.return_value = [b"short"]
        workload.session.get.return_value = response
        with tempfile.TemporaryDirectory() as tmp, patch("app.workloads.base.time.sleep"):
            dest = Path(tmp) / "file.bin"
            with self.assertRaises(IOError):
                workload._download("https://files.example/file", str(dest), 9, auth_required=False)
            self.assertFalse(dest.exists())
            self.assertEqual(workload.session.get.call_count, 5)

    def test_sharepoint_zero_quota_is_known_and_drives_paginate(self):
        workload = SharePointWorkload(TENANT)
        workload._paginate = MagicMock(return_value=iter([
            {"id": "drive-1", "quota": {"used": 0}},
            {"id": "drive-2", "quota": {"used": 0}},
        ]))
        estimate = workload._estimate_from_site({"id": "site-1"}, "sites/a")
        self.assertEqual(estimate["size_bytes"], 0)
        self.assertEqual(estimate["confidence"], "high")
        self.assertEqual(estimate["drives_count"], 2)

    def test_teams_downloads_nested_channel_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            workload = TeamsWorkload(TENANT, backup_root=tmp)
            def pages(url, params=None):
                if "root-item/children" in url:
                    return iter([{"id": "folder-1", "name": "Docs", "folder": {}}])
                return iter([{"id": "file-1", "name": "report.txt", "size": 4}])
            workload._paginate = pages
            def download(url, dest, size, auth_required=True):
                Path(dest).write_bytes(b"data")
                return 4
            workload._download = download
            index = []
            workload._walk_channel_files("drive-1", "root-item", Path(tmp), index)
            self.assertEqual((Path(tmp) / "Docs" / "report.txt").read_bytes(), b"data")
            self.assertEqual(workload.stats["bytes_downloaded"], 4)
            self.assertEqual(index[-1]["relative_path"], "Docs/report.txt")

    def test_user_folder_collision_uses_target_id_and_restore_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "backup_current"
            first = backup / "same_at_example.com"
            first.mkdir(parents=True)
            (first / "_user_metadata.json").write_text(json.dumps({"user_id": "user-a", "email": "same@example.com"}))
            workload = OneDriveWorkload(TENANT, backup_root=tmp)
            second = workload._user_backup_dir({"id": "user-b", "email": "same@example.com"}, backup)
            self.assertNotEqual(first, second)
            second.mkdir()
            (second / "_user_metadata.json").write_text(json.dumps({"email": "actual@example.com"}))
            restore = OneDriveRestore(TENANT, str(backup))
            restore._resolve_user_drive = MagicMock(return_value=("user-id", "drive-id"))
            restore._restore_user_drive = MagicMock()
            result = restore.restore()
            self.assertEqual(result["targets_processed"], 2)
            self.assertIn("actual@example.com", {item["backup_source"] for item in result["destination_plan"]})

    def test_onedrive_creates_nested_folders_and_encodes_file_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            restore = OneDriveRestore(TENANT, tmp, mode="new_location")
            restore._get = MagicMock(side_effect=not_found())
            restore._post = MagicMock(side_effect=[{"id": "parent"}, {"id": "child"}])
            restore._ensure_remote_folder("drive-1", "Restore/Quarter 1")
            self.assertEqual(restore._post.call_args_list[0].args[0],
                             f"{restore.GRAPH}/drives/drive-1/root/children")
            self.assertEqual(restore._post.call_args_list[1].args[0],
                             f"{restore.GRAPH}/drives/drive-1/items/parent/children")
            source = Path(tmp) / "report #1.txt"
            source.write_bytes(b"data")
            restore._put = MagicMock(return_value={"size": 4})
            restore._upload_file(source, "drive-1", "Restore/Quarter 1")
            self.assertIn("Quarter%201/report%20%231.txt", restore._put.call_args.args[0])
            self.assertEqual(restore.stats["items_processed"], 1)

    def test_outlook_restores_saved_attachment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup = root / "backup_current"
            backup.mkdir()
            source = backup / "user" / "_attachments" / "file.txt"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"content")
            restore = OutlookRestore(TENANT, str(backup))
            restore._post = MagicMock(return_value={"id": "attached"})
            restore._restore_attachment("user-id", "message-id", {
                "name": "file.txt", "saved_path": str(source.relative_to(root)),
            })
            body = restore._post.call_args.kwargs["json_body"]
            self.assertEqual(base64.b64decode(body["contentBytes"]), b"content")
            with self.assertRaises(ValueError):
                restore._restore_attachment("user-id", "message-id", {
                    "name": "outside", "saved_path": "../outside",
                })

    def test_outlook_skips_only_known_unavailable_mailbox(self):
        response = requests.Response()
        response.status_code = 404
        response._content = b'{"error":{"code":"MailboxNotEnabledForRESTAPI"}}'
        self.assertTrue(OutlookWorkload._is_unavailable_mailbox_error(requests.HTTPError(response=response)))
        response._content = b'{"error":{"code":"ItemNotFound"}}'
        self.assertFalse(OutlookWorkload._is_unavailable_mailbox_error(requests.HTTPError(response=response)))

    def test_outlook_message_restore_posts_attachment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup = root / "backup_current"
            user_dir = backup / "user"
            source = user_dir / "_attachments" / "file.txt"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"data")
            (user_dir / "messages.json").write_text(json.dumps([
                {"id": "old-message", "subject": "Hello", "body": {"contentType": "text", "content": "Hi"}, "hasAttachments": True}
            ]))
            (user_dir / "_message_attachments.json").write_text(json.dumps([
                {"message_id": "old-message", "name": "file.txt", "saved_path": str(source.relative_to(root))}
            ]))
            restore = OutlookRestore(TENANT, str(backup))
            restore._ensure_mail_folder = lambda *args: "folder-1"
            restore._post = MagicMock(side_effect=[{"id": "new-message"}, {"id": "new-attachment"}])
            restore._restore_messages(user_dir, "user-id")
            self.assertEqual(restore.stats["items_processed"], 1)
            self.assertEqual(restore._post.call_count, 2)

    def test_outlook_messages_stream_to_jsonl_and_restore_reads_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_dir = Path(tmp) / "user"
            user_dir.mkdir()
            workload = OutlookWorkload(TENANT, backup_root=tmp)
            workload._paginate = MagicMock(return_value=iter([
                {"id": "m1", "subject": "First"}, {"id": "m2", "subject": "Second"},
            ]))
            workload._backup_messages("user-1", user_dir, [{"id": "folder-1", "path": "Inbox"}], [])
            self.assertFalse((user_dir / "messages.json").exists())
            self.assertEqual([m["id"] for m in OutlookRestore._iter_backup_messages(user_dir)], ["m1", "m2"])
            self.assertEqual(workload.stats["files_downloaded"], 2)

    def test_teams_export_carries_channel_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup = root / "backup_current"
            files = backup / "Team A" / "channels" / "General" / "files"
            files.mkdir(parents=True)
            (files / "report.txt").write_text("report")
            (files.parent / "messages.json").write_text("[]")
            export = root / "export"
            result = TeamsRestore(TENANT, str(backup), export_format="json", export_dir=str(export)).restore()
            self.assertEqual(result["targets_processed"], 1)
            self.assertEqual((export / "Team A" / "files" / "General" / "report.txt").read_text(), "report")

    def test_site_estimate_api_reports_cached_error(self):
        web.app.testing = True
        try:
            config = {"sites": [{"name": "Site A", "path": "sites/a", "enabled": True,
                                 "size_estimate_error": {"message": "Site not found"}}]}
            with patch.object(web, "load_config", return_value=config):
                result = web.app.test_client().get("/api/sites/estimates")
            self.assertEqual(result.json["sites"][0]["status"], "error")
            self.assertIn("not found", result.json["sites"][0]["error"])
        finally:
            web.app.testing = False

    def test_teams_export_is_served_only_from_job_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            export = root / "teams-exports" / "run-1"
            export.mkdir(parents=True)
            (export / "index.html").write_text("<h1>Archive</h1>")
            job = {"id": "job-1", "workload": "teams", "status": "completed",
                   "result": {"export_dir": str(export)}}
            fake_registry = MagicMock(legacy_root=root, tenant_root=root / "m365")
            web.app.testing = True
            try:
                with patch("app.main_routes_v13.BackupRegistry", return_value=fake_registry), \
                     patch.object(RestoreManagerV2, "get_job", return_value=job):
                    client = web.app.test_client()
                    ok = client.get("/api/v2/restore/jobs/job-1/export/index.html")
                    missing = client.get("/api/v2/restore/jobs/job-1/export/missing.html")
                self.assertEqual(ok.status_code, 200)
                self.assertIn(b"Archive", ok.data)
                ok.close()
                self.assertEqual(missing.status_code, 404)
            finally:
                web.app.testing = False

    def test_restore_preflight_requires_write_roles(self):
        payload = base64.urlsafe_b64encode(json.dumps({"roles": ["Mail.Read"]}).encode()).decode().rstrip("=")
        runner = MagicMock()
        runner.get_token.return_value = f"header.{payload}.signature"
        with patch("app.restore_manager_v2.get_workload", return_value=runner):
            result = RestoreManagerV2()._check_restore_permission("outlook", TENANT, ["messages"])
        self.assertFalse(result["ready"])
        self.assertIn("Mail.ReadWrite", result["missing_scopes"])
        self.assertTrue(RestoreManagerV2()._check_restore_permission("teams", TENANT)["ready"])


if __name__ == "__main__":
    unittest.main()
