#!/usr/bin/env python3
"""Delete SharePoint files that are present in a completed backup manifest.

This intentionally deletes driveItems recorded in the backup manifest, not whole
sites. Microsoft Graph DELETE for SharePoint driveItems sends items to recycle
bin where supported by SharePoint.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

sys.path.insert(0, "/app")

from app.backup_engine import GraphAuth  # noqa: E402
from app.config_manager import load_config  # noqa: E402
from app.http_utils import build_retry_session  # noqa: E402


GRAPH = "https://graph.microsoft.com/v1.0"


def safe_manifest_name(name: str) -> str:
    return str(name or "").replace(" ", "_").replace("/", "_") + ".json"


def load_json(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def parse_dt(value: str):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def iter_site_plans(backup_dir: Path, manifest_dir: Path, min_site_backup_time=None):
    for meta_path in sorted(backup_dir.glob("*/_backup_metadata.json")):
        meta = load_json(meta_path)
        site_name = str(meta.get("site_name") or "").strip()
        if not site_name:
            continue
        backup_time = parse_dt(meta.get("backup_time"))
        if min_site_backup_time and (not backup_time or backup_time < min_site_backup_time):
            continue
        site_dir = meta_path.parent
        manifest_path = manifest_dir / safe_manifest_name(site_name)
        if not manifest_path.exists():
            yield {
                "site_name": site_name,
                "site_dir": str(site_dir),
                "error": f"Manifest not found: {manifest_path}",
                "items": [],
            }
            continue

        libraries = {
            str(lib.get("name") or ""): str(lib.get("id") or "")
            for lib in (meta.get("libraries") or [])
            if lib.get("name") and lib.get("id")
        }
        manifest = load_json(manifest_path)
        items = []
        for item_id, entry in manifest.items():
            local_path = Path(str(entry.get("path") or ""))
            try:
                rel_parts = local_path.relative_to(site_dir).parts
            except ValueError:
                rel_parts = ()
            library_name = rel_parts[0] if rel_parts else ""
            drive_id = libraries.get(library_name)
            if not drive_id and len(libraries) == 1:
                drive_id = next(iter(libraries.values()))
                library_name = next(iter(libraries.keys()))
            if not drive_id:
                items.append({
                    "item_id": item_id,
                    "name": entry.get("name"),
                    "error": f"Could not resolve drive for library '{library_name}'",
                })
                continue
            items.append({
                "item_id": item_id,
                "drive_id": drive_id,
                "library_name": library_name,
                "name": entry.get("name"),
                "size": entry.get("size", 0),
                "local_path": str(local_path),
            })
        yield {
            "site_name": site_name,
            "site_path": meta.get("site_path"),
            "site_dir": str(site_dir),
            "manifest_path": str(manifest_path),
            "items": items,
        }


def load_state(path: Path):
    if path.exists():
        return load_json(path)
    return {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "deleted": {},
        "not_found": {},
        "failed": {},
        "skipped": {},
    }


def save_state(path: Path, state: dict):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, default=str)
    tmp.replace(path)


def format_count(value):
    return f"{int(value or 0):,}"


def send_completion_email(config: dict, state: dict, recipient: str):
    notif = dict(config.get("notification") or {})
    if not recipient:
        return {"success": False, "message": "No recipient configured"}

    deleted = len(state.get("deleted") or {})
    not_found = len(state.get("not_found") or {})
    failed = len(state.get("failed") or {})
    skipped = len(state.get("skipped") or {})
    planned = int(state.get("planned_items") or 0)
    completed = deleted + not_found + failed + skipped
    ok = failed == 0
    subject = (
        f"[Microsoft 365 Delete] {'SUCCESS' if ok else 'COMPLETED WITH FAILURES'} - "
        f"{format_count(deleted)} deleted, {format_count(failed)} failed"
    )

    def sample_rows(items):
        rows = []
        for item in list(items.values())[:20]:
            rows.append(
                "<tr>"
                f"<td>{item.get('site_name','')}</td>"
                f"<td>{item.get('name','')}</td>"
                f"<td>{item.get('status_code') or item.get('error') or item.get('deleted_at') or ''}</td>"
                "</tr>"
            )
        return "".join(rows) or "<tr><td colspan='3'>None</td></tr>"

    html = f"""
    <html><body style="font-family:Arial,sans-serif;color:#111827">
      <h2>Microsoft 365 Delete Report</h2>
      <p>Status: <b style="color:{'#059669' if ok else '#dc2626'}">{'SUCCESS' if ok else 'COMPLETED WITH FAILURES'}</b></p>
      <table cellpadding="8" cellspacing="0" style="border-collapse:collapse">
        <tr><td>Backup dir</td><td><code>{state.get('backup_dir','')}</code></td></tr>
        <tr><td>Sites in scope</td><td>{format_count(state.get('site_count'))}</td></tr>
        <tr><td>Planned items</td><td>{format_count(planned)}</td></tr>
        <tr><td>Processed items</td><td>{format_count(completed)}</td></tr>
        <tr><td>Deleted</td><td>{format_count(deleted)}</td></tr>
        <tr><td>Not found</td><td>{format_count(not_found)}</td></tr>
        <tr><td>Failed</td><td>{format_count(failed)}</td></tr>
        <tr><td>Skipped</td><td>{format_count(skipped)}</td></tr>
        <tr><td>Started</td><td>{state.get('started_at','')}</td></tr>
        <tr><td>Finished</td><td>{state.get('finished_at','')}</td></tr>
      </table>
      <h3>Failed Samples</h3>
      <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">
        <tr><th>Site</th><th>Item</th><th>Error</th></tr>
        {sample_rows(state.get('failed') or {})}
      </table>
      <h3>Not Found Samples</h3>
      <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">
        <tr><th>Site</th><th>Item</th><th>Info</th></tr>
        {sample_rows(state.get('not_found') or {})}
      </table>
    </body></html>
    """

    method = str(notif.get("method") or "graph").lower()
    email_from = notif.get("email_from")
    if method == "graph":
        az = config["azure_ad"]
        token = GraphAuth(az["tenant_id"], az["client_id"], az["client_secret"]).get_token()
        session = build_retry_session()
        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": html},
                "toRecipients": [{"emailAddress": {"address": recipient}}],
            },
            "saveToSentItems": "false",
        }
        resp = session.post(
            f"https://graph.microsoft.com/v1.0/users/{email_from}/sendMail",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=(10, 30),
        )
        resp.raise_for_status()
        return {"success": True, "message": "Sent via Graph"}

    import smtplib

    smtp = notif["smtp"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = recipient
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP(smtp["server"], smtp["port"]) as srv:
        srv.starttls()
        srv.login(smtp["username"], smtp["password"])
        srv.send_message(msg)
    return {"success": True, "message": "Sent via SMTP"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup-dir", required=True)
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--min-site-backup-time", default="")
    parser.add_argument("--notify-email", default="")
    args = parser.parse_args()

    backup_dir = Path(args.backup_dir)
    if not backup_dir.exists():
        raise SystemExit(f"Backup directory not found: {backup_dir}")

    cfg = load_config()
    manifest_dir = Path(cfg["backup"]["manifest_dir"])
    auth_cfg = cfg["azure_ad"]
    auth = GraphAuth(auth_cfg["tenant_id"], auth_cfg["client_id"], auth_cfg["client_secret"])
    session = build_retry_session()
    state_path = Path(args.state_file)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = load_state(state_path)

    min_site_backup_time = parse_dt(args.min_site_backup_time)
    if args.min_site_backup_time and not min_site_backup_time:
        raise SystemExit(f"Invalid --min-site-backup-time: {args.min_site_backup_time}")

    plans = list(iter_site_plans(backup_dir, manifest_dir, min_site_backup_time=min_site_backup_time))
    all_items = []
    plan_errors = []
    for plan in plans:
        if plan.get("error"):
            plan_errors.append({"site": plan["site_name"], "error": plan["error"]})
        for item in plan["items"]:
            key = f"{item.get('drive_id')}:{item.get('item_id')}"
            item["site_name"] = plan["site_name"]
            item["site_path"] = plan.get("site_path")
            item["key"] = key
            all_items.append(item)

    state["backup_dir"] = str(backup_dir)
    state["site_count"] = len(plans)
    state["planned_items"] = len(all_items)
    state["plan_errors"] = plan_errors
    state["dry_run"] = bool(args.dry_run)
    state["min_site_backup_time"] = args.min_site_backup_time or None
    if args.notify_email:
        state["notify_email"] = args.notify_email
    save_state(state_path, state)

    print(json.dumps({
        "backup_dir": str(backup_dir),
        "site_count": len(plans),
        "planned_items": len(all_items),
        "plan_errors": len(plan_errors),
        "dry_run": bool(args.dry_run),
    }, indent=2))

    processed = 0
    token = auth.get_token()
    headers = {"Authorization": f"Bearer {token}"}

    for item in all_items:
        if args.limit and processed >= args.limit:
            break
        key = item["key"]
        if key in state["deleted"] or key in state["not_found"]:
            continue
        if item.get("error"):
            state["skipped"][key] = item
            processed += 1
            continue
        if args.dry_run:
            state["skipped"][key] = {**item, "reason": "dry_run"}
            processed += 1
            continue

        url = f"{GRAPH}/drives/{item['drive_id']}/items/{item['item_id']}"
        for attempt in range(1, 8):
            try:
                resp = session.delete(url, headers=headers, timeout=60)
            except requests.RequestException as exc:
                if attempt == 7:
                    state["failed"][key] = {**item, "error": str(exc)}
                    break
                time.sleep(min(60, 2 ** attempt))
                continue

            if resp.status_code in (200, 202, 204):
                state["deleted"][key] = {**item, "deleted_at": datetime.now(timezone.utc).isoformat()}
                break
            if resp.status_code == 404:
                state["not_found"][key] = {**item, "deleted_at": datetime.now(timezone.utc).isoformat()}
                break
            if resp.status_code == 401 and attempt < 7:
                token = auth.get_token()
                headers = {"Authorization": f"Bearer {token}"}
                continue
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 7:
                retry_after = resp.headers.get("Retry-After")
                try:
                    delay = int(retry_after) if retry_after else min(120, 2 ** attempt)
                except ValueError:
                    delay = min(120, 2 ** attempt)
                time.sleep(delay)
                continue
            state["failed"][key] = {
                **item,
                "status_code": resp.status_code,
                "response": resp.text[:1000],
            }
            break

        processed += 1
        if processed % 25 == 0:
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            save_state(state_path, state)
            print(json.dumps({
                "processed_this_run": processed,
                "deleted": len(state["deleted"]),
                "not_found": len(state["not_found"]),
                "failed": len(state["failed"]),
                "skipped": len(state["skipped"]),
            }))

    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    state["finished_at"] = state["updated_at"]
    if args.notify_email and not args.dry_run:
        try:
            state["email_result"] = send_completion_email(cfg, state, args.notify_email)
        except Exception as exc:
            state["email_result"] = {"success": False, "message": str(exc)}
    save_state(state_path, state)
    print(json.dumps({
        "done": True,
        "processed_this_run": processed,
        "deleted": len(state["deleted"]),
        "not_found": len(state["not_found"]),
        "failed": len(state["failed"]),
        "skipped": len(state["skipped"]),
        "state_file": str(state_path),
    }, indent=2))


if __name__ == "__main__":
    main()
