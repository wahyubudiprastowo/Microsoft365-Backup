#!/usr/bin/env python3
"""Map SharePoint site allowlist URLs to Microsoft 365 Group IDs via Graph."""
import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, "/app")

from app.backup_engine import GraphAuth  # noqa: E402
from app.config_manager import load_config  # noqa: E402


GRAPH = "https://graph.microsoft.com/v1.0"


def load_allowlist(path: Path):
    urls = []
    seen = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        url = raw.strip().rstrip("/")
        if not url or url.startswith("#") or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def graph_get(session, url, headers):
    for attempt in range(1, 8):
        resp = session.get(url, headers=headers, timeout=60)
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
        raise SystemExit("Usage: map_spo_sites_to_groups.py <allowlist.txt> <output.json>")
    allowlist_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2])
    cfg = load_config()
    az = cfg["azure_ad"]
    token = GraphAuth(az["tenant_id"], az["client_id"], az["client_secret"]).get_token()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    session = requests.Session()

    allow_urls = load_allowlist(allowlist_path)
    allowset = set(u.lower() for u in allow_urls)
    matches = {}
    groups_scanned = 0
    site_errors = []

    url = (
        f"{GRAPH}/groups?"
        "$select=id,displayName,mail,groupTypes,resourceProvisioningOptions,createdDateTime"
        "&$top=999"
    )
    while url:
        resp = graph_get(session, url, headers)
        if resp.status_code >= 400:
            raise SystemExit(f"List groups failed {resp.status_code}: {resp.text[:1000]}")
        payload = resp.json()
        for group in payload.get("value", []):
            groups_scanned += 1
            gid = group.get("id")
            if not gid:
                continue
            site_resp = graph_get(
                session,
                f"{GRAPH}/groups/{gid}/sites/root?$select=id,webUrl,displayName,name",
                headers,
            )
            if site_resp.status_code == 404:
                continue
            if site_resp.status_code >= 400:
                site_errors.append({
                    "group_id": gid,
                    "display_name": group.get("displayName"),
                    "status_code": site_resp.status_code,
                    "response": site_resp.text[:500],
                })
                continue
            site = site_resp.json()
            site_url = str(site.get("webUrl") or "").rstrip("/")
            if site_url.lower() not in allowset:
                continue
            matches[site_url] = {
                "site_url": site_url,
                "site_id": site.get("id"),
                "site_display_name": site.get("displayName"),
                "group_id": gid,
                "group_display_name": group.get("displayName"),
                "group_mail": group.get("mail"),
                "group_types": group.get("groupTypes"),
                "resource_provisioning_options": group.get("resourceProvisioningOptions"),
                "createdDateTime": group.get("createdDateTime"),
            }
            print(json.dumps({"matched": len(matches), "site_url": site_url, "group": group.get("displayName")}), flush=True)
        url = payload.get("@odata.nextLink")

    output = {
        "allowlist_count": len(allow_urls),
        "groups_scanned": groups_scanned,
        "matched_count": len(matches),
        "unmatched_count": len(allow_urls) - len(matches),
        "matches": matches,
        "unmatched": [u for u in allow_urls if u not in matches],
        "site_errors": site_errors,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(json.dumps({
        "done": True,
        "allowlist_count": output["allowlist_count"],
        "groups_scanned": groups_scanned,
        "matched_count": len(matches),
        "unmatched_count": output["unmatched_count"],
        "site_errors": len(site_errors),
        "output": str(output_path),
    }, indent=2), flush=True)


if __name__ == "__main__":
    main()
