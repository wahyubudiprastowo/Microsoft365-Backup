import io
import contextlib
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import audit_publication
from audit_publication import prohibited_path, scan_payload


class PublicationSecurityTests(unittest.TestCase):
    def test_runtime_files_and_archives_are_blocked(self):
        for name in ("config/config.json", "service/.env", "service/.env.production",
                     "config/config.old.json", "logs/run.json", "data/document.txt",
                     "patch/old.zip", "auth.pfx", "token_cache.json", "data/.gitkeep",
                     "app-source/main.py", "runtime.log.1"):
            with self.subTest(name=name):
                self.assertTrue(prohibited_path(name))

    def test_public_templates_and_media_are_allowed(self):
        for name in ("spo-backup-final/config.example.json", "spo-backup-final/.env.example",
                     "docs/assets/dashboard.png", "docs/assets/product-tour.mp4"):
            with self.subTest(name=name):
                self.assertFalse(prohibited_path(name))

    def test_secret_report_contains_location_without_value(self):
        findings = set()
        scan_payload("example.txt", b"fixture-secret-value", {b"fixture-secret-value": "local-secret"}, findings)
        self.assertEqual(findings, {("local-secret", "example.txt")})
        self.assertNotIn("fixture-secret-value", repr(findings))

    def test_nested_zip_secrets_are_found(self):
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as archive:
            archive.writestr("config.txt", "fixture-secret-value")
        outer = io.BytesIO()
        with zipfile.ZipFile(outer, "w") as archive:
            archive.writestr("nested.zip", inner.getvalue())
        findings = set()
        scan_payload("old.zip", outer.getvalue(), {b"fixture-secret-value": "local-secret"}, findings)
        self.assertIn(("local-secret", "old.zip!nested.zip!config.txt"), findings)

    def test_private_key_is_detected(self):
        findings = set()
        header = b"-----BEGIN " + b"OPENSSH PRIVATE KEY-----"
        scan_payload("key.txt", header, {}, findings)
        self.assertEqual(findings, {("private-key", "key.txt")})

    def test_private_organization_reference_is_case_insensitive(self):
        findings = set()
        scan_payload("metadata", b"Example-Private-Brand", {}, findings,
                     deny_terms=(b"example-private-brand",))
        self.assertEqual(findings, {("organization-reference", "metadata")})

    def test_publication_ref_excludes_private_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.check_output(["git", "-C", directory, "-c", "user.name=Test Contributor",
                                                "-c", "user.email=contributor@example.com",
                                                "-c", "commit.gpgsign=false", *args], stderr=subprocess.DEVNULL)
            git("init", "-q")
            (root / "fixture.log").write_text("Private operational fixture")
            git("add", "fixture.log")
            git("commit", "-qm", "Private history fixture")
            git("branch", "private-fixture")
            git("checkout", "--orphan", "public-fixture")
            git("rm", "-f", "--ignore-unmatch", "fixture.log")
            (root / "README.md").write_text("Public documentation")
            git("add", "README.md")
            git("commit", "-qm", "Public root fixture")
            output = io.StringIO()
            with patch.object(audit_publication, "ROOT", root), \
                 patch("sys.argv", ["audit_publication.py", "--history-ref", "HEAD"]), \
                 contextlib.redirect_stdout(output):
                result = audit_publication.main()
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.getvalue())["findings"], [])
            output = io.StringIO()
            with patch.object(audit_publication, "ROOT", root), \
                 patch("sys.argv", ["audit_publication.py", "--history"]), \
                 contextlib.redirect_stdout(output):
                result = audit_publication.main()
            self.assertEqual(result, 1)
            self.assertTrue(any(item["type"] == "historical-prohibited-path"
                                for item in json.loads(output.getvalue())["findings"]))

    def test_blocked_snapshot_does_not_copy_private_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            subprocess.check_call(["git", "-C", str(root), "init", "-q"])
            (root / "credentials.json").write_text('{"password":"synthetic-fixture"}')
            snapshot = Path(directory) / "snapshot"
            with patch.object(audit_publication, "ROOT", root), \
                 patch("sys.argv", ["audit_publication.py", "--snapshot", str(snapshot)]), \
                 contextlib.redirect_stdout(io.StringIO()):
                result = audit_publication.main()
            self.assertEqual(result, 1)
            self.assertFalse(snapshot.exists())


if __name__ == "__main__":
    unittest.main()
