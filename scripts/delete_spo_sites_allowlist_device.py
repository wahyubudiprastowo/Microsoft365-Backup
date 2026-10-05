#!/usr/bin/env python3
"""Move SharePoint Online sites from an allowlist to the tenant recycle bin.

Uses delegated device-code auth against the SharePoint admin resource so an SPO
admin can authorize from a browser. The script refuses to process URLs outside
the allowlist file.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import msal
import requests


def load_urls(path: Path):
    urls = []
    seen = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        url = raw.strip().rstrip("/")
        if not url or url.startswith("#"):
            continue
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def load_state(path: Path):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "deleted": {},
        "already_deleted": {},
        "failed": {},
        "skipped": {},
    }


def save_state(path: Path, state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def request_json(session, method, url, headers, **kwargs):
    for attempt in range(1, 8):
        timeout = kwargs.pop("timeout", 30)
        try:
            resp = session.request(method, url, headers=headers, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            if attempt >= 7:
                raise
            time.sleep(min(30, 2 ** attempt))
            continue
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < 7:
            retry_after = resp.headers.get("Retry-After")
            try:
                delay = int(retry_after) if retry_after else min(120, 2 ** attempt)
            except ValueError:
                delay = min(120, 2 ** attempt)
            time.sleep(delay)
            continue
        return resp


def get_digest(session, admin_url, token):
    resp = request_json(
        session,
        "POST",
        f"{admin_url}/_api/contextinfo",
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json;odata=nometadata",
            "Content-Type": "application/json;odata=nometadata",
        },
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"contextinfo failed {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    return (
        data.get("FormDigestValue")
        or data.get("d", {}).get("GetContextWebInformation", {}).get("FormDigestValue")
    )


def remove_site(session, admin_url, token, digest, site_url):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json;odata=nometadata",
        "Content-Type": "application/json;odata=nometadata",
    }
    if digest:
        headers["X-RequestDigest"] = digest
    payload_variants = [
        {"siteUrl": site_url},
        {"url": site_url},
    ]
    last_resp = None
    endpoint_variants = [
        f"{admin_url}/_api/SPO.Tenant/removeSite",
        f"{admin_url}/_api/SPO.Tenant/RemoveSite",
    ]
    for endpoint in endpoint_variants:
        for payload in payload_variants:
            resp = request_json(session, "POST", endpoint, headers, json=payload, timeout=20)
            last_resp = resp
            if resp.status_code in (200, 202, 204):
                return resp
            if resp.status_code not in (400, 404):
                return resp
    return last_resp


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--client-id", default="1950a258-227b-4e31-a9cf-717495945fc2", help="Public client id for device login")
    parser.add_argument("--admin-url", required=True)
    parser.add_argument("--scope-resource-url", default="", help="SharePoint resource URL for delegated scope, defaults to --admin-url")
    parser.add_argument("--allowlist", required=True)
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    allowlist_path = Path(args.allowlist)
    state_path = Path(args.state_file)
    urls = load_urls(allowlist_path)
    allowset = set(urls)
    state = load_state(state_path)
    state["admin_url"] = args.admin_url.rstrip("/")
    state["allowlist"] = str(allowlist_path)
    state["planned_sites"] = len(urls)
    save_state(state_path, state)

    app = msal.PublicClientApplication(args.client_id, authority=f"https://login.microsoftonline.com/{args.tenant_id}")
    scope_resource_url = (args.scope_resource_url or args.admin_url).rstrip("/")
    scopes = [f"{scope_resource_url}/AllSites.FullControl"]
    flow = app.initiate_device_flow(scopes=scopes)
    if "user_code" not in flow:
        raise SystemExit(f"Failed to create device flow: {flow}")
    print(flow["message"], flush=True)
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise SystemExit(f"Login failed: {json.dumps(result, indent=2)}")

    token = result["access_token"]
    session = requests.Session()
    digest = get_digest(session, args.admin_url.rstrip("/"), token)
    print(json.dumps({"authenticated": True, "planned_sites": len(urls), "digest": bool(digest)}, indent=2), flush=True)

    processed = 0
    for site_url in urls:
        if args.limit and processed >= args.limit:
            break
        if site_url not in allowset:
            state["skipped"][site_url] = {"url": site_url, "error": "URL not in allowlist"}
            processed += 1
            continue
        if site_url in state["deleted"] or site_url in state["already_deleted"]:
            continue
        print(json.dumps({"processing": site_url, "index": processed + 1, "planned_sites": len(urls)}), flush=True)
        try:
            resp = remove_site(session, args.admin_url.rstrip("/"), token, digest, site_url)
        except Exception as exc:
            state["failed"][site_url] = {
                "url": site_url,
                "error": str(exc)[:2000],
                "failed_at": datetime.now(timezone.utc).isoformat(),
            }
            processed += 1
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            save_state(state_path, state)
            continue
        if resp.status_code in (200, 202, 204):
            state["deleted"][site_url] = {
                "url": site_url,
                "deleted_at": datetime.now(timezone.utc).isoformat(),
                "status_code": resp.status_code,
            }
        elif resp.status_code == 404:
            state["already_deleted"][site_url] = {
                "url": site_url,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "status_code": resp.status_code,
                "response": resp.text[:1000],
            }
        else:
            state["failed"][site_url] = {
                "url": site_url,
                "status_code": resp.status_code,
                "response": resp.text[:2000],
                "failed_at": datetime.now(timezone.utc).isoformat(),
            }
        processed += 1
        if processed % 10 == 0:
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            save_state(state_path, state)
            print(json.dumps({
                "processed_this_run": processed,
                "deleted": len(state["deleted"]),
                "already_deleted": len(state["already_deleted"]),
                "failed": len(state["failed"]),
                "skipped": len(state["skipped"]),
            }), flush=True)

    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    state["finished_at"] = state["updated_at"]
    save_state(state_path, state)
    print(json.dumps({
        "done": True,
        "processed_this_run": processed,
        "deleted": len(state["deleted"]),
        "already_deleted": len(state["already_deleted"]),
        "failed": len(state["failed"]),
        "skipped": len(state["skipped"]),
        "state_file": str(state_path),
    }, indent=2), flush=True)


if __name__ == "__main__":
    main()
