"""
Multi-Protocol Remote Upload Module
Supports: SMB/CIFS, FTP, FTPS, SFTP/SSH, WebDAV, rsync
"""
import os
import logging
import time
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("spo_backup")


class BaseUploader:
    """Base class for protocol uploaders."""
    protocol = "base"

    def __init__(self, config: dict):
        self.config = config

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
            shares = [s.name for s in conn.listShares() if not s.isSpecial]
            conn.close()
            return {"status": "ok", "message": f"Connected. Shares: {', '.join(shares[:5])}"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def upload_directory(self, local_dir, remote_dir=None, progress_callback=None):
        remote_dir = remote_dir or self.config.get("remote_path", "/")
        share = self.config["share"]
        stats = {"uploaded": 0, "failed": 0, "bytes": 0, "errors": []}
        conn = self._connect()
        try:
            local_path = Path(local_dir)
            for fp in local_path.rglob("*"):
                if fp.is_file():
                    rel = fp.relative_to(local_path)
                    remote_path = os.path.join(remote_dir, str(rel)).replace("\\", "/")
                    try:
                        # Create intermediate directories
                        parts = remote_path.split("/")
                        for i in range(1, len(parts)):
                            d = "/".join(parts[:i])
                            if d:
                                try:
                                    conn.createDirectory(share, d)
                                except Exception:
                                    pass
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
            cwd = ftp.pwd()
            ftp.quit()
            return {"status": "ok", "message": f"Connected. Current dir: {cwd}"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def upload_directory(self, local_dir, remote_dir=None, progress_callback=None):
        remote_dir = remote_dir or self.config.get("remote_path", "/")
        stats = {"uploaded": 0, "failed": 0, "bytes": 0, "errors": []}
        ftp = self._connect()
        try:
            local_path = Path(local_dir)
            for fp in local_path.rglob("*"):
                if fp.is_file():
                    rel = fp.relative_to(local_path)
                    remote_file = os.path.join(remote_dir, str(rel)).replace("\\", "/")
                    try:
                        # Create remote directories
                        parts = remote_file.split("/")
                        for i in range(1, len(parts)):
                            d = "/".join(parts[:i])
                            if d:
                                try:
                                    ftp.mkd(d)
                                except Exception:
                                    pass
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
            sftp.close()
            client.close()
            return {"status": "ok", "message": f"Connected. Home: {home}"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

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
        remote_dir = remote_dir or self.config.get("remote_path", "/tmp/backup")
        stats = {"uploaded": 0, "failed": 0, "bytes": 0, "errors": []}
        client, sftp = self._connect()
        try:
            local_path = Path(local_dir)
            for fp in local_path.rglob("*"):
                if fp.is_file():
                    rel = fp.relative_to(local_path)
                    remote_file = os.path.join(remote_dir, str(rel)).replace("\\", "/")
                    try:
                        self._sftp_mkdir_p(sftp, os.path.dirname(remote_file))
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
            r = self.requests.request("PROPFIND", self.base_url, auth=self.auth, timeout=10)
            if r.status_code in (200, 207, 301, 302):
                return {"status": "ok", "message": "WebDAV reachable"}
            return {"status": "error", "message": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def _mkcol(self, path):
        """Create remote collection (directory)."""
        url = self.base_url + path.strip("/")
        try:
            self.requests.request("MKCOL", url, auth=self.auth, timeout=10)
        except Exception:
            pass

    def upload_directory(self, local_dir, remote_dir=None, progress_callback=None):
        remote_dir = remote_dir or self.config.get("remote_path", "")
        stats = {"uploaded": 0, "failed": 0, "bytes": 0, "errors": []}
        local_path = Path(local_dir)
        created_dirs = set()

        for fp in local_path.rglob("*"):
            if fp.is_file():
                rel = fp.relative_to(local_path)
                remote_file = os.path.join(remote_dir, str(rel)).replace("\\", "/").strip("/")
                # Create intermediate directories
                parts = remote_file.split("/")
                for i in range(1, len(parts)):
                    d = "/".join(parts[:i])
                    if d and d not in created_dirs:
                        self._mkcol(d)
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
        return uploader.test_connection()
    except Exception as e:
        return {"status": "error", "message": str(e)}


def upload_to_remote(dest: dict, local_dir: str, progress_callback=None) -> dict:
    """Upload local_dir to a configured remote destination."""
    protocol = dest.get("protocol", "").lower()
    uploader = get_uploader(protocol, dest.get("config", {}))
    return uploader.upload_directory(local_dir, progress_callback=progress_callback)
