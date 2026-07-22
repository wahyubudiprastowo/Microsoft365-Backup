"""Outlook workload backup and target discovery."""
import base64
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from app.task_control import PauseException, check_control
from app.workloads.base import BaseWorkload

log = logging.getLogger("spo_backup")


class OutlookWorkload(BaseWorkload):
    workload_type = "outlook"

    def __init__(self, tenant_config, backup_root=None, progress_callback=None, task_id=None):
        super().__init__(tenant_config)
        self.backup_root = backup_root
        self.progress_callback = progress_callback
        self.task_id = task_id
        self.stats = {
            "workload": "outlook",
            "start_time": None,
            "end_time": None,
            "backup_path": None,
            "mailbox_count": 0,
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
                if user.get("mail") or user.get("userPrincipalName"):
                    users.append({
                        "id": user["id"],
                        "name": user.get("displayName", ""),
                        "email": user.get("userPrincipalName") or user.get("mail", ""),
                        "type": "user_mailbox",
                    })
        except Exception as e:
            return [{"error": str(e)}]
        return users

    def backup(self):
        if not self.backup_root:
            raise ValueError("backup_root is required for Outlook backup")
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
            self.stats["mailbox_count"] = selection_info["effective_count"]
            for idx, user in enumerate(users, start=1):
                self._check_control()
                target_name = user.get("email") or user.get("name") or user.get("id")
                self._emit("target_start", {
                    "target_name": target_name,
                    "target_idx": idx,
                    "target_total": len(users),
                })
                try:
                    self._backup_mailbox(user, backup_path)
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
            log.error(f"Outlook backup failed: {e}", exc_info=True)
            self.stats["errors"].append(f"Fatal: {e}")

        self.stats["end_time"] = datetime.now(timezone.utc).isoformat()
        self._emit("workload_done", {"backup_path": str(backup_path)})
        return self.stats

    def _backup_mailbox(self, user: dict, backup_path: Path):
        target_name = user.get("email") or user.get("name") or user.get("id")
        user_dir = backup_path / self._safe_user_dir(target_name)
        user_dir.mkdir(parents=True, exist_ok=True)

        self._write_json(user_dir / "_user_metadata.json", {
            "user_id": user.get("id"),
            "display_name": user.get("name"),
            "email": user.get("email"),
            "backup_time": datetime.now(timezone.utc).isoformat(),
        })

        folders = self._list_mail_folders(user["id"])
        self._write_json(user_dir / "_mail_folders.json", folders)

        attachment_manifest = []
        messages = self._backup_messages(user["id"], user_dir, folders, attachment_manifest)
        calendar_items = self._backup_collection(
            f"{self.GRAPH}/users/{user['id']}/events",
            params={"$top": 100},
        )
        contacts = self._backup_collection(
            f"{self.GRAPH}/users/{user['id']}/contacts",
            params={"$top": 200},
        )

        self._write_json(user_dir / "messages.json", messages)
        self._write_json(user_dir / "calendar.json", calendar_items)
        self._write_json(user_dir / "contacts.json", contacts)
        self._write_json(user_dir / "_message_attachments.json", attachment_manifest)

    def _list_mail_folders(self, user_id: str, parent_folder_id: str = None, prefix: str = "") -> list:
        if parent_folder_id:
            url = f"{self.GRAPH}/users/{user_id}/mailFolders/{parent_folder_id}/childFolders"
        else:
            url = f"{self.GRAPH}/users/{user_id}/mailFolders"

        folders = []
        for folder in self._paginate(url, params={"$top": 200}):
            self._check_control()
            folder_name = folder.get("displayName") or folder.get("id")
            folder_path = f"{prefix}/{folder_name}" if prefix else folder_name
            folders.append({
                "id": folder.get("id"),
                "displayName": folder_name,
                "path": folder_path,
                "totalItemCount": folder.get("totalItemCount", 0),
                "unreadItemCount": folder.get("unreadItemCount", 0),
                "childFolderCount": folder.get("childFolderCount", 0),
            })
            if folder.get("childFolderCount", 0):
                folders.extend(self._list_mail_folders(user_id, folder["id"], folder_path))
        return folders

    def _backup_messages(self, user_id: str, user_dir: Path, folders: list, attachment_manifest: list) -> list:
        messages = []
        attachment_root = user_dir / "_attachments"
        for folder in folders:
            self._check_control()
            folder_url = f"{self.GRAPH}/users/{user_id}/mailFolders/{folder['id']}/messages"
            for msg in self._paginate(folder_url, params={"$top": 100}):
                self._check_control()
                msg["backupFolderPath"] = folder["path"]
                messages.append(msg)
                self.stats["files_downloaded"] += 1
                self.stats["bytes_downloaded"] += len(json.dumps(msg, default=str).encode("utf-8"))
                if msg.get("hasAttachments"):
                    self._backup_message_attachments(user_id, msg, attachment_root, attachment_manifest)
                self._emit("file_done", {
                    "current_file": (msg.get("subject") or msg.get("id") or "message")[:80],
                    "target_name": user_dir.name,
                })
        return messages

    def _backup_message_attachments(self, user_id: str, message: dict, attachment_root: Path, manifest: list):
        msg_id = message.get("id")
        if not msg_id:
            return
        safe_subject = self._safe_segment((message.get("subject") or "message")[:60])
        message_dir = attachment_root / f"{msg_id}_{safe_subject}"
        message_dir.mkdir(parents=True, exist_ok=True)

        for attachment in self._paginate(
            f"{self.GRAPH}/users/{user_id}/messages/{msg_id}/attachments",
            params={"$top": 100},
        ):
            self._check_control()
            record = {
                "message_id": msg_id,
                "message_subject": message.get("subject", ""),
                "attachment_id": attachment.get("id"),
                "name": attachment.get("name"),
                "contentType": attachment.get("contentType"),
                "size": attachment.get("size", 0),
                "type": attachment.get("@odata.type"),
            }
            manifest.append(record)
            if attachment.get("@odata.type") == "#microsoft.graph.fileAttachment" and attachment.get("contentBytes"):
                file_path = message_dir / self._safe_segment(attachment.get("name") or attachment.get("id") or "attachment.bin")
                raw = base64.b64decode(attachment["contentBytes"])
                with open(file_path, "wb") as handle:
                    handle.write(raw)
                record["saved_path"] = str(file_path.relative_to(self.backup_root))
                self.stats["files_downloaded"] += 1
                self.stats["bytes_downloaded"] += len(raw)
            else:
                meta_path = message_dir / f"{self._safe_segment(attachment.get('name') or attachment.get('id') or 'attachment')}.json"
                self._write_json(meta_path, attachment)

    def _backup_collection(self, url: str, params=None) -> list:
        items = []
        for item in self._paginate(url, params=params):
            self._check_control()
            items.append(item)
            self.stats["files_downloaded"] += 1
            self.stats["bytes_downloaded"] += len(json.dumps(item, default=str).encode("utf-8"))
        return items

    def _save_manifest(self, backup_path: Path):
        self._write_json(backup_path / "_workload_manifest.json", {
            "workload": "outlook",
            "tenant_name": self.tenant.get("name"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mailbox_count": self.stats["mailbox_count"],
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

    def _safe_segment(self, value: str) -> str:
        return (value or "unnamed").replace("/", "_").replace("\\", "_").strip() or "unnamed"

    def _write_json(self, path: Path, payload):
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
