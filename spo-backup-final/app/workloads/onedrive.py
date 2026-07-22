"""OneDrive workload backup and target discovery."""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from app.task_control import PauseException, check_control
from app.workloads.base import BaseWorkload

log = logging.getLogger("spo_backup")


class OneDriveWorkload(BaseWorkload):
    workload_type = "onedrive"

    def __init__(self, tenant_config, backup_root=None, progress_callback=None, task_id=None):
        super().__init__(tenant_config)
        self.backup_root = backup_root
        self.progress_callback = progress_callback
        self.task_id = task_id
        self.stats = {
            "workload": "onedrive",
            "start_time": None,
            "end_time": None,
            "backup_path": None,
            "users_count": 0,
            "targets_available": 0,
            "targets_in_scope": 0,
            "selection_mode": "all",
            "targets_processed": 0,
            "targets_failed": 0,
            "files_downloaded": 0,
            "bytes_downloaded": 0,
            "errors": [],
            "cancelled": False,
        }

    def list_targets(self):
        users = []
        try:
            for user in self._paginate(f"{self.GRAPH}/users?$select=id,displayName,userPrincipalName,mail"):
                users.append({
                    "id": user["id"],
                    "name": user.get("displayName", ""),
                    "email": user.get("userPrincipalName") or user.get("mail", ""),
                    "type": "user_onedrive",
                })
        except Exception as e:
            return [{"error": str(e)}]
        return users

    def backup(self):
        if not self.backup_root:
            raise ValueError("backup_root is required for OneDrive backup")
        self.stats["start_time"] = datetime.now(timezone.utc).isoformat()
        backup_path = Path(self.backup_root) / f"backup_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        backup_path.mkdir(parents=True, exist_ok=True)
        self.stats["backup_path"] = str(backup_path)
        self._emit("backup_start", {"backup_path": str(backup_path)})

        try:
            users = self.list_targets()
            if users and users[0].get("error"):
                raise Exception(users[0]["error"])
            users, selection_info = self.apply_target_selection(users)
            self.stats["selection_mode"] = selection_info["mode"]
            self.stats["targets_available"] = selection_info["available_count"]
            self.stats["targets_in_scope"] = selection_info["effective_count"]
            self.stats["users_count"] = selection_info["effective_count"]
            for idx, user in enumerate(users, start=1):
                self._check_control()
                target_name = user.get("email") or user.get("name") or user.get("id")
                self._emit("target_start", {
                    "target_name": target_name,
                    "target_idx": idx,
                    "target_total": len(users),
                })
                try:
                    self._backup_user(user, backup_path)
                    self.stats["targets_processed"] += 1
                    self._emit("target_done", {"target_name": target_name, "status": "success"})
                except PauseException:
                    raise
                except Exception as e:
                    self.stats["targets_failed"] += 1
                    self.stats["errors"].append(f"[{target_name}] {str(e)[:200]}")
                    self._emit("target_done", {"target_name": target_name, "status": "failed", "error": str(e)})
            self._save_manifest(backup_path)
        except PauseException:
            self.stats["cancelled"] = True
        except Exception as e:
            log.error(f"OneDrive backup failed: {e}", exc_info=True)
            self.stats["errors"].append(f"Fatal: {e}")

        self.stats["end_time"] = datetime.now(timezone.utc).isoformat()
        self._emit("workload_done", {"backup_path": str(backup_path)})
        return self.stats

    def _backup_user(self, user: dict, backup_path: Path):
        target_name = user.get("email") or user.get("name") or user.get("id")
        drive = self._get(f"{self.GRAPH}/users/{user['id']}/drive")
        user_dir = backup_path / self._safe_user_dir(target_name)
        user_dir.mkdir(parents=True, exist_ok=True)

        self._write_json(user_dir / "_user_metadata.json", {
            "user_id": user.get("id"),
            "display_name": user.get("name"),
            "email": user.get("email"),
            "drive_id": drive.get("id"),
            "drive_type": drive.get("driveType"),
            "web_url": drive.get("webUrl"),
            "backup_time": datetime.now(timezone.utc).isoformat(),
        })

        files_index = []
        self._walk_drive(drive["id"], user_dir, files_index)
        self._write_json(user_dir / "_files_index.json", {
            "user": target_name,
            "drive_id": drive.get("id"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "items": files_index,
        })

    def _walk_drive(self, drive_id: str, dest_dir: Path, files_index: list, item_id: str = None, relative_path: str = ""):
        self._check_control()
        if item_id:
            url = f"{self.GRAPH}/drives/{drive_id}/items/{item_id}/children"
        else:
            url = f"{self.GRAPH}/drives/{drive_id}/root/children"

        for item in self._paginate(url, params={"$top": 200}):
            self._check_control()
            name = item.get("name") or item.get("id", "unnamed")
            child_relative = f"{relative_path}/{name}" if relative_path else name
            entry = {
                "id": item.get("id"),
                "name": name,
                "relative_path": child_relative,
                "size": item.get("size", 0),
                "web_url": item.get("webUrl"),
                "last_modified": item.get("lastModifiedDateTime"),
                "is_folder": "folder" in item,
            }
            files_index.append(entry)

            if "folder" in item:
                target_dir = dest_dir / self._safe_path_segment(name)
                target_dir.mkdir(parents=True, exist_ok=True)
                self._walk_drive(drive_id, target_dir, files_index, item_id=item["id"], relative_path=child_relative)
                continue

            download_bytes = self._download(
                f"{self.GRAPH}/drives/{drive_id}/items/{item['id']}/content",
                str(dest_dir / self._safe_path_segment(name)),
                item.get("size", 0),
            )
            self.stats["files_downloaded"] += 1
            self.stats["bytes_downloaded"] += download_bytes
            self._emit("file_done", {"current_file": child_relative, "target_name": dest_dir.name})

    def _save_manifest(self, backup_path: Path):
        self._write_json(backup_path / "_workload_manifest.json", {
            "workload": "onedrive",
            "tenant_name": self.tenant.get("name"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "users_count": self.stats["users_count"],
            "targets_available": self.stats["targets_available"],
            "targets_in_scope": self.stats["targets_in_scope"],
            "selection_mode": self.stats["selection_mode"],
            "targets_processed": self.stats["targets_processed"],
            "targets_failed": self.stats["targets_failed"],
            "files_downloaded": self.stats["files_downloaded"],
            "bytes_downloaded": self.stats["bytes_downloaded"],
            "errors": self.stats["errors"][:20],
        })

    def _safe_user_dir(self, value: str) -> str:
        value = (value or "unknown-user").strip()
        return value.replace("@", "_at_").replace("/", "_").replace("\\", "_")

    def _safe_path_segment(self, value: str) -> str:
        return (value or "unnamed").replace("/", "_").replace("\\", "_").strip() or "unnamed"

    def _write_json(self, path: Path, payload: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as handle:
            json.dump(payload, handle, indent=2, default=str)

    def _check_control(self):
        if self.task_id:
            check_control(self.task_id)

    def _emit(self, event: str, data: dict):
        if self.progress_callback:
            payload = dict(self.stats)
            payload.update(data or {})
            self.progress_callback(event, payload)
