"""Microsoft Teams workload backup and target discovery."""
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

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
            "targets_processed": 0,
            "targets_failed": 0,
            "files_downloaded": 0,
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
        backup_path = Path(self.backup_root) / f"backup_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        backup_path.mkdir(parents=True, exist_ok=True)
        self.stats["backup_path"] = str(backup_path)
        self._emit("backup_start", {"backup_path": str(backup_path)})

        try:
            teams = self.list_targets()
            if teams and teams[0].get("error"):
                raise Exception(teams[0]["error"])
            self.stats["teams_count"] = len(teams)
            for idx, team in enumerate(teams, start=1):
                self._check_control()
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
        self._emit("workload_done", {"backup_path": str(backup_path)})
        return self.stats

    def _backup_team(self, team: dict, backup_path: Path):
        team_id = team["id"]
        team_dir = backup_path / self._safe_name(team["name"])
        team_dir.mkdir(parents=True, exist_ok=True)

        try:
            members = list(self._paginate(f"{self.GRAPH}/groups/{team_id}/members?$top=100"))
            owners = list(self._paginate(f"{self.GRAPH}/groups/{team_id}/owners?$top=100"))
            team_full = self._get(f"{self.GRAPH}/teams/{team_id}")
        except Exception as e:
            log.warning(f"Failed to get full team info for {team['name']}: {e}")
            members, owners, team_full = [], [], {}

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

    def _backup_channel(self, team_id: str, channel: dict, channels_dir: Path):
        ch_id = channel["id"]
        ch_name = channel.get("displayName", "Unknown")
        ch_dir = channels_dir / self._safe_name(ch_name)
        ch_dir.mkdir(parents=True, exist_ok=True)

        try:
            messages = list(self._paginate(
                f"{self.GRAPH}/teams/{team_id}/channels/{ch_id}/messages",
                params={"$top": 50},
            ))
            for msg in messages:
                if msg.get("id"):
                    try:
                        msg["_replies"] = list(self._paginate(
                            f"{self.GRAPH}/teams/{team_id}/channels/{ch_id}/messages/{msg['id']}/replies",
                            params={"$top": 50},
                        ))
                    except Exception:
                        msg["_replies"] = []
            with open(ch_dir / "messages.json", "w") as handle:
                json.dump(messages, handle, indent=2, default=str)
            self.stats["files_downloaded"] += 1
        except Exception as e:
            self.stats["errors"].append(f"messages [{ch_name}]: {str(e)[:100]}")

        try:
            tabs = list(self._paginate(f"{self.GRAPH}/teams/{team_id}/channels/{ch_id}/tabs?$expand=teamsApp"))
            with open(ch_dir / "tabs.json", "w") as handle:
                json.dump(tabs, handle, indent=2, default=str)
            self.stats["files_downloaded"] += 1
        except Exception as e:
            self.stats["errors"].append(f"tabs [{ch_name}]: {str(e)[:100]}")

        try:
            folder = self._get(f"{self.GRAPH}/teams/{team_id}/channels/{ch_id}/filesFolder")
            if folder.get("id"):
                drive_id = folder.get("parentReference", {}).get("driveId")
                if drive_id:
                    files_dir = ch_dir / "files"
                    files_dir.mkdir(parents=True, exist_ok=True)
                    file_items = list(self._paginate(f"{self.GRAPH}/drives/{drive_id}/items/{folder['id']}/children?$top=200"))
                    index = [{
                        "id": item.get("id"),
                        "name": item.get("name"),
                        "size": item.get("size", 0),
                        "webUrl": item.get("webUrl"),
                        "lastModified": item.get("lastModifiedDateTime"),
                        "isFolder": "folder" in item,
                    } for item in file_items]
                    with open(files_dir / "_files_index.json", "w") as handle:
                        json.dump({"drive_id": drive_id, "folder_id": folder["id"], "web_url": folder.get("webUrl"), "items": index}, handle, indent=2, default=str)
                    self.stats["files_downloaded"] += 1
        except Exception as e:
            self.stats["errors"].append(f"files [{ch_name}]: {str(e)[:100]}")

    def _save_manifest(self, backup_path: Path, teams: list):
        manifest = {
            "workload": "teams",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "teams_count": len(teams),
            "files_downloaded": self.stats["files_downloaded"],
            "targets_processed": self.stats["targets_processed"],
            "targets_failed": self.stats["targets_failed"],
            "errors": self.stats["errors"][:20],
        }
        with open(backup_path / "_workload_manifest.json", "w") as handle:
            json.dump(manifest, handle, indent=2, default=str)

    def _safe_name(self, name: str) -> str:
        return "".join(ch if ch.isalnum() or ch in "_-. " else "_" for ch in name).strip()[:100] or "Unknown"

    def _check_control(self):
        if self.task_id:
            check_control(self.task_id)

    def _emit(self, event: str, data: dict):
        if self.progress_callback:
            payload = dict(self.stats)
            payload.update(data or {})
            self.progress_callback(event, payload)
