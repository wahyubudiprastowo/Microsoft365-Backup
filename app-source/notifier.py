"""Multi-channel notification dispatcher: Email, Telegram, Teams."""
import logging, requests, smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

log = logging.getLogger("spo_backup")


class NotificationDispatcher:
    def __init__(self, config):
        self.config = config

    def send_all(self, stats):
        results = []
        notif = self.config.get("notification", {})

        # Telegram
        tg = notif.get("telegram", {})
        if tg.get("enabled") and tg.get("bot_token"):
            try:
                msg = self._build_telegram(stats)
                for chat_id in tg.get("chat_ids", []):
                    r = requests.post(
                        f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage",
                        json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
                        timeout=30,
                    )
                    r.raise_for_status()
                results.append({"channel": "telegram", "success": True, "message": "Sent"})
            except Exception as e:
                results.append({"channel": "telegram", "success": False, "message": str(e)})

        # Teams
        teams = notif.get("teams", {})
        if teams.get("enabled"):
            try:
                card = self._build_teams(stats)
                for url in teams.get("webhook_urls", []):
                    requests.post(url, json=card, timeout=30)
                results.append({"channel": "teams", "success": True, "message": "Sent"})
            except Exception as e:
                results.append({"channel": "teams", "success": False, "message": str(e)})

        # Email
        if notif.get("enabled"):
            try:
                self._send_email(stats)
                results.append({"channel": "email", "success": True, "message": "Sent"})
            except Exception as e:
                results.append({"channel": "email", "success": False, "message": str(e)})

        return results

    def send_test(self, channel=None):
        test_stats = {
            "total_sites": 3, "successful_sites": 3, "failed_sites": [],
            "files_downloaded": 50, "files_skipped": 100, "bytes_downloaded": 10485760,
            "errors": [], "start_time": datetime.now().isoformat(),
            "end_time": datetime.now().isoformat(),
        }
        return self.send_all(test_stats)

    def _build_telegram(self, s):
        ok = not s.get("failed_sites")
        return (
            f"{'🟢' if ok else '🔴'} <b>SharePoint Backup Report</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Status:</b> {'✅ SUCCESS' if ok else '⚠️ PARTIAL FAILURE'}\n"
            f"<b>Sites:</b> {s.get('successful_sites',0)}/{s.get('total_sites',0)}\n"
            f"<b>Files:</b> {s.get('files_downloaded',0)} downloaded, {s.get('files_skipped',0)} skipped\n"
            f"<b>Size:</b> {s.get('bytes_downloaded',0)/1024/1024:.2f} MB"
        )

    def _build_teams(self, s):
        ok = not s.get("failed_sites")
        return {
            "@type": "MessageCard", "@context": "http://schema.org/extensions",
            "themeColor": "00CC00" if ok else "FF0000",
            "summary": "SharePoint Backup Report",
            "sections": [{
                "activityTitle": "📦 SharePoint Backup",
                "activitySubtitle": "✅ SUCCESS" if ok else "⚠️ FAILURE",
                "facts": [
                    {"name": "Sites", "value": f"{s.get('successful_sites',0)}/{s.get('total_sites',0)}"},
                    {"name": "Downloaded", "value": str(s.get('files_downloaded',0))},
                    {"name": "Size", "value": f"{s.get('bytes_downloaded',0)/1024/1024:.2f} MB"},
                ],
            }],
        }

    def _send_email(self, s):
        notif = self.config["notification"]
        subject = f"[SPO Backup] {'SUCCESS' if not s.get('failed_sites') else 'PARTIAL FAILURE'} — {datetime.now().strftime('%Y-%m-%d %H:%M')}"
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
            r = requests.post(
                f"https://graph.microsoft.com/v1.0/users/{notif['email_from']}/sendMail",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=payload, timeout=30,
            )
            r.raise_for_status()
        else:
            smtp = notif["smtp"]
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = notif["email_from"]
            msg["To"] = ", ".join(notif["email_to"])
            msg.attach(MIMEText(html, "html"))
            with smtplib.SMTP(smtp["server"], smtp["port"]) as srv:
                srv.starttls()
                srv.login(smtp["username"], smtp["password"])
                srv.send_message(msg)

    def _build_html(self, s):
        ok = not s.get("failed_sites")
        color = "#10b981" if ok else "#ef4444"
        status = "✅ SUCCESS" if ok else "⚠️ PARTIAL FAILURE"
        return f"""<html><body style="font-family:'Segoe UI',Arial,sans-serif;margin:20px;">
<div style="background:linear-gradient(135deg,#6366f1,#06b6d4);color:#fff;padding:20px;border-radius:8px 8px 0 0;">
<h2 style="margin:0;">📦 SharePoint Backup Report</h2></div>
<div style="border:1px solid #dee2e6;border-top:none;padding:20px;border-radius:0 0 8px 8px;">
<div style="background:{color};color:#fff;padding:10px 15px;border-radius:5px;font-weight:bold;">{status}</div>
<table style="width:100%;margin-top:15px;border-collapse:collapse;">
<tr><td style="padding:8px;border-bottom:1px solid #eee;"><b>Sites</b></td><td>{s.get('successful_sites',0)}/{s.get('total_sites',0)}</td></tr>
<tr><td style="padding:8px;border-bottom:1px solid #eee;"><b>Files Downloaded</b></td><td>{s.get('files_downloaded',0)}</td></tr>
<tr><td style="padding:8px;border-bottom:1px solid #eee;"><b>Files Skipped</b></td><td>{s.get('files_skipped',0)}</td></tr>
<tr><td style="padding:8px;border-bottom:1px solid #eee;"><b>Total Size</b></td><td>{s.get('bytes_downloaded',0)/1024/1024:.2f} MB</td></tr>
</table>
<p style="color:#6c757d;font-size:12px;margin-top:20px;">SPO Backup System v4.0 | IT Infrastructure Team</p>
</div></body></html>"""
