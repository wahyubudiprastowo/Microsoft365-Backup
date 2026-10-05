#!/usr/bin/env python3
"""Delete Microsoft 365 groups mapped from an allowlisted site map."""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, "/app")

from app.backup_engine import GraphAuth  # noqa: E402
from app.config_manager import load_config  # noqa: E402


GRAPH = "https://graph.microsoft.com/v1.0"


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def request_delete(session, url, headers):
    for attempt in range(1, 8):
        resp = session.delete(url, headers=headers, timeout=60)
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < 7:
            retry_after = resp.headers.get("Retry-After")
            try:
                delay = int(retry_after) if retry_after else min(120, 2 ** attempt)
            except ValueError:
                delay = min(120, 2 ** attempt)
            time.sleep(delay)
            continue
        return resp


def main():
    if len(sys.argv) != 3:
        raise SystemExit("Usage: delete_m365_groups_from_site_map.py <site_to_group_map.json> <state.json>")
    map_path = Path(sys.argv[1])
    state_path = Path(sys.argv[2])
    mapping = load_json(map_path)
    matches = mapping.get("matches") or {}
    group_items = []
    seen = set()
    for site_url, item in matches.items():
        gid = item.get("group_id")
        if not gid or gid in seen:
            continue
        seen.add(gid)
        group_items.append({
            "group_id": gid,
            "site_url": site_url,
            "group_display_name": item.get("group_display_name"),
            "group_mail": item.get("group_mail"),
            "site_display_name": item.get("site_display_name"),
            "resource_provisioning_options": item.get("resource_provisioning_options"),
        })

    if state_path.exists():
        state = load_json(state_path)
    else:
        state = {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "deleted": {},
            "already_deleted": {},
            "failed": {},
            "skipped": {},
        }
    state["map_path"] = str(map_path)
    state["planned_groups"] = len(group_items)
    state["source_allowlist_count"] = mapping.get("allowlist_count")
    state["source_matched_sites"] = mapping.get("matched_count")
    state["source_unmatched_sites"] = mapping.get("unmatched_count")
    save_json(state_path, state)

    cfg = load_config()
    az = cfg["azure_ad"]
    token = GraphAuth(az["tenant_id"], az["client_id"], az["client_secret"]).get_token()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    session = requests.Session()

    print(json.dumps({"planned_groups": len(group_items), "state": str(state_path)}, indent=2), flush=True)
    processed = 0
    for item in group_items:
        gid = item["group_id"]
        if gid in state["deleted"] or gid in state["already_deleted"]:
            continue
        print(json.dumps({"processing": item["site_url"], "group": item["group_display_name"], "index": processed + 1, "planned_groups": len(group_items)}), flush=True)
        resp = request_delete(session, f"{GRAPH}/groups/{gid}", headers)
        if resp.status_code in (200, 202, 204):
            state["deleted"][gid] = {
                **item,
                "deleted_at": datetime.now(timezone.utc).isoformat(),
                "status_code": resp.status_code,
            }
        elif resp.status_code == 404:
            state["already_deleted"][gid] = {
                **item,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "status_code": resp.status_code,
                "response": resp.text[:1000],
            }
        else:
            state["failed"][gid] = {
                **item,
                "failed_at": datetime.now(timezone.utc).isoformat(),
                "status_code": resp.status_code,
                "response": resp.text[:2000],
            }
        processed += 1
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_json(state_path, state)
        if processed % 10 == 0:
            print(json.dumps({
                "processed_this_run": processed,
                "deleted": len(state["deleted"]),
                "already_deleted": len(state["already_deleted"]),
                "failed": len(state["failed"]),
            }), flush=True)

    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    state["finished_at"] = state["updated_at"]
    save_json(state_path, state)
    print(json.dumps({
        "done": True,
        "processed_this_run": processed,
        "deleted": len(state["deleted"]),
        "already_deleted": len(state["already_deleted"]),
        "failed": len(state["failed"]),
        "state": str(state_path),
    }, indent=2), flush=True)


if __name__ == "__main__":
    main()
