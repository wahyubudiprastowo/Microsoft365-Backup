#!/usr/bin/env python3
"""Recover files deleted by delete_completed_m365_files.py back to SharePoint."""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, "/app")

from app.backup_engine import GraphAuth  # noqa: E402
from app.config_manager import load_config  # noqa: E402
from app.http_utils import build_retry_session  # noqa: E402


GRAPH = "https://graph.microsoft.com/v1.0"
SMALL_UPLOAD_LIMIT = 4 * 1024 * 1024
CHUNK_SIZE = 5 * 1024 * 1024


def load_json(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, payload: dict):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=str)
    tmp.replace(path)


def join_path(*parts):
    cleaned = [str(part or "").strip("/") for part in parts if str(part or "").strip("/")]
    return "/".join(cleaned)


class RecoverClient:
    def __init__(self, config):
        az = config["azure_ad"]
        self.auth = GraphAuth(az["tenant_id"], az["client_id"], az["client_secret"])
        self.session = build_retry_session()
        self.headers = {}
        self.refresh_token()

    def refresh_token(self):
        self.headers = {"Authorization": f"Bearer {self.auth.get_token()}"}

    def request(self, method, url, **kwargs):
        for attempt in range(1, 8):
            try:
                response = self.session.request(method, url, headers=self.headers, timeout=kwargs.pop("timeout", 120), **kwargs)
            except requests.RequestException as exc:
                if attempt == 7:
                    raise
                time.sleep(min(60, 2 ** attempt))
                continue
            if response.status_code == 401 and attempt < 7:
                self.refresh_token()
                continue
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 7:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = int(retry_after) if retry_after else min(120, 2 ** attempt)
                except ValueError:
                    delay = min(120, 2 ** attempt)
                time.sleep(delay)
                continue
            response.raise_for_status()
            return response

    def ensure_folder(self, drive_id, remote_folder):
        normalized = str(remote_folder or "").strip("/")
        if not normalized:
            return
        parent_id = "root"
        current = ""
        for segment in [part for part in normalized.split("/") if part]:
            current = join_path(current, segment)
            try:
                existing = self.request("GET", f"{GRAPH}/drives/{drive_id}/root:/{current}", timeout=60).json()
                parent_id = existing["id"]
                continue
            except Exception:
                pass
            created = self.request(
                "POST",
                f"{GRAPH}/drives/{drive_id}/items/{parent_id}/children",
                json={
                    "name": segment,
                    "folder": {},
                    "@microsoft.graph.conflictBehavior": "replace",
                },
                timeout=60,
            ).json()
            parent_id = created["id"]

    def upload_file(self, local_file: Path, drive_id: str, remote_path: str):
        size = local_file.stat().st_size
        remote_folder = str(Path(remote_path).parent).replace("\\", "/")
        if remote_folder == ".":
            remote_folder = ""
        self.ensure_folder(drive_id, remote_folder)
        if size < SMALL_UPLOAD_LIMIT:
            with local_file.open("rb") as handle:
                self.request(
                    "PUT",
                    f"{GRAPH}/drives/{drive_id}/root:/{remote_path}:/content",
                    data=handle,
                    timeout=180,
                )
            return size

        session = self.request(
            "POST",
            f"{GRAPH}/drives/{drive_id}/root:/{remote_path}:/createUploadSession",
            json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
            timeout=60,
        ).json()
        upload_url = session["uploadUrl"]
        with local_file.open("rb") as handle:
            start = 0
            while start < size:
                chunk = handle.read(CHUNK_SIZE)
                if not chunk:
                    break
                end = start + len(chunk) - 1
                response = self.session.put(
                    upload_url,
                    data=chunk,
                    headers={
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {start}-{end}/{size}",
                    },
                    timeout=300,
                )
                if response.status_code not in (200, 201, 202):
                    response.raise_for_status()
                start = end + 1
        return size


def build_recovery_items(delete_state):
    backup_dir = Path(delete_state["backup_dir"])
    items = []
    for key, item in (delete_state.get("deleted") or {}).items():
        local_path = Path(str(item.get("local_path") or ""))
        site_dir = backup_dir / str(item.get("site_name") or "").replace(" ", "_")
        try:
            rel_parts = local_path.relative_to(site_dir).parts
        except ValueError:
            # Fall back to removing the first path segment below the site dir
            rel_parts = local_path.parts[-1:]
        remote_parts = rel_parts[1:] if len(rel_parts) > 1 else rel_parts
        remote_path = "/".join(remote_parts)
        items.append({
            **item,
            "delete_key": key,
            "local_path": str(local_path),
            "remote_path": remote_path,
        })
    return items


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--delete-state", required=True)
    parser.add_argument("--recover-state", required=True)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    delete_state = load_json(Path(args.delete_state))
    recover_state_path = Path(args.recover_state)
    recover_state_path.parent.mkdir(parents=True, exist_ok=True)
    if recover_state_path.exists():
        state = load_json(recover_state_path)
    else:
        state = {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "restored": {},
            "missing_local": {},
            "failed": {},
        }

    cfg = load_config()
    client = RecoverClient(cfg)
    items = build_recovery_items(delete_state)
    state["planned_items"] = len(items)
    state["source_delete_state"] = str(args.delete_state)
    save_json(recover_state_path, state)

    print(json.dumps({"planned_items": len(items), "recover_state": str(recover_state_path)}, indent=2), flush=True)
    processed = 0
    for item in items:
        if args.limit and processed >= args.limit:
            break
        key = item["delete_key"]
        if key in state["restored"] or key in state["missing_local"]:
            continue
        local_path = Path(item["local_path"])
        if not local_path.exists() or not local_path.is_file():
            state["missing_local"][key] = {**item, "error": "Local backup file missing"}
            processed += 1
            continue
        try:
            uploaded = client.upload_file(local_path, item["drive_id"], item["remote_path"])
            state["restored"][key] = {
                **item,
                "bytes_uploaded": uploaded,
                "restored_at": datetime.now(timezone.utc).isoformat(),
            }
        except Exception as exc:
            state["failed"][key] = {**item, "error": str(exc)[:1000]}
        processed += 1
        if processed % 25 == 0:
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            save_json(recover_state_path, state)
            print(json.dumps({
                "processed_this_run": processed,
                "restored": len(state["restored"]),
                "missing_local": len(state["missing_local"]),
                "failed": len(state["failed"]),
            }), flush=True)

    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    state["finished_at"] = state["updated_at"]
    save_json(recover_state_path, state)
    print(json.dumps({
        "done": True,
        "processed_this_run": processed,
        "restored": len(state["restored"]),
        "missing_local": len(state["missing_local"]),
        "failed": len(state["failed"]),
        "recover_state": str(recover_state_path),
    }, indent=2), flush=True)


if __name__ == "__main__":
    main()
