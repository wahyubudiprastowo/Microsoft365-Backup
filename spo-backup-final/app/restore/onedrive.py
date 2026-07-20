"""OneDrive restore implementation."""
from datetime import datetime, timezone
from pathlib import Path

from app.task_control import PauseException
from app.restore.base import BaseRestore

import logging

log = logging.getLogger("spo_backup")


class OneDriveRestore(BaseRestore):
    WORKLOAD_NAME = "onedrive"

    def __init__(self, tenant, backup_path, user_mapping=None, target_folder="Restored", **kwargs):
        super().__init__(tenant, backup_path, **kwargs)
        self.user_mapping = user_mapping or {}
        self.target_folder = target_folder.strip("/")

    def _resolve_user_drive(self, user_email: str):
        user = self._get(f"{self.GRAPH}/users/{user_email}")
        drive = self._get(f"{self.GRAPH}/users/{user['id']}/drive")
        return user["id"], drive["id"]

    def restore(self) -> dict:
        self.stats["start_time"] = datetime.now(timezone.utc).isoformat()
        self.emit("restore_start", {"backup_path": str(self.backup_path)})
        try:
            for user_dir in self.backup_path.iterdir():
                if not user_dir.is_dir() or user_dir.name.startswith("_"):
                    continue
                self._check_control()
                backup_email = user_dir.name.replace("_at_", "@")
                target_email = self.user_mapping.get(backup_email, backup_email)
                self.emit("target_start", {"target_name": target_email, "backup_source": backup_email})
                try:
                    _, drive_id = self._resolve_user_drive(target_email)
                    self._restore_user_drive(user_dir, drive_id)
                    self.stats["targets_processed"] += 1
                    self.emit("target_done", {"target_name": target_email, "status": "success"})
                except PauseException:
                    raise
                except Exception as e:
                    self.stats["targets_failed"] += 1
                    self.stats["errors"].append(f"[{target_email}] {e}")
                    self.emit("target_done", {"target_name": target_email, "status": "failed", "error": str(e)})
        except PauseException:
            self.stats["cancelled"] = True
        except Exception as e:
            log.error(f"OneDrive restore failed: {e}", exc_info=True)
            self.stats["errors"].append(f"Fatal: {e}")
        self.stats["end_time"] = datetime.now(timezone.utc).isoformat()
        self.emit("restore_done")
        return self.stats

    def _restore_user_drive(self, user_dir: Path, drive_id: str):
        base_path = self.target_folder if self.mode == "new_location" else ""
        if base_path:
            try:
                self._post(
                    f"{self.GRAPH}/drives/{drive_id}/root/children",
                    json_body={"name": base_path, "folder": {}, "@microsoft.graph.conflictBehavior": "replace"},
                )
            except Exception:
                pass
        self._upload_folder(user_dir, drive_id, base_path)

    def _upload_folder(self, local_dir: Path, drive_id: str, remote_path: str):
        for item in local_dir.iterdir():
            self._check_control()
            if item.name.startswith("_") or item.name.startswith("."):
                continue
            if item.is_dir():
                sub = f"{remote_path}/{item.name}" if remote_path else item.name
                self._upload_folder(item, drive_id, sub)
            elif item.is_file():
                self._upload_file(item, drive_id, remote_path)

    def _upload_file(self, local_file: Path, drive_id: str, remote_path: str):
        remote_full = f"{remote_path}/{local_file.name}" if remote_path else local_file.name
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
            self.emit("file_uploaded", {"current_file": local_file.name})
        except PauseException:
            raise
        except Exception as e:
            self.stats["items_failed"] += 1
            self.stats["errors"].append(f"{local_file.name}: {str(e)[:120]}")

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
                    headers={"Content-Length": str(len(chunk)), "Content-Range": f"bytes {start}-{end}/{total}"},
                    timeout=300,
                )
                response.raise_for_status()
                start = end + 1

    def dry_run(self) -> dict:
        result = {"workload": "onedrive", "users_in_backup": 0, "files_to_upload": 0, "total_size_bytes": 0}
        for user_dir in self.backup_path.iterdir():
            if not user_dir.is_dir() or user_dir.name.startswith("_"):
                continue
            result["users_in_backup"] += 1
            for item in user_dir.rglob("*"):
                if item.is_file() and not item.name.startswith("_"):
                    result["files_to_upload"] += 1
                    result["total_size_bytes"] += item.stat().st_size
        return result
