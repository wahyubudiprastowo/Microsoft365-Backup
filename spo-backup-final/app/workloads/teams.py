"""Microsoft Teams workload backup and target discovery."""
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

from app.task_control import PauseException, check_control
from app.workloads.base import BaseWorkload

log = logging.getLogger("spo_backup")


class TeamsWorkload(BaseWorkload):
    workload_type = "teams"

    def __init__(self, tenant_config, backup_root=None, progress_callback=None, task_id=None):
        super().__init__(tenant_config)
        self.backup_root = backup_root
        self.progress_callback = progress_callback
        self.task_id = task_id
        self.stats = {
            "workload": "teams",
            "start_time": None,
            "end_time": None,
            "backup_path": None,
            "teams_count": 0,
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

    def list_targets(self) -> list:
        teams = []
        try:
            for group in self._paginate(
                f"{self.GRAPH}/groups",
                params={
                    "$filter": "resourceProvisioningOptions/Any(x:x eq 'Team')",
                    "$select": "id,displayName,description,visibility,createdDateTime",
                    "$top": 100,
                },
            ):
                teams.append({
                    "id": group["id"],
                    "name": group.get("displayName", "Unknown"),
                    "description": group.get("description", ""),
                    "visibility": group.get("visibility", "private"),
                    "type": "team",
                })
        except Exception as e:
            return [{"error": str(e)}]
        return teams

    def backup(self):
        if not self.backup_root:
            raise ValueError("backup_root is required for Teams backup")
        self.stats["start_time"] = datetime.now(timezone.utc).isoformat()
        backup_path, resumed_existing = self._resolve_backup_dir(self.backup_root)
        self.stats["backup_path"] = str(backup_path)
        self.stats["resumed_existing_backup"] = resumed_existing
        self._update_run_manifest(backup_path, {
            "workload": "teams",
            "tenant_name": self.tenant.get("name"),
            "tenant_id": self.tenant.get("id"),
            "backup_path": str(backup_path),
            "status": "running",
            "start_time": self.stats["start_time"],
            "resumed_existing_backup": resumed_existing,
        })
        self._emit("backup_start", {"backup_path": str(backup_path), "resumed_existing_backup": resumed_existing})

        try:
            teams = self.list_targets()
            if teams and teams[0].get("error"):
                raise Exception(teams[0]["error"])
            teams, selection_info = self.apply_target_selection(teams)
            self.stats["selection_mode"] = selection_info["mode"]
            self.stats["targets_available"] = selection_info["available_count"]
            self.stats["targets_in_scope"] = selection_info["effective_count"]
            self.stats["teams_count"] = selection_info["effective_count"]
            if selection_info["mode"] == "selected" and not teams:
                raise ValueError("None of the selected Teams were found")
            for idx, team in enumerate(teams, start=1):
                self._check_control()
                team_dir = self._team_backup_dir(team, backup_path)
                marker = self._load_json(team_dir / self.TARGET_COMPLETE_MARKER, default={})
                if marker.get("schema_version", 0) >= 2 and marker.get("target_id") == team["id"] and not marker.get("partial"):
                    self.stats["targets_processed"] += 1
                    self._emit("target_done", {"target_name": team["name"], "status": "skipped-resume"})
                    continue
                self._emit("target_start", {"target_name": team["name"], "target_idx": idx, "target_total": len(teams)})
                try:
                    self._backup_team(team, backup_path)
                    self.stats["targets_processed"] += 1
                    self._emit("target_done", {"target_name": team["name"], "status": "success"})
                except PauseException:
                    raise
                except Exception as e:
                    self.stats["targets_failed"] += 1
                    self.stats["errors"].append(f"[{team['name']}] {str(e)[:200]}")
                    self._emit("target_done", {"target_name": team["name"], "status": "failed", "error": str(e)})
            self._save_manifest(backup_path, teams)
        except PauseException:
            self.stats["cancelled"] = True
        except Exception as e:
            log.error(f"Teams backup failed: {e}", exc_info=True)
            self.stats["errors"].append(f"Fatal: {e}")

        self.stats["end_time"] = datetime.now(timezone.utc).isoformat()
        final_status = "cancelled" if self.stats["cancelled"] else (
            "failed" if any(str(err).startswith("Fatal:") for err in self.stats["errors"])
            or (self.stats["targets_failed"] and not self.stats["targets_processed"])
            else ("partial" if self.stats["targets_failed"] or self.stats["errors"] else "success"))
        self._update_run_manifest(backup_path, {
            "status": final_status,
            "end_time": self.stats["end_time"],
            "targets_processed": self.stats["targets_processed"],
            "targets_failed": self.stats["targets_failed"],
            "files_downloaded": self.stats["files_downloaded"],
            "bytes_downloaded": self.stats["bytes_downloaded"],
            "errors": self.stats["errors"][:20],
        })
        self._emit("workload_done", {"backup_path": str(backup_path)})
        return self.stats

    def _backup_team(self, team: dict, backup_path: Path):
        errors_before = len(self.stats["errors"])
        team_id = team["id"]
        team_dir = self._team_backup_dir(team, backup_path)
        team_dir.mkdir(parents=True, exist_ok=True)

        members = list(self._paginate(f"{self.GRAPH}/groups/{team_id}/members?$top=100"))
        owners = list(self._paginate(f"{self.GRAPH}/groups/{team_id}/owners?$top=100"))
        team_full = self._get(f"{self.GRAPH}/teams/{team_id}")

        meta = {
            "team_id": team_id,
            "team_name": team["name"],
            "description": team.get("description"),
            "visibility": team.get("visibility"),
            "created": team_full.get("createdDateTime"),
            "settings": {
                "member": team_full.get("memberSettings"),
                "messaging": team_full.get("messagingSettings"),
                "guest": team_full.get("guestSettings"),
                "fun": team_full.get("funSettings"),
                "discovery": team_full.get("discoverySettings"),
            },
            "members": [{"id": m.get("id"), "displayName": m.get("displayName"), "email": m.get("mail") or m.get("userPrincipalName")} for m in members],
            "owners": [{"id": o.get("id"), "displayName": o.get("displayName"), "email": o.get("mail") or o.get("userPrincipalName")} for o in owners],
            "backup_time": datetime.now(timezone.utc).isoformat(),
        }
        with open(team_dir / "_team_metadata.json", "w") as handle:
            json.dump(meta, handle, indent=2, default=str)
        self.stats["files_downloaded"] += 1

        try:
            apps = list(self._paginate(f"{self.GRAPH}/teams/{team_id}/installedApps?$expand=teamsAppDefinition"))
            with open(team_dir / "apps.json", "w") as handle:
                json.dump(apps, handle, indent=2, default=str)
            self.stats["files_downloaded"] += 1
        except Exception as e:
            self.stats["errors"].append(f"apps [{team['name']}]: {str(e)[:100]}")

        channels_dir = team_dir / "channels"
        channels_dir.mkdir(parents=True, exist_ok=True)
        channels = list(self._paginate(f"{self.GRAPH}/teams/{team_id}/channels"))
        for channel in channels:
            self._check_control()
            self._backup_channel(team_id, channel, channels_dir)
        self._write_target_complete_marker(team_dir, {
            "schema_version": 2,
            "partial": len(self.stats["errors"]) > errors_before,
            "target_type": "team",
            "target_id": team_id,
            "target_name": team["name"],
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })

    def _backup_channel(self, team_id: str, channel: dict, channels_dir: Path):
        ch_id = channel["id"]
        ch_name = channel.get("displayName", "Unknown")
        ch_dir = channels_dir / self._safe_name(ch_name)
        ch_dir.mkdir(parents=True, exist_ok=True)

        messages = list(self._paginate(
                f"{self.GRAPH}/teams/{team_id}/channels/{ch_id}/messages",
                params={"$top": 50},
            ))
        for msg in messages:
            if msg.get("id"):
                msg["_replies"] = list(self._paginate(
                    f"{self.GRAPH}/teams/{team_id}/channels/{ch_id}/messages/{msg['id']}/replies",
                    params={"$top": 50},
                ))
        with open(ch_dir / "messages.json", "w") as handle:
            json.dump(messages, handle, indent=2, default=str)
        self.stats["files_downloaded"] += 1

        try:
            tabs = list(self._paginate(f"{self.GRAPH}/teams/{team_id}/channels/{ch_id}/tabs?$expand=teamsApp"))
            with open(ch_dir / "tabs.json", "w") as handle:
                json.dump(tabs, handle, indent=2, default=str)
            self.stats["files_downloaded"] += 1
        except Exception as e:
            self.stats["errors"].append(f"tabs [{ch_name}]: {str(e)[:100]}")

        try:
            folder = self._get(f"{self.GRAPH}/teams/{team_id}/channels/{ch_id}/filesFolder")
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return
            raise
        if not folder.get("id"):
            raise ValueError(f"Channel '{ch_name}' filesFolder has no item ID")
        drive_id = (folder.get("parentReference") or {}).get("driveId")
        if not drive_id:
            raise ValueError(f"Channel '{ch_name}' filesFolder has no drive ID")
        files_dir = ch_dir / "files"
        files_dir.mkdir(parents=True, exist_ok=True)
        index = []
        self._walk_channel_files(drive_id, folder["id"], files_dir, index)
        with open(files_dir / "_files_index.json", "w") as handle:
            json.dump({"drive_id": drive_id, "folder_id": folder["id"], "web_url": folder.get("webUrl"), "items": index}, handle, indent=2, default=str)
        self.stats["files_downloaded"] += 1

    def _walk_channel_files(self, drive_id: str, folder_id: str, local_dir: Path, index: list, prefix: str = ""):
        for item in self._paginate(f"{self.GRAPH}/drives/{drive_id}/items/{folder_id}/children", params={"$top": 200}):
            self._check_control()
            name = item.get("name") or item.get("id") or "unnamed"
            safe_name = self._safe_name(name)
            relative = f"{prefix}/{name}" if prefix else name
            record = {"id": item.get("id"), "name": name, "relative_path": relative,
                      "size": item.get("size", 0), "webUrl": item.get("webUrl"),
                      "lastModified": item.get("lastModifiedDateTime"), "isFolder": "folder" in item}
            index.append(record)
            destination = local_dir / safe_name
            if "folder" in item:
                destination.mkdir(parents=True, exist_ok=True)
                self._walk_channel_files(drive_id, item["id"], destination, index, relative)
                continue
            download_url = item.get("@microsoft.graph.downloadUrl") or f"{self.GRAPH}/drives/{drive_id}/items/{item['id']}/content"
            downloaded = self._download(download_url, str(destination), item.get("size", 0),
                                        auth_required=not bool(item.get("@microsoft.graph.downloadUrl")))
            self.stats["files_downloaded"] += 1
            self.stats["bytes_downloaded"] += downloaded
            self._emit("file_done", {"current_file": relative})

    def _save_manifest(self, backup_path: Path, teams: list):
        manifest = {
            "workload": "teams",
            "tenant_name": self.tenant.get("name"),
            "tenant_id": self.tenant.get("id"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "teams_count": len(teams),
            "targets_available": self.stats["targets_available"],
            "targets_in_scope": self.stats["targets_in_scope"],
            "selection_mode": self.stats["selection_mode"],
            "files_downloaded": self.stats["files_downloaded"],
            "targets_processed": self.stats["targets_processed"],
            "targets_failed": self.stats["targets_failed"],
            "errors": self.stats["errors"][:20],
            "status": ("cancelled" if self.stats["cancelled"] else
                       "failed" if any(str(err).startswith("Fatal:") for err in self.stats["errors"])
                       or (self.stats["targets_failed"] and not self.stats["targets_processed"])
                       else "partial" if self.stats["targets_failed"] or self.stats["errors"] else "success"),
            "backup_path": str(backup_path),
            "start_time": self.stats["start_time"],
            "end_time": self.stats["end_time"],
            "resumed_existing_backup": bool(self.stats.get("resumed_existing_backup")),
        }
        with open(backup_path / "_workload_manifest.json", "w") as handle:
            json.dump(manifest, handle, indent=2, default=str)

    def _safe_name(self, name: str) -> str:
        result = "".join(ch if ch.isalnum() or ch in "_-. " else "_" for ch in name).strip()[:100]
        return "Unknown" if result in {"", ".", ".."} else result

    def _team_backup_dir(self, team: dict, backup_path: Path) -> Path:
        return self._target_backup_dir(backup_path, self._safe_name(team["name"]), team["id"],
                                       "_team_metadata.json", "team_id")

    def _check_control(self):
        if self.task_id:
            check_control(self.task_id)

    def _emit(self, event: str, data: dict):
        if self.progress_callback:
            payload = dict(self.stats)
            payload.update(data or {})
            self.progress_callback(event, payload)
