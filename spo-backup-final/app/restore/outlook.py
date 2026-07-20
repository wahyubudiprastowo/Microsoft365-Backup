"""Outlook restore implementation."""
import json
from datetime import datetime, timezone
from pathlib import Path

from app.task_control import PauseException
from app.restore.base import BaseRestore

import logging

log = logging.getLogger("spo_backup")
RESTORE_FOLDER_NAME = "M365 Restored"


class OutlookRestore(BaseRestore):
    WORKLOAD_NAME = "outlook"

    def __init__(self, tenant, backup_path, user_mapping=None, restore_items=None, **kwargs):
        super().__init__(tenant, backup_path, **kwargs)
        self.user_mapping = user_mapping or {}
        self.restore_items = restore_items or ["messages", "calendar", "contacts"]

    def _resolve_user_id(self, user_email: str) -> str:
        user = self._get(f"{self.GRAPH}/users/{user_email}")
        return user["id"]

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
                    user_id = self._resolve_user_id(target_email)
                    if "messages" in self.restore_items:
                        self._restore_messages(user_dir, user_id)
                    if "calendar" in self.restore_items:
                        self._restore_calendar(user_dir, user_id)
                    if "contacts" in self.restore_items:
                        self._restore_contacts(user_dir, user_id)
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
            log.error(f"Outlook restore failed: {e}", exc_info=True)
            self.stats["errors"].append(f"Fatal: {e}")
        self.stats["end_time"] = datetime.now(timezone.utc).isoformat()
        self.emit("restore_done")
        return self.stats

    def _restore_messages(self, user_dir: Path, user_id: str):
        msg_file = user_dir / "messages.json"
        if not msg_file.exists():
            return
        messages = json.load(open(msg_file))
        restore_folder_id = self._ensure_mail_folder(user_id, RESTORE_FOLDER_NAME)
        existing_ids = set()
        if self.mode == "merge":
            try:
                items = self._get(
                    f"{self.GRAPH}/users/{user_id}/mailFolders/{restore_folder_id}/messages",
                    params={"$select": "internetMessageId", "$top": 999},
                )
                existing_ids = {item.get("internetMessageId") for item in items.get("value", [])}
            except Exception:
                pass
        for msg in messages:
            self._check_control()
            try:
                msg_id = msg.get("internetMessageId")
                if self.mode == "merge" and msg_id and msg_id in existing_ids:
                    self.stats["items_skipped"] += 1
                    continue
                self._post(
                    f"{self.GRAPH}/users/{user_id}/mailFolders/{restore_folder_id}/messages",
                    json_body=self._clean_message_for_post(msg),
                )
                self.stats["items_processed"] += 1
                self.emit("message_restored", {"current_file": msg.get("subject", "(no subject)")[:50]})
            except PauseException:
                raise
            except Exception as e:
                self.stats["items_failed"] += 1
                self.stats["errors"].append(f"msg '{msg.get('subject', '?')[:30]}': {str(e)[:100]}")

    def _ensure_mail_folder(self, user_id: str, folder_name: str) -> str:
        try:
            folders = self._get(
                f"{self.GRAPH}/users/{user_id}/mailFolders",
                params={"$filter": f"displayName eq '{folder_name}'"},
            )
            if folders.get("value"):
                return folders["value"][0]["id"]
        except Exception:
            pass
        created = self._post(f"{self.GRAPH}/users/{user_id}/mailFolders", json_body={"displayName": folder_name})
        return created["id"]

    def _clean_message_for_post(self, msg: dict) -> dict:
        remove_fields = {
            "id", "createdDateTime", "lastModifiedDateTime", "changeKey", "categories",
            "parentFolderId", "conversationId", "conversationIndex", "isDeliveryReceiptRequested",
            "isReadReceiptRequested", "webLink", "inferenceClassification", "@odata.etag", "@odata.context",
            "backupFolderPath",
        }
        cleaned = {k: v for k, v in msg.items() if k not in remove_fields}
        if "body" in cleaned and isinstance(cleaned["body"], dict):
            cleaned["body"] = {
                "contentType": cleaned["body"].get("contentType", "html"),
                "content": cleaned["body"].get("content", ""),
            }
        return cleaned

    def _restore_calendar(self, user_dir: Path, user_id: str):
        cal_file = user_dir / "calendar.json"
        if not cal_file.exists():
            return
        events = json.load(open(cal_file))
        existing_uids = set()
        if self.mode == "merge":
            try:
                items = self._get(f"{self.GRAPH}/users/{user_id}/events", params={"$select": "iCalUId", "$top": 999})
                existing_uids = {item.get("iCalUId") for item in items.get("value", []) if item.get("iCalUId")}
            except Exception:
                pass
        for event in events:
            self._check_control()
            try:
                uid = event.get("iCalUId")
                if self.mode == "merge" and uid and uid in existing_uids:
                    self.stats["items_skipped"] += 1
                    continue
                self._post(f"{self.GRAPH}/users/{user_id}/events", json_body=self._clean_event_for_post(event))
                self.stats["items_processed"] += 1
                self.emit("event_restored", {"current_file": event.get("subject", "(no subject)")[:50]})
            except PauseException:
                raise
            except Exception as e:
                self.stats["items_failed"] += 1
                self.stats["errors"].append(f"event '{event.get('subject', '?')[:30]}': {str(e)[:100]}")

    def _clean_event_for_post(self, event: dict) -> dict:
        remove_fields = {
            "id", "createdDateTime", "lastModifiedDateTime", "changeKey", "categories",
            "originalStartTimeZone", "originalEndTimeZone", "iCalUId", "reminderMinutesBeforeStart",
            "isReminderOn", "hasAttachments", "responseStatus", "seriesMasterId", "webLink",
            "onlineMeetingUrl", "@odata.etag", "@odata.context",
        }
        return {k: v for k, v in event.items() if k not in remove_fields}

    def _restore_contacts(self, user_dir: Path, user_id: str):
        cont_file = user_dir / "contacts.json"
        if not cont_file.exists():
            return
        contacts = json.load(open(cont_file))
        existing_emails = set()
        if self.mode == "merge":
            try:
                items = self._get(f"{self.GRAPH}/users/{user_id}/contacts", params={"$select": "emailAddresses", "$top": 999})
                for contact in items.get("value", []):
                    for email in contact.get("emailAddresses") or []:
                        if email.get("address"):
                            existing_emails.add(email["address"].lower())
            except Exception:
                pass
        for contact in contacts:
            self._check_control()
            try:
                if self.mode == "merge":
                    emails = [item.get("address", "").lower() for item in (contact.get("emailAddresses") or [])]
                    if any(email in existing_emails for email in emails):
                        self.stats["items_skipped"] += 1
                        continue
                self._post(f"{self.GRAPH}/users/{user_id}/contacts", json_body=self._clean_contact_for_post(contact))
                self.stats["items_processed"] += 1
                self.emit("contact_restored", {"current_file": contact.get("displayName", "(no name)")})
            except PauseException:
                raise
            except Exception as e:
                self.stats["items_failed"] += 1
                self.stats["errors"].append(f"contact '{contact.get('displayName', '?')[:30]}': {str(e)[:100]}")

    def _clean_contact_for_post(self, contact: dict) -> dict:
        remove_fields = {"id", "createdDateTime", "lastModifiedDateTime", "changeKey", "categories", "parentFolderId", "@odata.etag", "@odata.context"}
        return {k: v for k, v in contact.items() if k not in remove_fields}

    def dry_run(self) -> dict:
        result = {"workload": "outlook", "users_in_backup": 0, "messages_to_restore": 0, "events_to_restore": 0, "contacts_to_restore": 0}
        for user_dir in self.backup_path.iterdir():
            if not user_dir.is_dir() or user_dir.name.startswith("_"):
                continue
            result["users_in_backup"] += 1
            for key, filename in [("messages_to_restore", "messages.json"), ("events_to_restore", "calendar.json"), ("contacts_to_restore", "contacts.json")]:
                path = user_dir / filename
                if path.exists():
                    try:
                        result[key] += len(json.load(open(path)))
                    except Exception:
                        pass
        return result
