import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.main import _validate_remote_destination
from app.tasks import _remote_accepts_workload, _remote_upload_status
from app.uploader import BaseUploader, SMBUploader, FTPUploader, SFTPUploader, WebDAVUploader, upload_to_remote


class RemoteDestinationTests(unittest.TestCase):
    def test_checklist_validation_and_legacy_default(self):
        config = {"name": "NAS", "protocol": "ftp", "config": {
            "server": "nas.example", "username": "user", "password": "pass", "remote_path": "/backup",
        }}
        self.assertEqual(len(_validate_remote_destination(config)["workloads"]), 4)
        config["workloads"] = ["sharepoint", "sharepoint"]
        self.assertEqual(_validate_remote_destination(config)["workloads"], ["sharepoint"])
        config["workloads"] = []
        with self.assertRaises(ValueError):
            _validate_remote_destination(config)
        config["workloads"] = ["sharepoint"]
        config["config"]["remote_path"] = "/backup/../other"
        with self.assertRaises(ValueError):
            _validate_remote_destination(config)

    def test_workload_filter_and_partial_results(self):
        self.assertTrue(_remote_accepts_workload({}, "teams"))
        self.assertTrue(_remote_accepts_workload({"workloads": ["sharepoint"]}, "legacy"))
        self.assertFalse(_remote_accepts_workload({"workloads": ["sharepoint"]}, "outlook"))
        self.assertEqual(_remote_upload_status({"uploaded": 2, "failed": 1}), "partial")
        self.assertEqual(_remote_upload_status({"uploaded": 0, "failed": 1}), "failed")

    def test_remote_path_and_file_name_encoding(self):
        uploader = BaseUploader({"remote_path": "/backup"})
        with self.assertRaises(ValueError):
            uploader._join_remote_path("/backup", "../outside")
        webdav = WebDAVUploader({"url": "https://dav.example/files/", "username": "u", "password": "p"})
        self.assertEqual(webdav._build_url("folder/Quarterly report #1.xlsx"),
                         "https://dav.example/files/folder/Quarterly%20report%20%231.xlsx")

    def test_ftp_mkdir_rejects_permission_error(self):
        ftp = MagicMock()
        ftp.mkd.side_effect = PermissionError("denied")
        ftp.cwd.side_effect = PermissionError("denied")
        with self.assertRaises(PermissionError):
            FTPUploader({"remote_path": "/backup"})._mkdir_p(ftp, "/backup")

    def test_protocol_probes_write_a_file(self):
        smb = SMBUploader({"remote_path": "/backup", "share": "data"})
        connection = MagicMock()
        share = MagicMock()
        share.name = "data"
        share.isSpecial = False
        connection.listShares.return_value = [share]
        with patch.object(smb, "_connect", return_value=connection), patch.object(smb, "_mkdir_p"):
            self.assertEqual(smb.test_connection()["status"], "ok")
        connection.storeFile.assert_called_once()

        ftp = FTPUploader({"remote_path": "/backup"})
        connection = MagicMock()
        with patch.object(ftp, "_connect", return_value=connection), patch.object(ftp, "_mkdir_p"):
            self.assertEqual(ftp.test_connection()["status"], "ok")
        connection.storbinary.assert_called_once()

        sftp = SFTPUploader({"remote_path": "/backup"})
        channel = MagicMock()
        with patch.object(sftp, "_connect", return_value=(MagicMock(), channel)), patch.object(sftp, "_sftp_mkdir_p"):
            self.assertEqual(sftp.test_connection()["status"], "ok")
        channel.putfo.assert_called_once()

    def test_sftp_mkdir_rejects_permission_error(self):
        uploader = SFTPUploader({"remote_path": "/backup"})
        sftp = MagicMock()
        sftp.stat.side_effect = PermissionError("denied")
        with self.assertRaises(PermissionError):
            uploader._sftp_mkdir_p(sftp, "/backup")

    def test_sftp_rejects_wrong_host_fingerprint(self):
        uploader = SFTPUploader({"server": "ssh.example", "username": "u", "password": "p",
                                 "host_fingerprint": "SHA256:" + "A" * 43})
        client = MagicMock()
        client.get_transport.return_value.get_remote_server_key.return_value.asbytes.return_value = b"different-key"
        with patch.object(uploader.paramiko, "SSHClient", return_value=client):
            with self.assertRaises(uploader.paramiko.SSHException):
                uploader._connect()
        client.close.assert_called_once()

    def test_webdav_redirect_is_not_a_successful_upload(self):
        uploader = WebDAVUploader({"url": "https://dav.example/files/", "username": "u", "password": "p", "remote_path": "/backup"})
        with patch.object(uploader, "_mkcol_p"), tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "sample.txt").write_text("content")
            with patch.object(uploader, "_request", return_value=MagicMock(status_code=302)):
                result = uploader.upload_directory(tmp)
            self.assertEqual(result["uploaded"], 0)
            self.assertEqual(result["failed"], 1)

    def test_webdav_size_mismatch_is_reported(self):
        uploader = WebDAVUploader({"url": "https://dav.example/files/", "username": "u", "password": "p", "remote_path": "/backup"})
        with patch.object(uploader, "_mkcol_p"), tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "sample.txt").write_text("content")
            replies = [MagicMock(status_code=201), MagicMock(status_code=200, headers={"Content-Length": "2"})]
            with patch.object(uploader, "_request", side_effect=replies):
                result = uploader.upload_directory(tmp)
            self.assertEqual(result["failed"], 1)
            self.assertIn("expected 7", result["errors"][0])

    def test_webdav_probe_requires_actual_file_write(self):
        uploader = WebDAVUploader({"url": "https://dav.example/files/", "username": "u", "password": "p", "remote_path": "/backup"})
        responses = [MagicMock(status_code=207), MagicMock(status_code=201),
                     MagicMock(status_code=403), MagicMock(status_code=204)]
        with patch.object(uploader, "_mkcol_p"), patch.object(uploader, "_request", side_effect=responses):
            result = uploader.test_connection()
        self.assertEqual(result["status"], "error")
        self.assertIn("403", result["message"])

    def test_upload_to_remote_preserves_subpath(self):
        destination = {"protocol": "ftp", "config": {"remote_path": "/archive"}}
        with patch("app.uploader.FTPUploader.upload_directory", return_value={"uploaded": 1}) as upload:
            result = upload_to_remote(destination, "/tmp/source", remote_subpath="m365/sharepoint")
        self.assertEqual(upload.call_args.kwargs["remote_dir"], "/archive/m365/sharepoint")
        self.assertEqual(result["remote_path"], "/archive/m365/sharepoint")


if __name__ == "__main__":
    unittest.main()
