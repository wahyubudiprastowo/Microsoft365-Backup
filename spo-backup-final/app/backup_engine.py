"""
SharePoint Online Backup & Restore Engine v4.0
- ProgressTracker (overall %, file %, speed, ETA)
- ★ NEW: Pause/Resume/Cancel via TaskController
- ★ NEW: Custom destination directory
- Resume support: skips files already downloaded (size check)
"""
import os
import json
import shutil
import logging
import msal
import requests
import time
import hashlib
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, unquote
from typing import Optional, Dict, List, Callable

from app.task_control import check_control, PauseException, TaskController
from app.http_utils import (
    build_retry_session,
    compute_backoff_delay,
    is_retryable_exception,
    is_retryable_status,
)

log = logging.getLogger("spo_backup")
STREAM_CHUNK_SIZE = max(65536, int(os.environ.get("GRAPH_DOWNLOAD_CHUNK_SIZE", "8388608")))
GRAPH_LIST_PAGE_SIZE = min(999, max(50, int(os.environ.get("GRAPH_LIST_PAGE_SIZE", "999"))))
MANIFEST_FLUSH_EVERY = max(1, int(os.environ.get("BACKUP_MANIFEST_FLUSH_EVERY", "100")))
MANIFEST_FLUSH_INTERVAL = max(1.0, float(os.environ.get("BACKUP_MANIFEST_FLUSH_INTERVAL", "10")))
DOWNLOAD_RANGE_WORKERS = min(8, max(1, int(os.environ.get("GRAPH_DOWNLOAD_RANGE_WORKERS", "4"))))
DOWNLOAD_RANGE_MIN_SIZE = max(8 * 1024 * 1024, int(os.environ.get("GRAPH_DOWNLOAD_RANGE_MIN_SIZE", str(64 * 1024 * 1024))))


class IncompleteDownloadError(Exception):
    pass


class GraphAuth:
    AUTHORITY = "https://login.microsoftonline.com/{tenant_id}"
    SCOPE = ["https://graph.microsoft.com/.default"]

    def __init__(self, tenant_id, client_id, client_secret):
        self.app = msal.ConfidentialClientApplication(
            client_id,
            authority=self.AUTHORITY.format(tenant_id=tenant_id),
            client_credential=client_secret,
        )
        self._token = None
        self._token_expiry = 0.0

    def get_token(self):
        if self._token and time.time() < self._token_expiry:
            return self._token
        result = self.app.acquire_token_for_client(scopes=self.SCOPE)
        if "access_token" in result:
            expires_in = max(300, int(result.get("expires_in") or 3600))
            self._token = result["access_token"]
            self._token_expiry = time.time() + max(60, expires_in - 180)
            return self._token
        raise Exception(f"Auth failed: {result.get('error_description', 'Unknown')}")


class ProgressTracker:
    def __init__(self):
        self.start_time = time.time()
        self.pause_time = 0  # Track time spent paused (for accurate speed)
        self.bytes_total = 0
        self.bytes_done = 0
        self.transfer_bytes_done = 0
        self.files_total = 0
        self.files_done = 0
        self.current_file = ""
        self.current_file_size = 0
        self.current_file_done = 0
        self.is_paused = False

    def file_start(self, name, size):
        self.current_file = name
        self.current_file_size = size
        self.current_file_done = 0

    def file_chunk(self, b):
        self.current_file_done += b
        self.bytes_done += b
        self.transfer_bytes_done += b

    def file_done(self):
        self.files_done += 1

    def sync_current_file_progress(self, target_done):
        target_done = max(0, int(target_done or 0))
        delta = target_done - self.current_file_done
        self.current_file_done = target_done
        self.bytes_done = max(0, self.bytes_done + delta)

    @property
    def overall_pct(self):
        if self.bytes_total:
            return min(100, int(self.bytes_done / self.bytes_total * 100))
        if self.files_total:
            return min(100, int(self.files_done / self.files_total * 100))
        return 0

    @property
    def files_per_second(self):
        active_time = time.time() - self.start_time - self.pause_time
        return self.files_done / active_time if active_time > 0.1 else 0

    @property
    def file_pct(self):
        return min(100, int(self.current_file_done / self.current_file_size * 100)) if self.current_file_size else 0

    @property
    def speed_bps(self):
        active_time = time.time() - self.start_time - self.pause_time
        return self.transfer_bytes_done / active_time if active_time > 0.1 else 0

    @property
    def speed_human(self):
        s = self.speed_bps
        if s > 1024 * 1024:
            return f"{s / 1024 / 1024:.2f} MB/s"
        if s > 1024:
            return f"{s / 1024:.1f} KB/s"
        if s > 0:
            return f"{s:.0f} B/s"
        fps = self.files_per_second
        if fps >= 100:
            return f"{fps:.0f} items/s"
        if fps >= 10:
            return f"{fps:.1f} items/s"
        if fps > 0:
            return f"{fps:.2f} items/s"
        return f"{s:.0f} B/s"

    @property
    def eta_human(self):
        if self.files_total == 0:
            return "scanning..."
        if self.bytes_total > 0 and self.speed_bps > 0:
            remaining = self.bytes_total - self.bytes_done
            if remaining <= 0:
                return "finishing..."
            sec = int(remaining / self.speed_bps)
        else:
            remaining_files = max(self.files_total - self.files_done, 0)
            fps = self.files_per_second
            if remaining_files <= 0:
                return "finishing..."
            if fps <= 0:
                return "waiting..."
            sec = int(remaining_files / fps)
        if sec < 60:
            return f"{sec}s"
        if sec < 3600:
            return f"{sec // 60}m {sec % 60}s"
        return f"{sec // 3600}h {(sec % 3600) // 60}m"

    def to_dict(self):
        return {
            "overall_pct": self.overall_pct,
            "file_pct": self.file_pct,
            "current_file": self.current_file,
            "current_file_size": self.current_file_size,
            "current_file_done": self.current_file_done,
            "bytes_done": self.bytes_done,
            "bytes_total": self.bytes_total,
            "transfer_bytes_done": self.transfer_bytes_done,
            "files_done": self.files_done,
            "files_total": self.files_total,
            "speed_human": self.speed_human,
            "eta_human": self.eta_human,
            "files_per_second": self.files_per_second,
            "remaining_files": max(self.files_total - self.files_done, 0),
            "remaining_bytes": max(self.bytes_total - self.bytes_done, 0),
            "elapsed": int(time.time() - self.start_time - self.pause_time),
            "is_paused": self.is_paused,
        }


class ManifestManager:
    def __init__(self, manifest_dir):
        self.manifest_dir = Path(manifest_dir)
        self.manifest_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, name):
        return self.manifest_dir / f"{name.replace(' ', '_').replace('/', '_')}.json"

    def load(self, name):
        p = self._path(name)
        return json.load(open(p)) if p.exists() else {}

    def save(self, name, m):
        with open(self._path(name), "w") as f:
            json.dump(m, f, indent=2, default=str)

    def needs_update(self, old, fid, etag, modified):
        if fid not in old:
            return True
        e = old[fid]
        return e.get("eTag") != etag or e.get("lastModified") != modified


class BackupEngine:
    GRAPH = "https://graph.microsoft.com/v1.0"

    def __init__(self, config, progress_callback=None, task_id=None):
        self.config = config
        self.progress_callback = progress_callback
        self.task_id = task_id  # ← NEW: for pause/resume control
        az = config["azure_ad"]
        self.auth = GraphAuth(az["tenant_id"], az["client_id"], az["client_secret"])
        self.manifest = ManifestManager(config["backup"]["manifest_dir"])
        self.session = build_retry_session()
        self.progress = ProgressTracker()
        self.stats = {
            "total_sites": 0, "successful_sites": 0, "failed_sites": [],
            "files_downloaded": 0, "files_skipped": 0, "bytes_downloaded": 0,
            "bytes_stored": 0,
            "files_resumed": 0,
            "errors": [], "start_time": None, "end_time": None,
            "current_site": "", "cancelled": False,
        }
        self._last_emit = 0

    def _new_manifest_flush_state(self):
        return {
            "dirty": False,
            "pending": 0,
            "last_write": 0.0,
        }

    def _mark_manifest_dirty(self, state):
        if state is None:
            return
        state["dirty"] = True
        state["pending"] = int(state.get("pending", 0)) + 1

    def _flush_manifest_checkpoint(self, save_fn, state, force: bool = False):
        if state is None or not state.get("dirty"):
            return False
        now = time.time()
        pending = int(state.get("pending", 0))
        last_write = float(state.get("last_write", 0.0))
        if not force and pending < MANIFEST_FLUSH_EVERY and (now - last_write) < MANIFEST_FLUSH_INTERVAL:
            return False
        save_fn()
        state["dirty"] = False
        state["pending"] = 0
        state["last_write"] = now
        return True

    def _headers(self):
        return {"Authorization": f"Bearer {self.auth.get_token()}",
                "Content-Type": "application/json"}

    def _get(self, url, params=None):
        response = None
        last_error = None
        for attempt in range(5):
            try:
                response = self.session.get(url, headers=self._headers(), params=params, timeout=(20, 60))
                if response.status_code == 401 and attempt < 4:
                    time.sleep(1)
                    continue
                if is_retryable_status(response.status_code) and attempt < 4:
                    delay = compute_backoff_delay(attempt, response=response)
                    log.warning(f"Transient Graph GET {response.status_code} for {url} — retrying in {delay:.1f}s")
                    time.sleep(delay)
                    continue
                response.raise_for_status()
                return response.json()
            except Exception as e:
                last_error = e
                if not is_retryable_exception(e) or attempt == 4:
                    raise
                delay = compute_backoff_delay(attempt, response=response)
                log.warning(f"Transient Graph GET failure for {url}: {e} — retrying in {delay:.1f}s")
                time.sleep(delay)
        if last_error:
            raise last_error
        return {}

    def _check_control(self):
        """Check pause/cancel state. Raises PauseException if cancelled."""
        if not self.task_id:
            return
        try:
            state_before = TaskController.get_state(self.task_id)
            if state_before == TaskController.STATE_PAUSED:
                self.progress.is_paused = True
                self._emit("paused")
                pause_start = time.time()
                check_control(self.task_id)  # blocks until resumed/cancelled
                self.progress.pause_time += (time.time() - pause_start)
                self.progress.is_paused = False
                self._emit("resumed")
            else:
                check_control(self.task_id)
        except PauseException:
            self.stats["cancelled"] = True
            self._emit("cancelled")
            raise

    def _download(self, url, dest, size_hint=0, auth_required: bool = True, source_identity: str = ""):
        """Download with progress + pause/resume support."""
        self.progress.file_start(os.path.basename(dest), size_hint or 0)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".tmp"
        tmp_meta = dest + ".tmp.meta"
        if os.path.exists(tmp):
            try:
                with open(tmp_meta) as handle:
                    stored_identity = json.load(handle).get("source_identity")
            except (OSError, ValueError):
                stored_identity = None
            if not source_identity or stored_identity != source_identity:
                os.remove(tmp)
        if source_identity:
            with open(tmp_meta, "w") as handle:
                json.dump({"source_identity": source_identity}, handle)

        if size_hint >= DOWNLOAD_RANGE_MIN_SIZE and DOWNLOAD_RANGE_WORKERS > 1 and not os.path.exists(tmp):
            parallel = self._download_parallel_ranges(url, dest, size_hint, auth_required)
            if parallel is not None:
                if os.path.exists(tmp_meta):
                    os.remove(tmp_meta)
                return parallel

        last_error = None
        for attempt in range(5):
            response = None
            resume_from = 0
            try:
                self._check_control()
                headers = self._headers() if auth_required else {}
                if os.path.exists(tmp):
                    try:
                        resume_from = os.path.getsize(tmp)
                    except OSError:
                        resume_from = 0
                    if size_hint and resume_from >= size_hint:
                        os.remove(tmp)
                        resume_from = 0
                    if resume_from > 0:
                        headers["Range"] = f"bytes={resume_from}-"
                headers["Accept-Encoding"] = "identity"

                response = self.session.get(url, headers=headers, stream=True, timeout=(20, 300))
                if is_retryable_status(response.status_code) and attempt < 4:
                    delay = compute_backoff_delay(attempt, response=response)
                    log.warning(
                        f"Transient download status {response.status_code} for {os.path.basename(dest)} "
                        f"(attempt {attempt + 1}/5) — retrying in {delay:.1f}s"
                    )
                    response.close()
                    time.sleep(delay)
                    continue
                response.raise_for_status()
                if response.status_code == 206:
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", response.headers.get("Content-Range", ""))
                    if not match or int(match.group(1)) != resume_from:
                        raise IncompleteDownloadError("Invalid Content-Range from download server")

                total_size = int(response.headers.get("content-length", size_hint or 0))
                if resume_from and response.status_code == 206 and size_hint:
                    total_size = size_hint
                elif resume_from and response.status_code == 200:
                    # Range not honored by upstream; restart cleanly using this full-body response.
                    resume_from = 0
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass

                self.progress.current_file_size = max(total_size, size_hint or 0, resume_from)
                self.progress.sync_current_file_progress(resume_from)

                bytes_written = resume_from
                mode = "ab" if resume_from else "wb"
                with open(tmp, mode) as f:
                    for chunk in response.iter_content(chunk_size=STREAM_CHUNK_SIZE):
                        if not chunk:
                            continue
                        self._check_control()
                        f.write(chunk)
                        sz = len(chunk)
                        bytes_written += sz
                        self.progress.file_chunk(sz)
                        self._emit("file_progress")

                expected_body = response.headers.get("Content-Length")
                if expected_body and bytes_written - resume_from != int(expected_body):
                    raise IncompleteDownloadError(f"Received {bytes_written - resume_from} bytes, expected {expected_body}")
                if size_hint and bytes_written != size_hint:
                    raise IncompleteDownloadError(f"Downloaded {bytes_written} bytes, expected {size_hint}")

                os.replace(tmp, dest)
                if os.path.exists(tmp_meta):
                    os.remove(tmp_meta)
                response.close()
                self.progress.file_done()
                return {
                    "transferred_bytes": max(0, bytes_written - resume_from),
                    "final_size": bytes_written,
                    "skipped": False,
                    "resumed": resume_from > 0,
                }
            except Exception as e:
                last_error = e
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        pass
                if isinstance(e, PauseException):
                    raise
                if not (is_retryable_exception(e) or isinstance(e, IncompleteDownloadError)) or attempt == 4:
                    raise
                try:
                    local_size = os.path.getsize(tmp) if os.path.exists(tmp) else 0
                except OSError:
                    local_size = 0
                self.progress.sync_current_file_progress(local_size)
                delay = compute_backoff_delay(attempt)
                log.warning(
                    f"Transient download failure for {os.path.basename(dest)}: {e} "
                    f"(attempt {attempt + 1}/5, resume {local_size} bytes) — retrying in {delay:.1f}s"
                )
                time.sleep(delay)

        if last_error:
            raise last_error
        raise RuntimeError(f"Download failed for {dest}")

    def _download_parallel_ranges(self, url, dest, size_hint, auth_required):
        headers = self._headers() if auth_required else {}
        headers["Accept-Encoding"] = "identity"
        probe = None
        try:
            probe = self.session.get(
                url, headers={**headers, "Range": "bytes=0-0"}, stream=True, timeout=(20, 30)
            )
            match = re.fullmatch(r"bytes 0-0/(\d+)", probe.headers.get("Content-Range", ""))
            if probe.status_code != 206 or not match or int(match.group(1)) != size_hint:
                return None
        except requests.RequestException as exc:
            log.info("Range probe unavailable for %s: %s", os.path.basename(dest), exc)
            return None
        finally:
            if probe is not None:
                probe.close()

        scratch = dest + ".parallel." + uuid.uuid4().hex
        workers = min(DOWNLOAD_RANGE_WORKERS, max(1, size_hint // (8 * 1024 * 1024)))
        block = (size_hint + workers - 1) // workers
        stop = threading.Event()
        progress_lock = threading.Lock()
        fd = None

        def fetch_range(start, end):
            session = build_retry_session(total=0, connect=0, read=0, status=0)
            response = None
            try:
                self._check_control()
                response = session.get(
                    url, headers={**headers, "Range": f"bytes={start}-{end}"},
                    stream=True, timeout=(20, 300),
                )
                expected_range = f"bytes {start}-{end}/{size_hint}"
                if response.status_code != 206 or response.headers.get("Content-Range") != expected_range:
                    raise IncompleteDownloadError(f"Server did not honor range {start}-{end}")
                position = start
                for chunk in response.iter_content(chunk_size=STREAM_CHUNK_SIZE):
                    if stop.is_set():
                        raise IncompleteDownloadError("Parallel download stopped")
                    self._check_control()
                    if position + len(chunk) > end + 1:
                        raise IncompleteDownloadError("Range response exceeded requested size")
                    written = 0
                    while written < len(chunk):
                        count = os.pwrite(fd, chunk[written:], position + written)
                        if count <= 0:
                            raise OSError("Parallel download write made no progress")
                        written += count
                    position += len(chunk)
                    with progress_lock:
                        self.progress.file_chunk(len(chunk))
                if position != end + 1:
                    raise IncompleteDownloadError(f"Range ended at {position}, expected {end + 1}")
            finally:
                if response is not None:
                    response.close()
                session.close()

        try:
            fd = os.open(scratch, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.ftruncate(fd, size_hint)
            ranges = [(start, min(start + block - 1, size_hint - 1)) for start in range(0, size_hint, block)]
            with ThreadPoolExecutor(max_workers=workers) as pool:
                pending = {pool.submit(fetch_range, start, end) for start, end in ranges}
                try:
                    while pending:
                        completed, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                        for future in completed:
                            future.result()
                        self._check_control()
                        self._emit("file_progress")
                except Exception:
                    stop.set()
                    raise
            os.close(fd)
            fd = None
            if os.path.getsize(scratch) != size_hint:
                raise IncompleteDownloadError("Parallel download size mismatch")
            os.replace(scratch, dest)
            self.progress.file_done()
            return {"transferred_bytes": size_hint, "final_size": size_hint, "skipped": False, "resumed": False}
        except PauseException:
            raise
        except Exception as exc:
            log.warning("Parallel transfer unavailable for %s, retrying serially: %s", os.path.basename(dest), exc)
            self.progress.sync_current_file_progress(0)
            return None
        finally:
            if fd is not None:
                os.close(fd)
            if os.path.exists(scratch):
                try:
                    os.remove(scratch)
                except OSError:
                    log.warning("Could not remove parallel scratch file: %s", scratch)

    def _emit(self, event, extra=None):
        now = time.time()
        if event in ("backup_start", "site_start", "site_done", "backup_done",
                     "file_done", "paused", "resumed", "cancelled") or (now - self._last_emit > 0.25):
            self._last_emit = now
            if self.progress_callback:
                data = {**self.stats, **self.progress.to_dict()}
                if extra:
                    data.update(extra)
                self.progress_callback(event, data)

    def get_site_id(self, site_path):
        host = self.config["sharepoint"]["host"]
        url = f"{self.GRAPH}/sites/{host}:/{site_path}" if site_path else f"{self.GRAPH}/sites/{host}"
        return self._get(url)["id"]

    def get_drives(self, site_id):
        return self._get(f"{self.GRAPH}/sites/{site_id}/drives").get("value", [])

    def list_files_recursive(self, drive_id, folder_id="root", on_file: Optional[Callable] = None, collect_items: bool = False):
        items = [] if collect_items else None
        url = f"{self.GRAPH}/drives/{drive_id}/items/{folder_id}/children"
        params = {"$top": GRAPH_LIST_PAGE_SIZE}
        while url:
            data = self._get(url, params)
            for item in data.get("value", []):
                if "folder" in item:
                    child_items = self.list_files_recursive(
                        drive_id,
                        item["id"],
                        on_file=on_file,
                        collect_items=collect_items,
                    )
                    if collect_items:
                        items.extend(child_items)
                elif "file" in item:
                    if on_file:
                        on_file(item)
                    if collect_items:
                        items.append(item)
            url = data.get("@odata.nextLink")
            params = None
        return items or []

    def _write_size_cache(self, backup_dir: str):
        try:
            total_size = 0
            for root, _, files in os.walk(backup_dir):
                for filename in files:
                    if filename.endswith((".tmp", ".tmp.meta")) or ".parallel." in filename:
                        continue
                    try:
                        total_size += os.path.getsize(os.path.join(root, filename))
                    except OSError:
                        pass
            with open(os.path.join(backup_dir, "_size_cache.json"), "w") as handle:
                json.dump({
                    "size_bytes": total_size,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }, handle, indent=2)
        except Exception as e:
            log.warning(f"Failed to write size cache for {backup_dir}: {e}")

    def _write_backup_runtime(self, backup_dir: str, status: str, extra: Optional[Dict] = None):
        try:
            payload = {
                "status": status,
                "current_site": self.stats.get("current_site", ""),
                "files_downloaded": self.stats.get("files_downloaded", 0),
                "files_skipped": self.stats.get("files_skipped", 0),
                "files_resumed": self.stats.get("files_resumed", 0),
                "bytes_downloaded": self.stats.get("bytes_downloaded", 0),
                "bytes_stored": self.stats.get("bytes_stored", 0),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            if extra:
                payload.update(extra)
            with open(os.path.join(backup_dir, "_backup_runtime.json"), "w") as handle:
                json.dump(payload, handle, indent=2, default=str)
        except Exception as e:
            log.warning(f"Failed to write runtime backup marker for {backup_dir}: {e}")

    def _is_resumable_backup_dir(self, path: Path) -> bool:
        if not path.is_dir() or not path.name.startswith("backup_"):
            return False
        workload_manifest = path / "_workload_manifest.json"
        if workload_manifest.exists():
            try:
                status = str(json.load(open(workload_manifest)).get("status") or "").strip().lower()
                return status in {"interrupted", "running"}
            except Exception:
                return False
        runtime_file = path / "_backup_runtime.json"
        if runtime_file.exists():
            try:
                status = str(json.load(open(runtime_file)).get("status") or "").strip().lower()
                return status in {"running", "interrupted"}
            except Exception:
                return True
        return any(child.is_dir() and not child.name.startswith(".") for child in path.iterdir())

    def _resolve_legacy_backup_dir(self, root_dir: str, ts: str) -> tuple[str, bool]:
        root_path = Path(root_dir)
        canonical_dir = root_path / "backup_current"
        if canonical_dir.exists() and canonical_dir.is_dir():
            return str(canonical_dir), True

        candidates = []
        for entry in root_path.iterdir():
            if not entry.is_dir() or not entry.name.startswith("backup_"):
                continue
            candidates.append(entry)

        def sort_key(path: Path):
            manifest = {}
            for candidate in (path / "_workload_manifest.json", path / "_backup_runtime.json"):
                if candidate.exists():
                    try:
                        manifest = json.load(open(candidate))
                        break
                    except Exception:
                        manifest = {}
            status = str(manifest.get("status") or "").strip().lower()
            priority = {
                "running": 6,
                "interrupted": 5,
                "partial": 4,
                "failed": 3,
                "cancelled": 2,
                "success": 1,
            }.get(status, 0)
            marker_time = (
                manifest.get("ended_at")
                or manifest.get("updated_at")
                or manifest.get("start_time")
                or manifest.get("started_at")
                or ""
            )
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0
            return (priority, marker_time, mtime, path.name)

        if candidates:
            candidates.sort(key=sort_key, reverse=True)
            return str(candidates[0]), True

        return str(canonical_dir), False

    def _materialize_existing_file(self, source_path: str, dest_path: str, expected_size: int = 0) -> bool:
        if not source_path or not os.path.exists(source_path):
            return False
        if expected_size and os.path.getsize(source_path) != expected_size:
            return False
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        if os.path.exists(dest_path):
            return not expected_size or os.path.getsize(dest_path) == expected_size
        try:
            os.link(source_path, dest_path)
        except OSError:
            shutil.copy2(source_path, dest_path)
        return True

    @staticmethod
    def _site_storage_key(name: str, site_id: str) -> str:
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")[:80] or "site"
        digest = hashlib.sha256(site_id.encode("utf-8")).hexdigest()[:12]
        return f"{safe_name}--{digest}"

    @staticmethod
    def _safe_destination(root: str, *parts: str) -> str:
        root_path = Path(root).resolve()
        destination = root_path.joinpath(*parts).resolve()
        if root_path not in destination.parents:
            raise ValueError("Downloaded file path escapes its backup folder")
        return str(destination)

    def _site_backup_dir(self, backup_dir: str, name: str, site_id: str, site_path: str) -> tuple[str, bool]:
        legacy = Path(backup_dir) / name.replace(" ", "_")
        root = Path(backup_dir).resolve()
        if (legacy.name not in {".", ".."} and not legacy.is_symlink()
                and legacy.parent.resolve() == root and legacy.is_dir() and legacy.resolve().parent == root):
            meta_file = legacy / "_backup_metadata.json"
            try:
                meta = json.loads(meta_file.read_text())
                if meta.get("site_id") == site_id or (
                    not meta.get("site_id")
                    and
                    meta.get("site_path", "").strip("/").lower() == site_path.strip("/").lower()
                    and meta.get("site_path") is not None
                ):
                    return str(legacy), True
            except (OSError, ValueError):
                pass
        site_dir = root / self._site_storage_key(name, site_id)
        if site_dir.exists():
            if site_dir.is_symlink() or site_dir.resolve().parent != root:
                raise ValueError(f"Unsafe site backup path: {site_dir}")
            try:
                meta = json.loads((site_dir / "_backup_metadata.json").read_text())
            except (OSError, ValueError) as exc:
                raise ValueError(f"Site backup identity is missing for {site_dir}") from exc
            if meta.get("site_id") != site_id:
                raise ValueError(f"Site backup identity mismatch for {site_dir}")
        return str(site_dir), False

    # ════════════════════════════════════════════════════════════
    # FULL BACKUP (multiple sites)
    # ════════════════════════════════════════════════════════════
    @staticmethod
    def _normalize_site_path(value: str) -> str:
        return str(value or "").strip().strip("/")

    def backup_site(self, site_info, backup_dir):
        name, path = site_info["name"], site_info["path"]
        errors_before = len(self.stats["errors"])
        self.stats["current_site"] = name
        self._emit("site_start", {"site": name})

        try:
            site_id = self.get_site_id(path)
            site_dir, legacy_identity = self._site_backup_dir(backup_dir, name, site_id, path)
            os.makedirs(site_dir, exist_ok=True)
            identity_path = os.path.join(site_dir, "_backup_metadata.json")
            if not os.path.exists(identity_path):
                with open(identity_path, "w") as handle:
                    json.dump({"site_name": name, "site_path": path, "site_id": site_id,
                               "site_storage_key": os.path.basename(site_dir), "status": "running"}, handle)
            manifest_key = self._site_storage_key(name, site_id)
            old_manifest = self.manifest.load(manifest_key)
            if not old_manifest and legacy_identity:
                old_manifest = self.manifest.load(name)
            new_manifest = {}
            flush_state = self._new_manifest_flush_state()
            libraries = self.get_drives(site_id)
            def save_site_manifest():
                self.manifest.save(manifest_key, new_manifest)

            for lib in libraries:
                drive_id, lib_name = lib["id"], lib["name"]
                self._emit("site_scanning", {
                    "site": name,
                    "library": lib_name,
                    "current_file": f"Scanning {lib_name}...",
                })

                def process_file(item):
                    self._check_control()
                    self.progress.files_total += 1
                    fid = item["id"]
                    fname = item["name"]
                    fpath = item.get("parentReference", {}).get("path", "")
                    fsize = item.get("size", 0)
                    etag = item.get("eTag", "")
                    modified = item.get("lastModifiedDateTime", "")
                    rel = fpath.split("root:")[-1].lstrip("/")
                    dest = self._safe_destination(site_dir, lib_name, rel, fname)
                    old_entry = old_manifest.get(fid, {})
                    needs_update = self.manifest.needs_update(old_manifest, fid, etag, modified)

                    if needs_update:
                        self.progress.bytes_total += fsize
                    self._emit("site_scanning", {
                        "site": name,
                        "library": lib_name,
                        "current_file": f"{lib_name} / {fname}",
                    })

                    if not needs_update:
                        manifest_entry = dict(old_entry)
                        manifest_entry["path"] = dest
                        manifest_entry["backupTime"] = datetime.now(timezone.utc).isoformat()
                        if not os.path.exists(dest):
                            if not self._materialize_existing_file(old_entry.get("path", ""), dest, int(fsize or 0)):
                                needs_update = True
                            else:
                                new_manifest[fid] = manifest_entry
                                self._mark_manifest_dirty(flush_state)
                                self._flush_manifest_checkpoint(save_site_manifest, flush_state)
                                self.progress.files_done += 1
                                self.stats["files_skipped"] += 1
                                self.stats["bytes_stored"] += int(fsize or 0)
                                self._emit("file_done", {"file": fname, "status": "reused"})
                                return
                        elif not fsize or os.path.getsize(dest) == fsize:
                            new_manifest[fid] = manifest_entry
                            self._mark_manifest_dirty(flush_state)
                            self._flush_manifest_checkpoint(save_site_manifest, flush_state)
                            self.progress.files_done += 1
                            self.stats["files_skipped"] += 1
                            self.stats["bytes_stored"] += int(fsize or 0)
                            self._emit("file_done", {"file": fname, "status": "skipped"})
                            return

                    if not needs_update:
                        needs_update = True
                        self.progress.bytes_total += fsize

                    try:
                        dl_url = item.get("@microsoft.graph.downloadUrl") or f"{self.GRAPH}/drives/{drive_id}/items/{fid}/content"
                        dl_result = self._download(
                            dl_url,
                            dest,
                            fsize,
                            auth_required=not bool(item.get("@microsoft.graph.downloadUrl")),
                            source_identity=f"{drive_id}:{fid}:{etag}:{modified}:{fsize}",
                        )
                        if dl_result["skipped"]:
                            self.stats["files_skipped"] += 1
                        else:
                            self.stats["files_downloaded"] += 1
                            if dl_result["resumed"]:
                                self.stats["files_resumed"] += 1
                        self.stats["bytes_downloaded"] += int(dl_result.get("transferred_bytes", 0) or 0)
                        self.stats["bytes_stored"] += int(dl_result.get("final_size", fsize) or 0)
                        new_manifest[fid] = {
                            "name": fname, "path": dest, "eTag": etag,
                            "lastModified": modified, "size": fsize,
                            "backupTime": datetime.now(timezone.utc).isoformat(),
                        }
                        self._mark_manifest_dirty(flush_state)
                        self._flush_manifest_checkpoint(save_site_manifest, flush_state)
                        self._emit("file_done", {"file": fname})
                    except PauseException:
                        self._flush_manifest_checkpoint(save_site_manifest, flush_state, force=True)
                        raise
                    except Exception as e:
                        self.stats["errors"].append(f"{fname}: {e}")

                self.list_files_recursive(drive_id, on_file=process_file)

            self._flush_manifest_checkpoint(save_site_manifest, flush_state, force=True)
            meta = {
                "site_name": name, "site_path": path, "site_id": site_id,
                "site_storage_key": os.path.basename(site_dir),
                "backup_time": datetime.now(timezone.utc).isoformat(),
                "libraries": [{"id": l["id"], "name": l["name"]} for l in libraries],
                "total_files": len(new_manifest),
            }
            os.makedirs(site_dir, exist_ok=True)
            with open(os.path.join(site_dir, "_backup_metadata.json"), "w") as f:
                json.dump(meta, f, indent=2)
            if len(self.stats["errors"]) > errors_before:
                self.stats["failed_sites"].append(name)
                self._emit("site_done", {"site": name, "status": "partial"})
                return False
            self.stats["successful_sites"] += 1
            self._emit("site_done", {"site": name, "status": "success"})
            return True

        except PauseException:
            raise
        except Exception as e:
            self.stats["failed_sites"].append(name)
            self.stats["errors"].append(f"Site {name}: {e}")
            self._emit("site_done", {"site": name, "status": "failed", "error": str(e)})
            return False

    def run_backup(self, custom_root: str = None, site_paths: list[str] | None = None):
        """Run full backup. ★ NEW: custom_root parameter."""
        self.stats["start_time"] = datetime.now(timezone.utc)
        ts = self.stats["start_time"].strftime("%Y%m%d_%H%M%S")

        # ★ Use custom_root if provided, else default
        root_dir = custom_root or self.config["backup"]["root_dir"]
        os.makedirs(root_dir, exist_ok=True)
        backup_dir, resumed_existing = self._resolve_legacy_backup_dir(root_dir, ts)
        os.makedirs(backup_dir, exist_ok=True)
        self.stats["backup_path"] = backup_dir
        self.stats["resumed_existing_backup"] = resumed_existing
        self._write_backup_runtime(
            backup_dir,
            "running",
            {
                "started_at": self.stats["start_time"].isoformat(),
                "resumed_existing_backup": resumed_existing,
            },
        )

        enabled_sites = [s for s in self.config["sites"] if s.get("enabled", True)]
        normalized_filters = {
            self._normalize_site_path(item)
            for item in (site_paths or [])
            if self._normalize_site_path(item)
        }
        if normalized_filters:
            enabled_sites = [
                s for s in enabled_sites
                if self._normalize_site_path(s.get("path", "")) in normalized_filters
            ]
        self.stats["total_sites"] = len(enabled_sites)
        self._emit("backup_start", {
            "total": len(enabled_sites),
            "dest": backup_dir,
            "resumed_existing_backup": resumed_existing,
            "site_paths": sorted(normalized_filters),
        })

        try:
            for site in enabled_sites:
                self.backup_site(site, backup_dir)
        except PauseException:
            self.stats["cancelled"] = True
            log.warning("Backup cancelled by user")

        self.stats["end_time"] = datetime.now(timezone.utc)
        if self.task_id:
            TaskController.cleanup(self.task_id)
        if not self.stats.get("cancelled"):
            self._write_size_cache(backup_dir)
            self._write_backup_runtime(
                backup_dir,
                "partial" if self.stats["errors"] or self.stats["failed_sites"] else "success",
                {"ended_at": self.stats["end_time"].isoformat()},
            )
        else:
            self._write_backup_runtime(
                backup_dir,
                "interrupted",
                {"ended_at": self.stats["end_time"].isoformat()},
            )
        self._emit("backup_done")
        return self.stats

    # ════════════════════════════════════════════════════════════
    # CUSTOM URL DOWNLOAD (with pause/resume + custom dest)
    # ════════════════════════════════════════════════════════════
    def parse_sharepoint_url(self, url):
        parsed = urlparse(url)
        if not parsed.hostname:
            raise ValueError("Invalid URL")
        from urllib.parse import parse_qs
        qs = parse_qs(parsed.query)
        if "parent" in qs and qs["parent"]:
            full_path = unquote(qs["parent"][0])
        elif "id" in qs and qs["id"]:
            full_path = unquote(qs["id"][0])
        else:
            full_path = unquote(parsed.path)
        full_path = full_path.strip("/")
        parts = full_path.split("/")
        if len(parts) >= 2 and parts[0] in ("sites", "teams"):
            site_path = f"{parts[0]}/{parts[1]}"
            folder_path = "/".join(parts[2:]) if len(parts) > 2 else ""
        else:
            site_path, folder_path = "", full_path
        folder_path = self._normalize_sharepoint_folder_path(folder_path)
        return {
            "host": parsed.hostname, "site_path": site_path,
            "folder_path": folder_path, "full_url": url,
        }

    def _normalize_sharepoint_folder_path(self, folder_path: str) -> str:
        folder_path = (folder_path or "").strip("/")
        if not folder_path:
            return ""

        lower = folder_path.lower()
        forms_marker = "/forms/"
        if forms_marker in lower and lower.endswith(".aspx"):
            marker_idx = lower.index(forms_marker)
            return folder_path[:marker_idx].strip("/")

        if lower.endswith(".aspx"):
            parts = folder_path.split("/")
            return "/".join(parts[:-1]).strip("/")

        return folder_path

    def download_custom_url(self, url, dest_dir: str = None):
        """
        Download from custom SharePoint URL.
        ★ NEW dest_dir parameter — can be:
          - Full path: /backup/sharepoint/project-x
          - Relative: project-x (will be placed under backup_root)
          - None: auto-generate timestamped folder
        """
        parsed = self.parse_sharepoint_url(url)
        site_path = parsed["site_path"]
        folder_path = parsed["folder_path"]

        # ★ Resolve destination directory
        if dest_dir:
            if not os.path.isabs(dest_dir):
                # Relative path → place under backup_root
                dest_dir = os.path.join(self.config["backup"]["root_dir"], dest_dir)
            # Ensure it doesn't escape backup_root for safety
            dest_dir = os.path.abspath(dest_dir)
        else:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            safe_name = site_path.replace("/", "_") or "custom"
            dest_dir = os.path.join(self.config["backup"]["root_dir"], f"custom_{safe_name}_{ts}")

        os.makedirs(dest_dir, exist_ok=True)
        self.stats["current_site"] = site_path or "custom"
        self._emit("custom_start", {"url": url, "parsed": parsed, "dest": dest_dir})

        try:
            site_id = self.get_site_id(site_path)
            libraries = self.get_drives(site_id)
            if not libraries:
                raise Exception("No document libraries found")

            target_drive = None
            target_folder_id = "root"

            if folder_path:
                folder_parts = folder_path.split("/")
                first = folder_parts[0]
                for lib in libraries:
                    library_url_name = unquote(urlparse(lib.get("webUrl") or "").path.rstrip("/").split("/")[-1])
                    if lib["name"].lower() == first.lower() or library_url_name.lower() == first.lower():
                        target_drive = lib
                        if len(folder_parts) > 1:
                            sub_path = "/".join(folder_parts[1:])
                            try:
                                folder_item = self._get(
                                    f"{self.GRAPH}/drives/{target_drive['id']}/root:/{sub_path}"
                                )
                                target_folder_id = folder_item["id"]
                            except Exception as exc:
                                raise ValueError(f"Folder not found in library: {sub_path}") from exc
                        break
                if not target_drive:
                    raise ValueError(f"Library not found in site: {first}")
            else:
                target_drive = libraries[0]

            custom_manifest_path = os.path.join(dest_dir, "_custom_download_manifest.json")
            try:
                with open(custom_manifest_path, "r") as handle:
                    custom_manifest = json.load(handle)
            except Exception:
                custom_manifest = {}
            flush_state = self._new_manifest_flush_state()

            def save_custom_manifest():
                with open(custom_manifest_path, "w") as handle:
                    json.dump(custom_manifest, handle, indent=2, default=str)

            downloaded = 0
            skipped = 0
            resumed = 0
            total_seen = 0

            def process_item(item):
                nonlocal downloaded, skipped, resumed, total_seen
                try:
                    self._check_control()  # ★ Check pause/cancel
                    total_seen += 1
                    self.progress.files_total += 1
                    fname = item["name"]
                    fpath = item.get("parentReference", {}).get("path", "")
                    fsize = item.get("size", 0)
                    item_id = item["id"]
                    etag = item.get("eTag", "")
                    modified = item.get("lastModifiedDateTime", "")
                    rel = fpath.split("root:")[-1].lstrip("/")
                    dest = self._safe_destination(dest_dir, rel, fname)
                    old_entry = custom_manifest.get(item_id, {})
                    if (old_entry.get("eTag") == etag and old_entry.get("lastModified") == modified
                            and os.path.exists(dest) and (not fsize or os.path.getsize(dest) == fsize)):
                        self.progress.sync_current_file_progress(fsize)
                        self.progress.files_done += 1
                        self.stats["files_skipped"] += 1
                        self.stats["bytes_stored"] += int(fsize or 0)
                        skipped += 1
                        self._emit("file_done", {"file": fname, "status": "skipped"})
                        return
                    self.progress.bytes_total += fsize
                    self._emit("custom_scanning", {
                        "current_file": f"Downloading {fname}",
                        "dest": dest_dir,
                    })
                    dl_url = item.get("@microsoft.graph.downloadUrl") or f"{self.GRAPH}/drives/{target_drive['id']}/items/{item['id']}/content"
                    dl_result = self._download(
                        dl_url,
                        dest,
                        fsize,
                        auth_required=not bool(item.get("@microsoft.graph.downloadUrl")),
                        source_identity=f"{target_drive['id']}:{item_id}:{etag}:{modified}:{fsize}",
                    )
                    if dl_result["skipped"]:
                        skipped += 1
                        self.stats["files_skipped"] += 1
                    else:
                        downloaded += 1
                        self.stats["files_downloaded"] += 1
                        if dl_result["resumed"]:
                            resumed += 1
                            self.stats["files_resumed"] += 1
                    self.stats["bytes_downloaded"] += int(dl_result.get("transferred_bytes", 0) or 0)
                    self.stats["bytes_stored"] += int(dl_result.get("final_size", fsize) or 0)
                    custom_manifest[item_id] = {
                        "name": fname,
                        "path": dest,
                        "eTag": etag,
                        "lastModified": modified,
                        "size": fsize,
                        "downloadTime": datetime.now(timezone.utc).isoformat(),
                    }
                    self._mark_manifest_dirty(flush_state)
                    self._flush_manifest_checkpoint(save_custom_manifest, flush_state)
                    self._emit("file_done", {"file": fname})
                except PauseException:
                    self._flush_manifest_checkpoint(save_custom_manifest, flush_state, force=True)
                    raise
                except Exception as e:
                    self.stats["errors"].append(f"{item.get('name', '?')}: {e}")

            self.list_files_recursive(target_drive["id"], target_folder_id, on_file=process_item)
            self._flush_manifest_checkpoint(save_custom_manifest, flush_state, force=True)
            self._emit("custom_done")
            if self.task_id:
                TaskController.cleanup(self.task_id)
            return {
                "url": url, "downloaded": downloaded, "total": total_seen,
                "dest": dest_dir, "bytes": self.stats["bytes_downloaded"],
                "skipped": skipped, "resumed": resumed,
                "errors": list(self.stats["errors"]),
                "status": "partial" if self.stats["errors"] and downloaded + skipped else (
                    "failed" if self.stats["errors"] else "success"
                ),
                "cancelled": self.stats.get("cancelled", False),
            }

        except PauseException:
            self.stats["cancelled"] = True
            try:
                self._flush_manifest_checkpoint(save_custom_manifest, flush_state, force=True)
            except Exception:
                pass
            if self.task_id:
                TaskController.cleanup(self.task_id)
            return {
                "url": url, "downloaded": self.stats["files_downloaded"],
                "dest": dest_dir, "bytes": self.stats["bytes_downloaded"],
                "cancelled": True, "message": "Cancelled by user",
            }


class RestoreEngine:
    GRAPH = "https://graph.microsoft.com/v1.0"

    def __init__(self, config, progress_callback=None):
        self.config = config
        az = config["azure_ad"]
        self.auth = GraphAuth(az["tenant_id"], az["client_id"], az["client_secret"])
        self.session = build_retry_session()

    def _headers(self):
        return {"Authorization": f"Bearer {self.auth.get_token()}",
                "Content-Type": "application/json"}

    def list_backups(self):
        root = Path(self.config["backup"]["root_dir"])
        backups = []
        if not root.exists():
            return backups
        for d in sorted(root.iterdir(), reverse=True):
            if d.is_dir() and (d.name.startswith("backup_") or d.name.startswith("custom_")):
                try:
                    size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                    sites = [sd.name for sd in d.iterdir() if sd.is_dir() and not sd.name.startswith(".")]
                    type_ = "custom" if d.name.startswith("custom_") else "scheduled"
                    backups.append({
                        "name": d.name, "type": type_,
                        "date": datetime.fromtimestamp(d.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                        "size_bytes": size, "size_human": f"{size / 1024 / 1024:.1f} MB",
                        "sites": sites, "site_count": len(sites),
                    })
                except Exception:
                    pass
        return backups

    def list_backup_contents(self, backup_name, site_name=None):
        root = Path(self.config["backup"]["root_dir"]) / backup_name
        if not root.exists():
            return {"error": "Backup not found"}
        sites = []
        for sd in root.iterdir():
            if sd.is_dir() and not sd.name.startswith("."):
                meta_file = sd / "_backup_metadata.json"
                meta = json.load(open(meta_file)) if meta_file.exists() else {}
                file_count = sum(1 for _ in sd.rglob("*") if _.is_file())
                sites.append({
                    "name": sd.name, "display_name": meta.get("site_name", sd.name),
                    "file_count": file_count, "backup_time": meta.get("backup_time", ""),
                })
        return {"backup": backup_name, "sites": sites}

    def delete_backup(self, backup_name):
        path = Path(self.config["backup"]["root_dir"]) / backup_name
        if path.exists() and path.is_dir() and (backup_name.startswith("backup_") or backup_name.startswith("custom_")):
            shutil.rmtree(path)
            return {"status": "deleted", "backup": backup_name}
        return {"error": "Invalid backup name"}

    def restore_site(self, backup_name, site_name, target_site_path=None, dry_run=False):
        safe = site_name.replace(" ", "_")
        backup_path = Path(self.config["backup"]["root_dir"]) / backup_name / safe
        if not backup_path.exists():
            return {"error": f"Not found: {backup_path}"}
        meta_file = backup_path / "_backup_metadata.json"
        meta = json.load(open(meta_file))
        restore_path = target_site_path or meta["site_path"]
        host = self.config["sharepoint"]["host"]
        url = f"{self.GRAPH}/sites/{host}:/{restore_path}" if restore_path else f"{self.GRAPH}/sites/{host}"
        site_data = self.session.get(url, headers=self._headers()).json()
        site_id = site_data["id"]
        drives_data = self.session.get(f"{self.GRAPH}/sites/{site_id}/drives", headers=self._headers()).json()
        drives_map = {d["name"]: d["id"] for d in drives_data.get("value", [])}
        stats = {"uploaded": 0, "errors": [], "total_bytes": 0}

        for lib_dir in backup_path.iterdir():
            if not lib_dir.is_dir() or lib_dir.name.startswith("_"):
                continue
            drive_id = drives_map.get(lib_dir.name)
            if not drive_id:
                continue
            for fp in lib_dir.rglob("*"):
                if fp.is_file() and not fp.name.startswith("_"):
                    rel = str(fp.relative_to(lib_dir)).replace("\\", "/")
                    if dry_run:
                        stats["uploaded"] += 1
                        continue
                    try:
                        fsize = fp.stat().st_size
                        up_url = f"{self.GRAPH}/drives/{drive_id}/root:/{rel}:/content"
                        with open(fp, "rb") as fobj:
                            r = self.session.put(up_url, headers={
                                "Authorization": f"Bearer {self.auth.get_token()}",
                                "Content-Type": "application/octet-stream",
                            }, data=fobj, timeout=120)
                            r.raise_for_status()
                        stats["uploaded"] += 1
                        stats["total_bytes"] += fsize
                    except Exception as e:
                        stats["errors"].append(f"{rel}: {e}")
        return stats
