"""Multi-channel notification dispatcher: Email, Telegram, Teams.
v6.0 — DETAILED email format with per-site breakdown, duration, performance metrics."""
import copy
import logging, requests, smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from email.utils import formataddr

from app.http_utils import build_retry_session

log = logging.getLogger("spo_backup")


class NotificationDispatcher:
    def __init__(self, config):
        self.config = config
        self.session = build_retry_session()

    def send_all(self, stats, only_channel=None):
        results = []
        notif = self.config.get("notification", {})
        selected = (only_channel or "").strip().lower()

        def should_send(channel_name):
            return selected in {"", channel_name}

        tg = notif.get("telegram", {})
        if should_send("telegram") and tg.get("enabled") and tg.get("bot_token"):
            try:
                msg = self._build_telegram(stats)
                for chat_id in tg.get("chat_ids", []):
                    r = requests.post(
                        f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage",
                        json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
                        timeout=(10, 30),
                    )
                    r.raise_for_status()
                results.append({"channel": "telegram", "success": True, "message": "Sent"})
            except Exception as e:
                results.append({"channel": "telegram", "success": False, "message": str(e)})

        teams = notif.get("teams", {})
        if should_send("teams") and teams.get("enabled"):
            try:
                card = self._build_teams(stats)
                for url in teams.get("webhook_urls", []):
                    response = self.session.post(url, json=card, timeout=(10, 30))
                    response.raise_for_status()
                results.append({"channel": "teams", "success": True, "message": "Sent"})
            except Exception as e:
                results.append({"channel": "teams", "success": False, "message": str(e)})

        if should_send("email") and notif.get("enabled"):
            try:
                self._send_email(stats)
                results.append({"channel": "email", "success": True, "message": "Sent"})
            except Exception as e:
                results.append({"channel": "email", "success": False, "message": str(e)})

        return results

    def send_test(self, channel=None):
        selected = (channel or "").strip().lower()
        original_config = self.config
        if selected:
            self.config = copy.deepcopy(self.config)
            notif = self.config.setdefault("notification", {})
            if selected == "email":
                notif["enabled"] = True
            elif selected == "telegram":
                tg = copy.deepcopy(notif.get("telegram", {}))
                tg["enabled"] = True
                notif["telegram"] = tg
            elif selected == "teams":
                teams = copy.deepcopy(notif.get("teams", {}))
                teams["enabled"] = True
                notif["teams"] = teams

        test_stats = {
            "total_sites": 13, "successful_sites": 12,
            "failed_sites": ["Photo Gallery"],
            "files_downloaded": 247, "files_skipped": 1503,
            "bytes_downloaded": 524288000,
            "errors": ["Photo Gallery: Connection timeout after 60s"],
            "start_time": datetime.now().isoformat(),
            "end_time": datetime.now().isoformat(),
            "site_details": [
                {"name": "BIRA TEAM", "status": "success", "files": 4, "size": 4126720},
                {"name": "backupsite", "status": "success", "files": 7032, "size": 5297045504},
                {"name": "Photo Gallery", "status": "failed", "error": "Connection timeout"},
            ],
        }
        try:
            return self.send_all(test_stats, only_channel=channel)
        finally:
            self.config = original_config

    # Helpers
    def _calc_duration(self, stats):
        try:
            st = stats.get("start_time"); et = stats.get("end_time")
            if not st or not et: return "N/A"
            if isinstance(st, str): st = datetime.fromisoformat(st.replace("Z", "+00:00"))
            if isinstance(et, str): et = datetime.fromisoformat(et.replace("Z", "+00:00"))
            secs = int((et - st).total_seconds())
            if secs < 60: return f"{secs}s"
            if secs < 3600: return f"{secs // 60}m {secs % 60}s"
            return f"{secs // 3600}h {(secs % 3600) // 60}m {secs % 60}s"
        except: return "N/A"

    def _format_size(self, bytes_count):
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if bytes_count < 1024:
                return f"{bytes_count:.2f} {unit}"
            bytes_count /= 1024
        return f"{bytes_count:.2f} PB"

    def _calc_speed(self, stats):
        try:
            st = stats.get("start_time"); et = stats.get("end_time")
            bytes_dl = stats.get("bytes_downloaded", 0)
            if not st or not et or bytes_dl == 0: return "N/A"
            if isinstance(st, str): st = datetime.fromisoformat(st.replace("Z", "+00:00"))
            if isinstance(et, str): et = datetime.fromisoformat(et.replace("Z", "+00:00"))
            secs = max(1, (et - st).total_seconds())
            return self._format_size(bytes_dl / secs) + "/s"
        except: return "N/A"

    def _format_time(self, time_str):
        try:
            if not time_str: return "N/A"
            if isinstance(time_str, str):
                dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
            else:
                dt = time_str
            from datetime import timedelta, timezone as tz
            return dt.astimezone(tz(timedelta(hours=7))).strftime("%Y-%m-%d %H:%M:%S WIB")
        except: return str(time_str)

    # Telegram
    def _build_telegram(self, s):
        ok = not s.get("failed_sites")
        duration = self._calc_duration(s)
        speed = self._calc_speed(s)
        size = self._format_size(s.get("bytes_downloaded", 0))
        msg = (
            f"{'🟢' if ok else '🔴'} <b>SharePoint Backup Report</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Status:</b> {'✅ SUCCESS' if ok else '⚠️ PARTIAL FAILURE'}\n"
            f"<b>Start:</b> {self._format_time(s.get('start_time'))}\n"
            f"<b>Duration:</b> {duration}\n"
            f"<b>Avg Speed:</b> {speed}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Sites:</b> {s.get('successful_sites',0)}/{s.get('total_sites',0)}\n"
            f"<b>Files Downloaded:</b> {s.get('files_downloaded',0):,}\n"
            f"<b>Files Skipped:</b> {s.get('files_skipped',0):,}\n"
            f"<b>Total Size:</b> {size}\n"
        )
        if s.get("failed_sites"):
            msg += "\n❌ <b>Failed:</b>\n"
            for site in s["failed_sites"][:5]:
                msg += f"  • {site}\n"
        if s.get("errors"):
            msg += "\n⚠️ <b>Errors:</b>\n"
            for err in s["errors"][:3]:
                msg += f"  • <code>{str(err)[:80]}</code>\n"
        return msg

    # Teams
    def _build_teams(self, s):
        ok = not s.get("failed_sites")
        facts = [
            {"name": "Status", "value": "✅ SUCCESS" if ok else "⚠️ PARTIAL FAILURE"},
            {"name": "Start Time", "value": self._format_time(s.get("start_time"))},
            {"name": "Duration", "value": self._calc_duration(s)},
            {"name": "Avg Speed", "value": self._calc_speed(s)},
            {"name": "Sites", "value": f"{s.get('successful_sites',0)}/{s.get('total_sites',0)}"},
            {"name": "Files Downloaded", "value": f"{s.get('files_downloaded',0):,}"},
            {"name": "Files Skipped", "value": f"{s.get('files_skipped',0):,}"},
            {"name": "Total Size", "value": self._format_size(s.get('bytes_downloaded', 0))},
        ]
        sections = [{
            "activityTitle": "📦 SharePoint Backup Report",
            "activitySubtitle": "Digiserve by Telkom — IT Infrastructure",
            "facts": facts,
        }]
        if s.get("failed_sites"):
            sections.append({"activityTitle": "❌ Failed Sites",
                             "text": "\n".join(f"• {x}" for x in s["failed_sites"][:10])})
        if s.get("errors"):
            sections.append({"activityTitle": "⚠️ Errors",
                             "text": "\n".join(f"• {str(e)[:120]}" for e in s["errors"][:5])})
        return {
            "@type": "MessageCard", "@context": "http://schema.org/extensions",
            "themeColor": "00CC00" if ok else "FF0000",
            "summary": "SharePoint Backup Report",
            "sections": sections,
        }

    # Email
    def _send_email(self, s):
        notif = self.config["notification"]
        ok = not s.get("failed_sites")
        subject = (
            f"[Microsoft 365 Backup] "
            f"{'SUCCESS ✅' if ok else 'PARTIAL FAILURE ⚠️'} — "
            f"{s.get('successful_sites', 0)}/{s.get('total_sites', 0)} sites, "
            f"{self._format_size(s.get('bytes_downloaded', 0))} — "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
        html = self._build_html(s)
        if notif.get("method") == "graph":
            from app.backup_engine import GraphAuth
            az = self.config["azure_ad"]
            auth = GraphAuth(az["tenant_id"], az["client_id"], az["client_secret"])
            token = auth.get_token()
            payload = {
                "message": {
                    "subject": subject,
                    "body": {"contentType": "HTML", "content": html},
                    "toRecipients": [{"emailAddress": {"address": a}} for a in notif["email_to"]],
                },
                "saveToSentItems": "false",
            }
            r = self.session.post(
                f"https://graph.microsoft.com/v1.0/users/{notif['email_from']}/sendMail",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=payload, timeout=(10, 30),
            )
            r.raise_for_status()
        else:
            smtp = notif["smtp"]
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = formataddr(("Microsoft 365 Backup", notif["email_from"]))
            msg["To"] = ", ".join(notif["email_to"])
            msg.attach(MIMEText(html, "html"))
            with smtplib.SMTP(smtp["server"], smtp["port"]) as srv:
                srv.starttls()
                srv.login(smtp["username"], smtp["password"])
                srv.send_message(msg)

    def _build_html(self, s):
        """DETAILED HTML email report."""
        ok = not s.get("failed_sites")
        status_color = "#10b981" if ok else "#ef4444"
        status_bg = "#d1fae5" if ok else "#fee2e2"
        status_text = "✅ BACKUP SUCCESSFUL" if ok else "⚠️ PARTIAL FAILURE"
        gradient = "linear-gradient(135deg,#6366f1,#06b6d4)" if ok else "linear-gradient(135deg,#ef4444,#f59e0b)"

        duration = self._calc_duration(s)
        avg_speed = self._calc_speed(s)
        size = self._format_size(s.get("bytes_downloaded", 0))
        start_str = self._format_time(s.get("start_time"))
        end_str = self._format_time(s.get("end_time"))

        # Per-site breakdown
        site_section = ""
        site_details = s.get("site_details", [])
        if site_details:
            site_rows = ""
            for sd in site_details:
                status_icon = "✅" if sd.get("status") == "success" else "❌"
                status_color_row = "#10b981" if sd.get("status") == "success" else "#ef4444"
                site_size = self._format_size(sd.get("size", 0)) if sd.get("size") else "—"
                files = f"{sd.get('files', 0):,}" if sd.get('files') else "0"
                site_rows += (
                    f'<tr>'
                    f'<td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;">{status_icon} <b>{sd.get("name","?")}</b></td>'
                    f'<td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;text-align:right;">{files}</td>'
                    f'<td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;text-align:right;">{site_size}</td>'
                    f'<td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;color:{status_color_row};font-weight:600;">{sd.get("status","").upper()}</td>'
                    f'</tr>'
                )
            site_section = (
                '<div style="padding:0 28px 12px;">'
                '<h3 style="margin:16px 0 12px;font-size:14px;color:#374151;text-transform:uppercase;letter-spacing:1px;border-bottom:2px solid #e5e7eb;padding-bottom:8px;">🌐 Per-Site Breakdown</h3>'
                '<table style="width:100%;border-collapse:collapse;font-size:13px;">'
                '<thead><tr style="background:#f3f4f6;">'
                '<th style="padding:10px 12px;text-align:left;color:#374151;font-weight:600;">Site</th>'
                '<th style="padding:10px 12px;text-align:right;color:#374151;font-weight:600;">Files</th>'
                '<th style="padding:10px 12px;text-align:right;color:#374151;font-weight:600;">Size</th>'
                '<th style="padding:10px 12px;text-align:left;color:#374151;font-weight:600;">Status</th>'
                '</tr></thead>'
                f'<tbody>{site_rows}</tbody>'
                '</table></div>'
            )

        # Failed sites
        failed_html = ""
        if s.get("failed_sites"):
            failed_items = "".join(
                f'<div style="padding:8px 12px;background:#fef2f2;border-left:3px solid #ef4444;margin-bottom:6px;border-radius:4px;">❌ <b>{x}</b></div>'
                for x in s["failed_sites"]
            )
            failed_html = (
                f'<div style="padding:0 28px 12px;margin-top:24px;">'
                f'<h3 style="color:#ef4444;margin-bottom:12px;font-size:16px;">❌ Failed Sites ({len(s["failed_sites"])})</h3>'
                f'{failed_items}</div>'
            )

        # Errors
        errors_html = ""
        if s.get("errors"):
            error_items = "".join(
                f'<div style="padding:10px 12px;background:#fef3c7;border-left:3px solid #f59e0b;margin-bottom:6px;border-radius:4px;font-family:monospace;font-size:12px;color:#78350f;">⚠️ {str(e)[:300]}</div>'
                for e in s["errors"][:10]
            )
            errors_html = (
                f'<div style="padding:0 28px 12px;margin-top:24px;">'
                f'<h3 style="color:#f59e0b;margin-bottom:12px;font-size:16px;">⚠️ Errors ({len(s["errors"])})</h3>'
                f'{error_items}</div>'
            )

        total_processed = s.get("files_downloaded", 0) + s.get("files_skipped", 0)
        skip_pct = (s.get("files_skipped", 0) / total_processed * 100) if total_processed else 0
        report_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S WIB")

        return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="font-family:'Segoe UI',Arial,sans-serif;background:#f3f4f6;margin:0;padding:20px;">
<div style="max-width:780px;margin:0 auto;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 4px 12px rgba(0,0,0,0.1);">

    <div style="background:{gradient};color:#fff;padding:32px 28px;">
        <h1 style="margin:0 0 8px;font-size:24px;font-weight:700;letter-spacing:-0.02em;">📦 SharePoint Backup Report</h1>
        <p style="margin:0;opacity:0.9;font-size:14px;">Digiserve by Telkom — Microsoft 365 Backup System</p>
    </div>

    <div style="padding:20px 28px 12px;">
        <div style="background:{status_bg};border-left:4px solid {status_color};padding:14px 18px;border-radius:6px;">
            <div style="color:{status_color};font-weight:700;font-size:18px;">{status_text}</div>
            <div style="color:#6b7280;font-size:13px;margin-top:4px;">{s.get('successful_sites', 0)} of {s.get('total_sites', 0)} sites backed up successfully</div>
        </div>
    </div>

    <div style="padding:0 28px 12px;">
        <table style="width:100%;border-collapse:separate;border-spacing:8px;">
            <tr>
                <td style="background:#f9fafb;padding:14px;border-radius:8px;border:1px solid #e5e7eb;width:25%;text-align:center;">
                    <div style="font-size:11px;color:#6b7280;text-transform:uppercase;font-weight:600;letter-spacing:0.5px;">Duration</div>
                    <div style="font-size:20px;font-weight:700;color:#111827;margin-top:4px;">{duration}</div>
                </td>
                <td style="background:#f9fafb;padding:14px;border-radius:8px;border:1px solid #e5e7eb;width:25%;text-align:center;">
                    <div style="font-size:11px;color:#6b7280;text-transform:uppercase;font-weight:600;letter-spacing:0.5px;">Avg Speed</div>
                    <div style="font-size:20px;font-weight:700;color:#111827;margin-top:4px;">{avg_speed}</div>
                </td>
                <td style="background:#f9fafb;padding:14px;border-radius:8px;border:1px solid #e5e7eb;width:25%;text-align:center;">
                    <div style="font-size:11px;color:#6b7280;text-transform:uppercase;font-weight:600;letter-spacing:0.5px;">Files DL</div>
                    <div style="font-size:20px;font-weight:700;color:#6366f1;margin-top:4px;">{s.get('files_downloaded',0):,}</div>
                </td>
                <td style="background:#f9fafb;padding:14px;border-radius:8px;border:1px solid #e5e7eb;width:25%;text-align:center;">
                    <div style="font-size:11px;color:#6b7280;text-transform:uppercase;font-weight:600;letter-spacing:0.5px;">Total Size</div>
                    <div style="font-size:20px;font-weight:700;color:#06b6d4;margin-top:4px;">{size}</div>
                </td>
            </tr>
        </table>
    </div>

    <div style="padding:0 28px 12px;">
        <h3 style="margin:16px 0 12px;font-size:14px;color:#374151;text-transform:uppercase;letter-spacing:1px;border-bottom:2px solid #e5e7eb;padding-bottom:8px;">📅 Timeline</h3>
        <table style="width:100%;font-size:13px;">
            <tr><td style="padding:6px 0;color:#6b7280;width:130px;">Start Time:</td><td style="padding:6px 0;color:#111827;font-weight:600;">{start_str}</td></tr>
            <tr><td style="padding:6px 0;color:#6b7280;">End Time:</td><td style="padding:6px 0;color:#111827;font-weight:600;">{end_str}</td></tr>
            <tr><td style="padding:6px 0;color:#6b7280;">Duration:</td><td style="padding:6px 0;color:#111827;font-weight:600;">{duration}</td></tr>
        </table>
    </div>

    <div style="padding:0 28px 12px;">
        <h3 style="margin:16px 0 12px;font-size:14px;color:#374151;text-transform:uppercase;letter-spacing:1px;border-bottom:2px solid #e5e7eb;padding-bottom:8px;">📊 File Statistics</h3>
        <table style="width:100%;border-collapse:collapse;font-size:14px;">
            <tr style="background:#f9fafb;">
                <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;font-weight:600;">📥 Files Downloaded (new/changed)</td>
                <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;text-align:right;color:#6366f1;font-weight:700;font-size:16px;">{s.get('files_downloaded',0):,}</td>
            </tr>
            <tr>
                <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;font-weight:600;">⏭️ Files Skipped (unchanged)</td>
                <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;text-align:right;color:#6b7280;font-weight:700;font-size:16px;">{s.get('files_skipped',0):,} ({skip_pct:.1f}%)</td>
            </tr>
            <tr style="background:#f9fafb;">
                <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;font-weight:600;">📁 Total Files Processed</td>
                <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;text-align:right;font-weight:700;font-size:16px;">{total_processed:,}</td>
            </tr>
            <tr>
                <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;font-weight:600;">💾 Total Downloaded Size</td>
                <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;text-align:right;color:#06b6d4;font-weight:700;font-size:16px;">{size}</td>
            </tr>
            <tr style="background:#f9fafb;">
                <td style="padding:10px 12px;font-weight:600;">⚡ Average Download Speed</td>
                <td style="padding:10px 12px;text-align:right;color:#10b981;font-weight:700;font-size:16px;">{avg_speed}</td>
            </tr>
        </table>
    </div>

    {site_section}
    {failed_html}
    {errors_html}

    <div style="padding:24px 28px;background:#f9fafb;border-top:1px solid #e5e7eb;margin-top:24px;">
        <table style="width:100%;font-size:12px;color:#6b7280;">
            <tr>
                <td style="vertical-align:top;">
                    <div style="font-weight:600;color:#374151;margin-bottom:4px;">📦 Microsoft 365 Backup</div>
                    <div>IT Infrastructure Team — Digiserve by Telkom</div>
                </td>
                <td style="text-align:right;vertical-align:top;">
                    <div style="font-weight:600;color:#374151;margin-bottom:4px;">Report Generated</div>
                    <div>{report_time}</div>
                </td>
            </tr>
        </table>
        <div style="margin-top:16px;padding-top:16px;border-top:1px solid #e5e7eb;font-size:11px;color:#9ca3af;text-align:center;">
            This is an automated email. Storage: <code>/backup/sharepoint</code>
        </div>
    </div>
</div>
</body>
</html>"""
