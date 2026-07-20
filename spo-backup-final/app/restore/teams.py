"""Teams restore means export backup to HTML/JSON/TXT archives."""
import json
from datetime import datetime
from html import escape
from pathlib import Path

from app.restore.base import BaseRestore
from app.task_control import PauseException

import logging
import re

log = logging.getLogger("spo_backup")


class TeamsRestore(BaseRestore):
    WORKLOAD_NAME = "teams"

    def __init__(self, tenant, backup_path, export_format=None, export_dir=None, **kwargs):
        super().__init__(tenant, backup_path, **kwargs)
        self.export_format = export_format or "all"
        self.export_dir = Path(export_dir) if export_dir else self.backup_path.parent.parent / "teams-exports" / self.backup_path.name

    def restore(self) -> dict:
        self.stats["start_time"] = datetime.utcnow().isoformat()
        self.emit("restore_start", {"backup_path": str(self.backup_path)})
        try:
            self.export_dir.mkdir(parents=True, exist_ok=True)
            for team_dir in self.backup_path.iterdir():
                if not team_dir.is_dir() or team_dir.name.startswith("_"):
                    continue
                self._check_control()
                self.emit("target_start", {"target_name": team_dir.name})
                try:
                    out_dir = self.export_dir / team_dir.name
                    out_dir.mkdir(parents=True, exist_ok=True)
                    self._export_team(team_dir, out_dir)
                    self.stats["targets_processed"] += 1
                    self.emit("target_done", {"target_name": team_dir.name, "status": "success"})
                except PauseException:
                    raise
                except Exception as e:
                    self.stats["targets_failed"] += 1
                    self.stats["errors"].append(f"[{team_dir.name}] {str(e)[:200]}")
                    self.emit("target_done", {"target_name": team_dir.name, "status": "failed", "error": str(e)})
            self._write_master_index()
        except PauseException:
            self.stats["cancelled"] = True
        except Exception as e:
            log.error(f"Teams export failed: {e}", exc_info=True)
            self.stats["errors"].append(f"Fatal: {e}")
        self.stats["end_time"] = datetime.utcnow().isoformat()
        self.stats["export_dir"] = str(self.export_dir)
        self.emit("restore_done", {"export_dir": str(self.export_dir)})
        return self.stats

    def _export_team(self, team_dir: Path, out_dir: Path):
        meta = {}
        if (team_dir / "_team_metadata.json").exists():
            try:
                meta = json.load(open(team_dir / "_team_metadata.json"))
            except Exception:
                pass
        channels_dir = team_dir / "channels"
        if not channels_dir.exists():
            return
        team_name = meta.get("team_name", team_dir.name)
        channel_summaries = []
        for ch_dir in channels_dir.iterdir():
            if not ch_dir.is_dir():
                continue
            self._check_control()
            msg_file = ch_dir / "messages.json"
            if not msg_file.exists():
                continue
            try:
                messages = json.load(open(msg_file))
            except Exception as e:
                self.stats["errors"].append(f"{ch_dir.name}: {e}")
                continue
            total_msgs = len(messages) + sum(len(m.get("_replies", [])) for m in messages)
            if self.export_format in ("html", "all"):
                self._write_channel_html(out_dir / f"{ch_dir.name}.html", team_name, ch_dir.name, messages)
                self.stats["items_processed"] += 1
            if self.export_format in ("txt", "all"):
                self._write_channel_txt(out_dir / f"{ch_dir.name}.txt", team_name, ch_dir.name, messages)
                self.stats["items_processed"] += 1
            if self.export_format in ("json", "all"):
                self._write_channel_json(out_dir / f"{ch_dir.name}.json", messages)
                self.stats["items_processed"] += 1
            channel_summaries.append({"name": ch_dir.name, "message_count": total_msgs, "html_file": f"{ch_dir.name}.html"})
        self._write_team_index(out_dir, team_name, meta, channel_summaries)

    def _render_message_html(self, msg: dict, is_reply=False) -> str:
        cls = "msg reply" if is_reply else "msg"
        sender = msg.get("from", {}) or {}
        user_info = sender.get("user") or {}
        name = user_info.get("displayName") or sender.get("application", {}).get("displayName") or "Unknown"
        ts = msg.get("createdDateTime", "")
        subject = msg.get("subject", "")
        body = msg.get("body", {}) or {}
        content = body.get("content", "")
        content_type = body.get("contentType", "html")
        content_html = f"<div>{escape(content).replace(chr(10), '<br>')}</div>" if content_type == "text" else content
        attachments = msg.get("attachments", [])
        attach_html = ""
        if attachments:
            attach_html = '<div class="attach">Attachments: ' + ", ".join(escape(a.get("name", "unknown")) for a in attachments) + "</div>"
        subject_html = f'<div class="subject">{escape(subject)}</div>' if subject else ""
        return f"""
<div class="{cls}">
  <div class="msg-header"><strong>{escape(name)}</strong> · {escape(ts)}</div>
  {subject_html}
  <div class="msg-body">{content_html}</div>
  {attach_html}
</div>
"""

    def _write_channel_html(self, path: Path, team_name: str, ch_name: str, messages: list):
        html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>{escape(team_name)} · {escape(ch_name)}</title>
<style>
body {{ font-family: -apple-system, "Segoe UI", sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; background: #f5f5f7; color: #1a1a1a; }}
h1 {{ background: linear-gradient(135deg, #464eb8, #6264a7); color: #fff; padding: 20px; border-radius: 12px; margin: 0 0 20px; }}
.msg {{ background: #fff; margin: 10px 0; padding: 14px; border-radius: 10px; border-left: 4px solid #6264a7; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }}
.msg-header {{ font-size: 12px; color: #666; margin-bottom: 6px; }}
.reply {{ margin-left: 30px; border-left-color: #a8a9d0; background: #fafaff; }}
.subject {{ font-weight: 600; color: #464eb8; margin-bottom: 4px; }}
.attach {{ font-size: 12px; color: #0078d4; margin-top: 6px; }}
</style></head><body>
<h1>{escape(team_name)} · #{escape(ch_name)}</h1>
<p style="color:#666;font-size:13px;">Exported {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} · {len(messages)} top-level messages</p>
"""
        for msg in messages:
            html += self._render_message_html(msg)
            for reply in msg.get("_replies", []):
                html += self._render_message_html(reply, is_reply=True)
        html += "</body></html>"
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(html)

    def _write_channel_txt(self, path: Path, team_name: str, ch_name: str, messages: list):
        lines = [
            "=" * 70,
            f"Team: {team_name}",
            f"Channel: #{ch_name}",
            f"Exported: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "=" * 70,
            "",
        ]
        def render_msg(msg, indent=0):
            prefix = "    " * indent
            sender = msg.get("from", {}) or {}
            name = (sender.get("user") or {}).get("displayName") or "Unknown"
            ts = msg.get("createdDateTime", "")
            subject = msg.get("subject", "")
            body = (msg.get("body") or {}).get("content", "")
            body_text = re.sub(r"<[^>]+>", "", body)
            out = [f"{prefix}[{ts}] {name}"]
            if subject:
                out.append(f"{prefix}Subject: {subject}")
            out.append(f"{prefix}{body_text[:500]}")
            out.append("")
            return "\n".join(out)
        for msg in messages:
            lines.append(render_msg(msg))
            for reply in msg.get("_replies", []):
                lines.append(render_msg(reply, indent=1))
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines))

    def _write_channel_json(self, path: Path, messages: list):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(messages, handle, indent=2, default=str, ensure_ascii=False)

    def _write_team_index(self, out_dir: Path, team_name: str, team_meta: dict, channels: list):
        members = team_meta.get("members", [])
        owners = team_meta.get("owners", [])
        html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>{escape(team_name)}</title>
<style>
body {{ font-family: -apple-system, "Segoe UI", sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; background: #f5f5f7; }}
h1 {{ background: linear-gradient(135deg, #464eb8, #6264a7); color: #fff; padding: 20px; border-radius: 12px; margin: 0 0 20px; }}
.card {{ background: #fff; padding: 16px; border-radius: 10px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); margin: 10px 0; }}
a {{ color: #464eb8; text-decoration: none; font-weight: 600; }}
.badge {{ background: #6264a7; color: #fff; padding: 2px 8px; border-radius: 6px; font-size: 11px; margin-left: 6px; }}
</style></head><body>
<h1>{escape(team_name)}</h1>
<div class="card"><h3>Overview</h3>
<p><strong>Description:</strong> {escape(team_meta.get("description", "—") or "—")}</p>
<p><strong>Visibility:</strong> {escape(team_meta.get("visibility", "—") or "—")}</p>
<p><strong>Members:</strong> {len(members)}</p>
<p><strong>Owners:</strong> {len(owners)}</p></div>
<div class="card"><h3>Channels ({len(channels)})</h3><ul>"""
        for channel in channels:
            html += f'<li><a href="{escape(channel["html_file"])}">#{escape(channel["name"])}</a><span class="badge">{channel["message_count"]} msgs</span></li>'
        html += "</ul></div><div class=\"card\"><h3>Members</h3><ul>"
        for member in members[:50]:
            html += f'<li>{escape(member.get("displayName") or "")} · <em>{escape(member.get("email") or "")}</em></li>'
        if len(members) > 50:
            html += f"<li><em>...and {len(members) - 50} more</em></li>"
        html += "</ul></div></body></html>"
        with open(out_dir / "index.html", "w", encoding="utf-8") as handle:
            handle.write(html)

    def _write_master_index(self):
        teams = [item for item in self.export_dir.iterdir() if item.is_dir() and not item.name.startswith("_")]
        html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Teams Backup Export</title>
<style>
body {{ font-family: -apple-system, "Segoe UI", sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; background: #f5f5f7; }}
h1 {{ background: linear-gradient(135deg, #464eb8, #6264a7); color: #fff; padding: 24px; border-radius: 12px; margin: 0 0 20px; }}
.card {{ background: #fff; padding: 16px; border-radius: 10px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); margin: 10px 0; }}
a {{ color: #464eb8; text-decoration: none; font-weight: 600; }}
</style></head><body>
<h1>Teams Backup Export</h1>
<p style="color:#666;">Tenant: <strong>{escape(self.tenant.get('name', 'Unknown'))}</strong> · Exported: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
<div class="card"><h3>Teams ({len(teams)})</h3><ul>"""
        for team in teams:
            html += f'<li><a href="{escape(team.name)}/index.html">{escape(team.name)}</a></li>'
        html += "</ul></div></body></html>"
        with open(self.export_dir / "index.html", "w", encoding="utf-8") as handle:
            handle.write(html)

    def dry_run(self) -> dict:
        result = {
            "workload": "teams",
            "mode": "export_only",
            "teams_in_backup": 0,
            "channels_in_backup": 0,
            "messages_in_backup": 0,
            "export_dir": str(self.export_dir),
            "note": "Teams does not support direct message re-import via Graph API. Restore = export to HTML/JSON/TXT.",
        }
        if not self.backup_path.exists():
            result["error"] = f"Backup not found: {self.backup_path}"
            return result
        for team_dir in self.backup_path.iterdir():
            if not team_dir.is_dir() or team_dir.name.startswith("_"):
                continue
            result["teams_in_backup"] += 1
            channels_dir = team_dir / "channels"
            if channels_dir.exists():
                for channel_dir in channels_dir.iterdir():
                    if channel_dir.is_dir():
                        result["channels_in_backup"] += 1
                        msg_file = channel_dir / "messages.json"
                        if msg_file.exists():
                            try:
                                msgs = json.load(open(msg_file))
                                result["messages_in_backup"] += len(msgs)
                                result["messages_in_backup"] += sum(len(m.get("_replies", [])) for m in msgs)
                            except Exception:
                                pass
        return result
