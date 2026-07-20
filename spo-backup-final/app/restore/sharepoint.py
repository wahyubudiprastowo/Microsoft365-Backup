"""SharePoint restore implementation."""
from datetime import datetime, timezone
from pathlib import Path

from app.task_control import PauseException
from app.restore.base import BaseRestore

import logging

log = logging.getLogger("spo_backup")


class SharePointRestore(BaseRestore):
    WORKLOAD_NAME = "sharepoint"

    def __init__(self, tenant, backup_path, target_site_id=None, target_site_path=None, **kwargs):
        super().__init__(tenant, backup_path, **kwargs)
        self.target_site_id = target_site_id
        self.target_site_path = target_site_path

    def _resolve_target_site(self) -> str:
        if self.target_site_id:
            return self.target_site_id
        if self.target_site_path:
            host = self.tenant.get("sharepoint_host")
            data = self._get(f"{self.GRAPH}/sites/{host}:/{self.target_site_path}")
            return data["id"]
        raise Exception("No target site specified")

    def restore(self) -> dict:
        self.stats["start_time"] = datetime.now(timezone.utc).isoformat()
        self.emit("restore_start", {"backup_path": str(self.backup_path)})
        try:
            target_site_id = self._resolve_target_site()
            drives_data = self._get(f"{self.GRAPH}/sites/{target_site_id}/drives")
            target_drives = {drive["name"]: drive["id"] for drive in drives_data.get("value", [])}
            if not target_drives:
                raise Exception("Target site has no document libraries")

            for site_dir in self.backup_path.iterdir():
                if not site_dir.is_dir() or site_dir.name.startswith("_"):
                    continue
                self._check_control()
                self.emit("target_start", {"target_name": site_dir.name})
                try:
                    self._restore_site_dir(site_dir, target_drives)
                    self.stats["targets_processed"] += 1
                    self.emit("target_done", {"target_name": site_dir.name, "status": "success"})
                except PauseException:
                    raise
                except Exception as e:
                    self.stats["targets_failed"] += 1
                    self.stats["errors"].append(f"[{site_dir.name}] {e}")
                    self.emit("target_done", {"target_name": site_dir.name, "status": "failed", "error": str(e)})
        except PauseException:
            self.stats["cancelled"] = True
        except Exception as e:
            log.error(f"SharePoint restore failed: {e}", exc_info=True)
            self.stats["errors"].append(f"Fatal: {e}")

        self.stats["end_time"] = datetime.now(timezone.utc).isoformat()
        self.emit("restore_done")
        return self.stats

    def _restore_site_dir(self, site_dir: Path, target_drives: dict):
        for lib_dir in site_dir.iterdir():
            if not lib_dir.is_dir() or lib_dir.name.startswith("_"):
                continue
            drive_id = target_drives.get(lib_dir.name) or target_drives.get("Documents") or list(target_drives.values())[0]
            self._upload_folder(lib_dir, drive_id, "")

    def _upload_folder(self, local_dir: Path, drive_id: str, remote_path: str):
        for item in local_dir.iterdir():
            self._check_control()
            if item.name.startswith("_") or item.name.startswith("."):
                continue
            if item.is_dir():
                sub_remote = f"{remote_path}/{item.name}" if remote_path else item.name
                self._upload_folder(item, drive_id, sub_remote)
            elif item.is_file():
                self._upload_file(item, drive_id, remote_path)

    def _upload_file(self, local_file: Path, drive_id: str, remote_path: str):
        file_name = local_file.name
        remote_full = f"{remote_path}/{file_name}" if remote_path else file_name
        if self.mode == "merge":
            try:
                existing = self._get(f"{self.GRAPH}/drives/{drive_id}/root:/{remote_full}")
                if existing.get("id"):
                    self.stats["items_skipped"] += 1
                    return
            except Exception:
                pass
        try:
            size = local_file.stat().st_size
            if size < 4 * 1024 * 1024:
                with open(local_file, "rb") as handle:
                    self._put(f"{self.GRAPH}/drives/{drive_id}/root:/{remote_full}:/content", data=handle)
            else:
                self._upload_large_file(local_file, drive_id, remote_full)
            self.stats["items_processed"] += 1
            self.stats["bytes_uploaded"] += size
            self.emit("file_uploaded", {"current_file": file_name})
        except PauseException:
            raise
        except Exception as e:
            self.stats["items_failed"] += 1
            self.stats["errors"].append(f"{file_name}: {str(e)[:120]}")

    def _upload_large_file(self, local_file: Path, drive_id: str, remote_path: str):
        session = self._post(
            f"{self.GRAPH}/drives/{drive_id}/root:/{remote_path}:/createUploadSession",
            json_body={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
        )
        upload_url = session["uploadUrl"]
        chunk_size = 5 * 1024 * 1024
        total = local_file.stat().st_size
        with open(local_file, "rb") as handle:
            start = 0
            while start < total:
                self._check_control()
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                end = start + len(chunk) - 1
                response = self.session.put(
                    upload_url,
                    data=chunk,
                    headers={
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {start}-{end}/{total}",
                    },
                    timeout=300,
                )
                response.raise_for_status()
                start = end + 1

    def dry_run(self) -> dict:
        result = {"workload": "sharepoint", "files_to_upload": 0, "total_size_bytes": 0}
        for site_dir in self.backup_path.iterdir():
            if not site_dir.is_dir() or site_dir.name.startswith("_"):
                continue
            for lib_dir in site_dir.iterdir():
                if not lib_dir.is_dir() or lib_dir.name.startswith("_"):
                    continue
                for item in lib_dir.rglob("*"):
                    if item.is_file() and not item.name.startswith("_"):
                        result["files_to_upload"] += 1
                        result["total_size_bytes"] += item.stat().st_size
        return result
