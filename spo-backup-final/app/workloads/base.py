"""Base workload utilities for Microsoft Graph access."""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import msal
import requests

from app.http_utils import (
    build_retry_session,
    compute_backoff_delay,
    is_retryable_exception,
    is_retryable_status,
)
from app.task_control import check_control


class BaseWorkload:
    GRAPH = "https://graph.microsoft.com/v1.0"
    workload_type = "base"
    TARGET_COMPLETE_MARKER = "_backup_target_complete.json"
    CANONICAL_BACKUP_DIRNAME = "backup_current"
    STREAM_CHUNK_SIZE = max(65536, int(os.environ.get("GRAPH_DOWNLOAD_CHUNK_SIZE", "4194304")))

    def __init__(self, tenant_config):
        self.tenant = tenant_config
        self.tenant_id = tenant_config["tenant_id"]
        self.client_id = tenant_config["client_id"]
        self.client_secret = tenant_config["client_secret"]
        self.session = build_retry_session()
        self._token = None
        self._token_expiry = 0

    def get_token(self):
        if self._token and time.time() < self._token_expiry:
            return self._token
        app = msal.ConfidentialClientApplication(
            self.client_id,
            authority=f"https://login.microsoftonline.com/{self.tenant_id}",
            client_credential=self.client_secret,
        )
        result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" not in result:
            raise Exception(f"Auth failed: {result.get('error_description', 'Unknown')}")
        self._token = result["access_token"]
        self._token_expiry = time.time() + 3500
        return self._token

    def _headers(self):
        return {"Authorization": f"Bearer {self.get_token()}", "Content-Type": "application/json"}

    def _get(self, url, params=None, max_retry=3):
        response = None
        last_error = None
        for attempt in range(max_retry):
            try:
                response = self.session.get(url, headers=self._headers(), params=params, timeout=(20, 60))
                if response.status_code == 401 and attempt < max_retry - 1:
                    self._token = None
                    self._token_expiry = 0
                    time.sleep(1)
                    continue
                if is_retryable_status(response.status_code) and attempt < max_retry - 1:
                    time.sleep(compute_backoff_delay(attempt, response=response))
                    continue
                response.raise_for_status()
                return response.json()
            except Exception as e:
                last_error = e
                if not is_retryable_exception(e) or attempt == max_retry - 1:
                    raise
                time.sleep(compute_backoff_delay(attempt, response=response))
        if last_error:
            raise last_error
        return {}

    def _paginate(self, url, params=None):
        while url:
            data = self._get(url, params)
            for item in data.get("value", []):
                yield item
            url = data.get("@odata.nextLink")
            params = None

    def get_target_selection(self):
        raw = (self.tenant.get("workload_target_selection", {}) or {}).get(self.workload_type, {}) or {}
        mode = str(raw.get("mode") or "all").strip().lower()
        selected_ids = []
        for item in raw.get("selected_ids", []) or []:
            value = str(item or "").strip()
            if value:
                selected_ids.append(value)
        if mode != "selected" or not selected_ids:
            mode = "all"
            selected_ids = []
        return {
            "mode": mode,
            "selected_ids": selected_ids,
            "selected_count": len(selected_ids),
        }

    def apply_target_selection(self, targets):
        targets = list(targets or [])
        selection = self.get_target_selection()
        if selection["mode"] != "selected":
            return targets, {
                "mode": "all",
                "available_count": len(targets),
                "selected_count": 0,
                "effective_count": len(targets),
            }

        selected_ids = set(selection["selected_ids"])
        filtered = [
            item for item in targets
            if str(item.get("id") or "").strip() in selected_ids
        ]
        return filtered, {
            "mode": "selected",
            "available_count": len(targets),
            "selected_count": len(selected_ids),
            "effective_count": len(filtered),
        }

    def _load_json(self, path, default=None):
        try:
            with open(path, "r") as handle:
                return json.load(handle)
        except Exception:
            return {} if default is None else default

    def _iter_backup_dirs(self, backup_root):
        root = Path(backup_root)
        if not root.exists():
            return []
        return [
            entry for entry in root.iterdir()
            if entry.is_dir() and entry.name.startswith("backup_")
        ]

    def _backup_sort_key(self, path):
        path = Path(path)
        manifest = self._load_json(path / "_workload_manifest.json", default={})
        status = str(manifest.get("status") or "").strip().lower()
        priority = {
            "running": 6,
            "interrupted": 5,
            "partial": 4,
            "failed": 3,
            "cancelled": 2,
            "success": 1,
            "unknown": 0,
        }.get(status, 0)
        manifest_time = (
            manifest.get("end_time")
            or manifest.get("written_at")
            or manifest.get("generated_at")
            or manifest.get("start_time")
            or ""
        )
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0
        return (priority, manifest_time, mtime, path.name)

    def _is_resumable_backup_dir(self, path):
        path = Path(path)
        manifest = self._load_json(path / "_workload_manifest.json", default={})
        status = str(manifest.get("status") or "").strip().lower()
        if status in {"running", "interrupted", "cancelled", "failed", "partial"}:
            return True
        if status in {"success"}:
            return False
        for entry in path.iterdir():
            if entry.name.startswith("."):
                continue
            if entry.name.endswith(".tmp"):
                return True
            if entry.is_dir():
                return True
        return False

    def _resolve_backup_dir(self, backup_root):
        root = Path(backup_root)
        root.mkdir(parents=True, exist_ok=True)
        canonical_dir = root / self.CANONICAL_BACKUP_DIRNAME
        if canonical_dir.exists() and canonical_dir.is_dir():
            return canonical_dir, True
        candidates = self._iter_backup_dirs(root)
        if candidates:
            candidates.sort(key=self._backup_sort_key, reverse=True)
            return candidates[0], True
        backup_dir = canonical_dir
        backup_dir.mkdir(parents=True, exist_ok=True)
        return backup_dir, False

    def _write_target_complete_marker(self, target_dir, payload):
        marker_path = Path(target_dir) / self.TARGET_COMPLETE_MARKER
        with open(marker_path, "w") as handle:
            json.dump(payload, handle, indent=2, default=str)

    def _is_target_completed(self, target_dir):
        return (Path(target_dir) / self.TARGET_COMPLETE_MARKER).exists()

    def _update_run_manifest(self, backup_path, payload=None):
        backup_path = Path(backup_path)
        current = self._load_json(backup_path / "_workload_manifest.json", default={})
        current.update(payload or {})
        with open(backup_path / "_workload_manifest.json", "w") as handle:
            json.dump(current, handle, indent=2, default=str)

    @staticmethod
    def _format_speed_human(speed_bps: float) -> str:
        speed_bps = max(0.0, float(speed_bps or 0.0))
        if speed_bps >= 1024 * 1024 * 1024:
            return f"{speed_bps / 1024 / 1024 / 1024:.2f} GB/s"
        if speed_bps >= 1024 * 1024:
            return f"{speed_bps / 1024 / 1024:.2f} MB/s"
        if speed_bps >= 1024:
            return f"{speed_bps / 1024:.1f} KB/s"
        return f"{speed_bps:.0f} B/s"

    def _emit_file_progress(self, dest: str, bytes_written: int, total_size: int, started_at: float):
        emitter = getattr(self, "_emit", None)
        if not callable(emitter):
            return
        stats = getattr(self, "stats", {}) or {}
        base_bytes = int(stats.get("bytes_downloaded", 0) or 0)
        elapsed = max(0.001, time.monotonic() - started_at)
        speed_bps = max(0.0, float(bytes_written) / elapsed)
        current_file = dest
        backup_root = getattr(self, "backup_root", None)
        if backup_root:
            try:
                current_file = os.path.relpath(dest, backup_root)
            except Exception:
                current_file = dest
        emitter("file_progress", {
            "current_file": current_file,
            "current_file_size": max(int(total_size or 0), int(bytes_written or 0)),
            "current_file_done": int(bytes_written or 0),
            "bytes_downloaded": base_bytes + int(bytes_written or 0),
            "speed_bps": speed_bps,
            "speed_human": self._format_speed_human(speed_bps),
        })

    def _download(self, url, dest, size_hint=0, auth_required: bool = True):
        if os.path.exists(dest) and size_hint > 0 and os.path.getsize(dest) == size_hint:
            return size_hint
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".tmp"
        last_error = None
        for attempt in range(5):
            response = None
            resume_from = 0
            try:
                self._check_control()
                headers = self._headers() if auth_required else {}
                if os.path.exists(tmp):
                    resume_from = os.path.getsize(tmp)
                    if size_hint and resume_from >= size_hint:
                        os.replace(tmp, dest)
                        return size_hint
                    if resume_from > 0:
                        headers["Range"] = f"bytes={resume_from}-"

                response = self.session.get(url, headers=headers, stream=True, timeout=(20, 300))
                if is_retryable_status(response.status_code) and attempt < 4:
                    response.close()
                    time.sleep(compute_backoff_delay(attempt, response=response))
                    continue
                response.raise_for_status()

                bytes_written = resume_from
                total_size = int(size_hint or 0)
                content_range = response.headers.get("Content-Range", "")
                if not total_size and "/" in content_range:
                    try:
                        total_size = int(content_range.rsplit("/", 1)[-1])
                    except Exception:
                        total_size = 0
                if not total_size:
                    try:
                        total_size = int(response.headers.get("Content-Length") or 0)
                        if resume_from and response.status_code == 206:
                            total_size += resume_from
                    except Exception:
                        total_size = 0

                mode = "ab" if resume_from else "wb"
                if resume_from and response.status_code == 200:
                    mode = "wb"
                    bytes_written = 0
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                started_at = time.monotonic()
                last_progress_emit = 0.0
                with open(tmp, mode) as handle:
                    for chunk in response.iter_content(chunk_size=self.STREAM_CHUNK_SIZE):
                        if chunk:
                            self._check_control()
                            handle.write(chunk)
                            bytes_written += len(chunk)
                            now = time.monotonic()
                            if (now - last_progress_emit) >= 0.5:
                                self._emit_file_progress(dest, bytes_written, total_size, started_at)
                                last_progress_emit = now
                self._emit_file_progress(dest, bytes_written, total_size, started_at)
                os.replace(tmp, dest)
                return bytes_written
            except Exception as e:
                last_error = e
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        pass
                if not is_retryable_exception(e) or attempt == 4:
                    raise
                time.sleep(compute_backoff_delay(attempt))
        if last_error:
            raise last_error
        raise RuntimeError(f"Download failed for {dest}")

    def _check_control(self):
        task_id = getattr(self, "task_id", None)
        if task_id:
            check_control(task_id)
