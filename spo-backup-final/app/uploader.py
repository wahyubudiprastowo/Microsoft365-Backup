"""
Multi-Protocol Remote Upload Module
Supports: SMB/CIFS, FTP, FTPS, SFTP/SSH, WebDAV, rsync
"""
import os
import logging
import time
from pathlib import Path
from posixpath import dirname as posix_dirname
from urllib.parse import urlparse

log = logging.getLogger("spo_backup")


class BaseUploader:
    """Base class for protocol uploaders."""
    protocol = "base"

    def __init__(self, config: dict):
        self.config = config

    def _normalize_remote_path(self, value: str | None = None) -> str:
        raw = str(self.config.get("remote_path", "/") if value is None else value).strip()
        raw = raw.replace("\\", "/")
        if not raw:
            return "/"
        leading = raw.startswith("/")
        parts = [part for part in raw.split("/") if part and part != "."]
        normalized = "/".join(parts)
        if not normalized:
            return "/"
        return f"/{normalized}" if leading else normalized

    def _join_remote_path(self, *parts: str) -> str:
        leading = False
        clean_parts = []
        for part in parts:
            if part is None:
                continue
            text = str(part).strip().replace("\\", "/")
            if not text:
                continue
            if text.startswith("/"):
                leading = True
            clean_parts.extend(item for item in text.split("/") if item and item != ".")
        if not clean_parts:
            return "/" if leading else ""
        joined = "/".join(clean_parts)
        return f"/{joined}" if leading else joined

    def _probe_name(self) -> str:
        return f".m365backup_probe_{int(time.time())}"

    def _result(self, status: str, message: str, **extra) -> dict:
        payload = {
            "status": status,
            "message": message,
            "protocol": self.protocol,
            "remote_path": self._normalize_remote_path(),
        }
        payload.update(extra)
        return payload

    def test_connection(self) -> dict:
        """Test if connection works. Returns {status, message}."""
        raise NotImplementedError

    def upload_directory(self, local_dir: str, remote_dir: str = None,
                         progress_callback=None) -> dict:
        """Upload a directory recursively. Returns stats."""
        raise NotImplementedError


# ════════════════════════════════════════════════════════════════
# SMB / CIFS UPLOADER
# ════════════════════════════════════════════════════════════════
class SMBUploader(BaseUploader):
    """
    SMB/CIFS upload via pysmb.
    Config: {
        "server": "192.168.1.100",
        "port": 445,
        "share": "backup",
        "username": "user",
        "password": "pass",
        "domain": "WORKGROUP",
        "remote_path": "/sharepoint-backups"
    }
    """
    protocol = "smb"

    def __init__(self, config):
        super().__init__(config)
        try:
            from smb.SMBConnection import SMBConnection
            self.SMBConnection = SMBConnection
        except ImportError:
            raise ImportError("pysmb not installed. Add 'pysmb' to requirements.txt")

    def _connect(self):
        conn = self.SMBConnection(
            self.config["username"],
            self.config["password"],
            "m365-backup-client",
            self.config.get("server", "server"),
            domain=self.config.get("domain", "WORKGROUP"),
            use_ntlm_v2=True,
        )
        if not conn.connect(self.config["server"], self.config.get("port", 445)):
            raise Exception("SMB connection failed")
        return conn

    def test_connection(self):
        try:
            conn = self._connect()
            share = self.config["share"]
            remote_path = self._normalize_remote_path()
            shares = [s.name for s in conn.listShares() if not s.isSpecial]
            if share not in shares:
                raise Exception(f"Configured share '{share}' not found on server")
            self._mkdir_p(conn, share, remote_path)
            probe_dir = self._join_remote_path(remote_path, self._probe_name())
            conn.createDirectory(share, probe_dir)
            conn.deleteDirectory(share, probe_dir)
            conn.close()
            return self._result(
                "ok",
                f"Connected. Share '{share}' is reachable and writable.",
                checks={"connectivity": True, "path_ready": True, "write_access": True},
                share=share,
            )
        except Exception as e:
            return self._result("error", str(e), checks={"connectivity": False, "path_ready": False, "write_access": False})

    def _mkdir_p(self, conn, share, remote_dir):
        normalized = self._normalize_remote_path(remote_dir)
        parts = [part for part in normalized.split("/") if part]
        current = ""
        for part in parts:
            current = self._join_remote_path(current, part)
            try:
                conn.createDirectory(share, current)
            except Exception:
                pass

    def upload_directory(self, local_dir, remote_dir=None, progress_callback=None):
        remote_dir = self._normalize_remote_path(remote_dir)
        share = self.config["share"]
        stats = {"uploaded": 0, "failed": 0, "bytes": 0, "errors": []}
        conn = self._connect()
        try:
            local_path = Path(local_dir)
            self._mkdir_p(conn, share, remote_dir)
            for fp in local_path.rglob("*"):
                if fp.is_file():
                    rel = fp.relative_to(local_path)
                    remote_path = self._join_remote_path(remote_dir, str(rel))
                    try:
                        self._mkdir_p(conn, share, posix_dirname(remote_path))
                        with open(fp, "rb") as f:
                            conn.storeFile(share, remote_path, f)
                        stats["uploaded"] += 1
                        stats["bytes"] += fp.stat().st_size
                        if progress_callback:
                            progress_callback({"current_file": fp.name, **stats})
                    except Exception as e:
                        stats["failed"] += 1
                        stats["errors"].append(f"{rel}: {e}")
        finally:
            conn.close()
        stats["remote_path"] = remote_dir
        return stats


# ════════════════════════════════════════════════════════════════
# FTP / FTPS UPLOADER
# ════════════════════════════════════════════════════════════════
class FTPUploader(BaseUploader):
    """
    FTP/FTPS upload via stdlib ftplib.
    Config: {
        "server": "ftp.example.com",
        "port": 21,
        "username": "user",
        "password": "pass",
        "use_tls": false,
        "passive": true,
        "remote_path": "/backups"
    }
    """
    protocol = "ftp"

    def _connect(self):
        from ftplib import FTP, FTP_TLS
        if self.config.get("use_tls"):
            ftp = FTP_TLS()
        else:
            ftp = FTP()
        ftp.connect(self.config["server"], self.config.get("port", 21), timeout=30)
        ftp.login(self.config["username"], self.config["password"])
        if self.config.get("use_tls"):
            ftp.prot_p()
        ftp.set_pasv(self.config.get("passive", True))
        return ftp

    def test_connection(self):
        try:
            ftp = self._connect()
            remote_path = self._normalize_remote_path()
            self._mkdir_p(ftp, remote_path)
            probe_dir = self._join_remote_path(remote_path, self._probe_name())
            ftp.mkd(probe_dir)
            ftp.rmd(probe_dir)
            cwd = ftp.pwd()
            ftp.quit()
            return self._result(
                "ok",
                f"Connected. FTP path '{remote_path}' is reachable and writable.",
                checks={"connectivity": True, "path_ready": True, "write_access": True},
                current_dir=cwd,
            )
        except Exception as e:
            return self._result("error", str(e), checks={"connectivity": False, "path_ready": False, "write_access": False})

    def _mkdir_p(self, ftp, remote_dir):
        normalized = self._normalize_remote_path(remote_dir)
        parts = [part for part in normalized.split("/") if part]
        current = ""
        for part in parts:
            current = self._join_remote_path(current, part)
            try:
                ftp.mkd(current)
            except Exception:
                pass

    def upload_directory(self, local_dir, remote_dir=None, progress_callback=None):
        remote_dir = self._normalize_remote_path(remote_dir)
        stats = {"uploaded": 0, "failed": 0, "bytes": 0, "errors": []}
        ftp = self._connect()
        try:
            local_path = Path(local_dir)
            self._mkdir_p(ftp, remote_dir)
            for fp in local_path.rglob("*"):
                if fp.is_file():
                    rel = fp.relative_to(local_path)
                    remote_file = self._join_remote_path(remote_dir, str(rel))
                    try:
                        self._mkdir_p(ftp, posix_dirname(remote_file))
                        with open(fp, "rb") as f:
                            ftp.storbinary(f"STOR {remote_file}", f)
                        stats["uploaded"] += 1
                        stats["bytes"] += fp.stat().st_size
                        if progress_callback:
                            progress_callback({"current_file": fp.name, **stats})
                    except Exception as e:
                        stats["failed"] += 1
                        stats["errors"].append(f"{rel}: {e}")
        finally:
            ftp.quit()
        stats["remote_path"] = remote_dir
        return stats


# ════════════════════════════════════════════════════════════════
# SFTP / SSH UPLOADER
# ════════════════════════════════════════════════════════════════
class SFTPUploader(BaseUploader):
    """
    SFTP upload via paramiko.
    Config: {
        "server": "ssh.example.com",
        "port": 22,
        "username": "user",
        "password": "pass",        // or use private_key_path
        "private_key_path": "/path/to/id_rsa",
        "remote_path": "/home/user/backups"
    }
    """
    protocol = "sftp"

    def __init__(self, config):
        super().__init__(config)
        try:
            import paramiko
            self.paramiko = paramiko
        except ImportError:
            raise ImportError("paramiko not installed. Add 'paramiko' to requirements.txt")

    def _connect(self):
        client = self.paramiko.SSHClient()
        client.set_missing_host_key_policy(self.paramiko.AutoAddPolicy())
        connect_kwargs = {
            "hostname": self.config["server"],
            "port": self.config.get("port", 22),
            "username": self.config["username"],
            "timeout": 30,
        }
        if self.config.get("private_key_path"):
            connect_kwargs["key_filename"] = self.config["private_key_path"]
        else:
            connect_kwargs["password"] = self.config.get("password", "")
        client.connect(**connect_kwargs)
        return client, client.open_sftp()

    def test_connection(self):
        try:
            client, sftp = self._connect()
            home = sftp.normalize(".")
            remote_path = self._normalize_remote_path()
            self._sftp_mkdir_p(sftp, remote_path)
            probe_dir = self._join_remote_path(remote_path, self._probe_name())
            sftp.mkdir(probe_dir)
            sftp.rmdir(probe_dir)
            sftp.close()
            client.close()
            return self._result(
                "ok",
                f"Connected. SFTP path '{remote_path}' is reachable and writable.",
                checks={"connectivity": True, "path_ready": True, "write_access": True},
                home=home,
            )
        except Exception as e:
            return self._result("error", str(e), checks={"connectivity": False, "path_ready": False, "write_access": False})

    def _sftp_mkdir_p(self, sftp, remote_dir):
        """Recursive mkdir on SFTP."""
        if remote_dir in ("/", ""):
            return
        try:
            sftp.stat(remote_dir)
        except IOError:
            parent = os.path.dirname(remote_dir)
            if parent and parent != remote_dir:
                self._sftp_mkdir_p(sftp, parent)
            try:
                sftp.mkdir(remote_dir)
            except Exception:
                pass

    def upload_directory(self, local_dir, remote_dir=None, progress_callback=None):
        remote_dir = self._normalize_remote_path(remote_dir or self.config.get("remote_path", "/tmp/backup"))
        stats = {"uploaded": 0, "failed": 0, "bytes": 0, "errors": []}
        client, sftp = self._connect()
        try:
            local_path = Path(local_dir)
            self._sftp_mkdir_p(sftp, remote_dir)
            for fp in local_path.rglob("*"):
                if fp.is_file():
                    rel = fp.relative_to(local_path)
                    remote_file = self._join_remote_path(remote_dir, str(rel))
                    try:
                        self._sftp_mkdir_p(sftp, posix_dirname(remote_file))
                        sftp.put(str(fp), remote_file)
                        stats["uploaded"] += 1
                        stats["bytes"] += fp.stat().st_size
                        if progress_callback:
                            progress_callback({"current_file": fp.name, **stats})
                    except Exception as e:
                        stats["failed"] += 1
                        stats["errors"].append(f"{rel}: {e}")
        finally:
            sftp.close()
            client.close()
        stats["remote_path"] = remote_dir
        return stats


# ════════════════════════════════════════════════════════════════
# WebDAV UPLOADER
# ════════════════════════════════════════════════════════════════
class WebDAVUploader(BaseUploader):
    """
    WebDAV upload via requests (no extra dep).
    Config: {
        "url": "https://nextcloud.example.com/remote.php/dav/files/user/",
        "username": "user",
        "password": "pass",
        "remote_path": "backups"
    }
    """
    protocol = "webdav"

    def __init__(self, config):
        super().__init__(config)
        import requests
        self.requests = requests
        self.auth = (config["username"], config["password"])
        self.base_url = config["url"].rstrip("/") + "/"

    def test_connection(self):
        try:
            remote_path = self._normalize_remote_path()
            target_url = self._build_url(remote_path)
            r = self.requests.request("PROPFIND", self.base_url, auth=self.auth, timeout=10)
            if r.status_code in (200, 207, 301, 302):
                self._mkcol_p(remote_path)
                probe_dir = self._join_remote_path(remote_path, self._probe_name())
                probe_url = self._build_url(probe_dir)
                self.requests.request("MKCOL", probe_url, auth=self.auth, timeout=10).raise_for_status()
                cleanup = self.requests.request("DELETE", probe_url, auth=self.auth, timeout=10)
                if cleanup.status_code not in (200, 202, 204):
                    raise Exception(f"Probe cleanup failed: HTTP {cleanup.status_code}")
                path_check = self.requests.request("PROPFIND", target_url, auth=self.auth, timeout=10)
                if path_check.status_code not in (200, 207, 301, 302):
                    raise Exception(f"Remote path check failed: HTTP {path_check.status_code}")
                return self._result(
                    "ok",
                    f"WebDAV path '{remote_path}' is reachable and writable.",
                    checks={"connectivity": True, "path_ready": True, "write_access": True},
                )
            return self._result("error", f"HTTP {r.status_code}", checks={"connectivity": False, "path_ready": False, "write_access": False})
        except Exception as e:
            return self._result("error", str(e), checks={"connectivity": False, "path_ready": False, "write_access": False})

    def _build_url(self, path):
        normalized = self._normalize_remote_path(path).strip("/")
        return self.base_url + normalized if normalized else self.base_url

    def _mkcol(self, path):
        url = self._build_url(path)
        response = self.requests.request("MKCOL", url, auth=self.auth, timeout=10)
        if response.status_code not in (200, 201, 204, 301, 302, 405):
            raise Exception(f"MKCOL failed for '{path}': HTTP {response.status_code}")

    def _mkcol_p(self, path):
        normalized = self._normalize_remote_path(path)
        current = ""
        for part in [piece for piece in normalized.split("/") if piece]:
            current = self._join_remote_path(current, part)
            try:
                self._mkcol(current)
            except Exception:
                pass

    def upload_directory(self, local_dir, remote_dir=None, progress_callback=None):
        remote_dir = self._normalize_remote_path(remote_dir or self.config.get("remote_path", ""))
        stats = {"uploaded": 0, "failed": 0, "bytes": 0, "errors": []}
        local_path = Path(local_dir)
        created_dirs = set()
        self._mkcol_p(remote_dir)

        for fp in local_path.rglob("*"):
            if fp.is_file():
                rel = fp.relative_to(local_path)
                remote_file = self._join_remote_path(remote_dir, str(rel)).strip("/")
                # Create intermediate directories
                parts = remote_file.split("/")
                for i in range(1, len(parts)):
                    d = "/".join(parts[:i])
                    if d and d not in created_dirs:
                        self._mkcol_p(d)
                        created_dirs.add(d)
                try:
                    url = self.base_url + remote_file
                    with open(fp, "rb") as f:
                        r = self.requests.put(url, data=f, auth=self.auth, timeout=120)
                        r.raise_for_status()
                    stats["uploaded"] += 1
                    stats["bytes"] += fp.stat().st_size
                    if progress_callback:
                        progress_callback({"current_file": fp.name, **stats})
                except Exception as e:
                    stats["failed"] += 1
                    stats["errors"].append(f"{rel}: {e}")
        stats["remote_path"] = remote_dir
        return stats


# ════════════════════════════════════════════════════════════════
# FACTORY
# ════════════════════════════════════════════════════════════════
UPLOADER_MAP = {
    "smb": SMBUploader,
    "ftp": FTPUploader,
    "sftp": SFTPUploader,
    "webdav": WebDAVUploader,
}


def get_uploader(protocol: str, config: dict) -> BaseUploader:
    """Factory function to get the right uploader."""
    cls = UPLOADER_MAP.get(protocol.lower())
    if not cls:
        raise ValueError(f"Unsupported protocol: {protocol}. Supported: {list(UPLOADER_MAP.keys())}")
    return cls(config)


def test_remote_destination(dest: dict) -> dict:
    """Test connection to a remote destination."""
    protocol = dest.get("protocol", "").lower()
    if not protocol:
        return {"status": "error", "message": "Protocol required"}
    try:
        uploader = get_uploader(protocol, dest.get("config", {}))
        result = uploader.test_connection()
        result.setdefault("destination_name", dest.get("name", "Unnamed"))
        result.setdefault("protocol", protocol)
        return result
    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
            "destination_name": dest.get("name", "Unnamed"),
            "protocol": protocol,
            "remote_path": str((dest.get("config") or {}).get("remote_path") or "/"),
            "checks": {"connectivity": False, "path_ready": False, "write_access": False},
        }


def upload_to_remote(dest: dict, local_dir: str, progress_callback=None, remote_subpath: str | None = None) -> dict:
    """Upload local_dir to a configured remote destination."""
    protocol = dest.get("protocol", "").lower()
    uploader = get_uploader(protocol, dest.get("config", {}))
    base_remote = uploader._normalize_remote_path()
    effective_remote = uploader._join_remote_path(base_remote, remote_subpath or "")
    result = uploader.upload_directory(local_dir, remote_dir=effective_remote, progress_callback=progress_callback)
    result.setdefault("remote_path", effective_remote)
    return result
