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
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, unquote
from typing import Optional, Dict, List, Callable

from app.task_control import check_control, PauseException, TaskController

log = logging.getLogger("spo_backup")


class GraphAuth:
    AUTHORITY = "https://login.microsoftonline.com/{tenant_id}"
    SCOPE = ["https://graph.microsoft.com/.default"]

    def __init__(self, tenant_id, client_id, client_secret):
        self.app = msal.ConfidentialClientApplication(
            client_id,
            authority=self.AUTHORITY.format(tenant_id=tenant_id),
            client_credential=client_secret,
        )

    def get_token(self):
        result = self.app.acquire_token_for_client(scopes=self.SCOPE)
        if "access_token" in result:
            return result["access_token"]
        raise Exception(f"Auth failed: {result.get('error_description', 'Unknown')}")


class ProgressTracker:
    def __init__(self):
        self.start_time = time.time()
        self.pause_time = 0  # Track time spent paused (for accurate speed)
        self.bytes_total = 0
        self.bytes_done = 0
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

    def file_done(self):
        self.files_done += 1

    @property
    def overall_pct(self):
        if self.bytes_total:
            return min(100, int(self.bytes_done / self.bytes_total * 100))
        if self.files_total and self.files_done >= self.files_total:
            return 100
        return 0

    @property
    def file_pct(self):
        return min(100, int(self.current_file_done / self.current_file_size * 100)) if self.current_file_size else 0

    @property
    def speed_bps(self):
        active_time = time.time() - self.start_time - self.pause_time
        return self.bytes_done / active_time if active_time > 0.1 else 0

    @property
    def speed_human(self):
        s = self.speed_bps
        if s > 1024 * 1024:
            return f"{s / 1024 / 1024:.2f} MB/s"
        if s > 1024:
            return f"{s / 1024:.1f} KB/s"
        return f"{s:.0f} B/s"

    @property
    def eta_human(self):
        if self.files_total == 0:
            return "scanning..."
        if self.speed_bps == 0:
            return "waiting..."
        remaining = self.bytes_total - self.bytes_done
        if remaining <= 0:
            return "finishing..."
        sec = int(remaining / self.speed_bps)
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
            "files_done": self.files_done,
            "files_total": self.files_total,
            "speed_human": self.speed_human,
            "eta_human": self.eta_human,
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
        self.session = requests.Session()
        self.progress = ProgressTracker()
        self.stats = {
            "total_sites": 0, "successful_sites": 0, "failed_sites": [],
            "files_downloaded": 0, "files_skipped": 0, "bytes_downloaded": 0,
            "files_resumed": 0,
            "errors": [], "start_time": None, "end_time": None,
            "current_site": "", "cancelled": False,
        }
        self._last_emit = 0

    def _headers(self):
        return {"Authorization": f"Bearer {self.auth.get_token()}",
                "Content-Type": "application/json"}

    def _get(self, url, params=None):
        for attempt in range(3):
            try:
                r = self.session.get(url, headers=self._headers(), params=params, timeout=60)
                if r.status_code == 429:
                    time.sleep(int(r.headers.get("Retry-After", 30)))
                    continue
                r.raise_for_status()
                return r.json()
            except Exception:
                if attempt == 2:
                    raise
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

    def _download(self, url, dest, size_hint=0):
        """Download with progress + pause/resume support."""
        # ── NEW: Resume support — skip if file already exists with same size
        if os.path.exists(dest) and size_hint > 0:
            existing_size = os.path.getsize(dest)
            if existing_size == size_hint:
                log.info(f"Skip existing: {os.path.basename(dest)}")
                self.progress.bytes_done += size_hint
                self.progress.files_done += 1
                return {"bytes_written": size_hint, "skipped": True, "resumed": False}

        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".tmp"
        resume_from = 0
        headers = self._headers()
        if os.path.exists(tmp):
            try:
                resume_from = os.path.getsize(tmp)
            except OSError:
                resume_from = 0
            if size_hint and resume_from >= size_hint:
                os.replace(tmp, dest)
                self.progress.bytes_done += size_hint
                self.progress.files_done += 1
                return {"bytes_written": size_hint, "skipped": True, "resumed": False}
            if resume_from > 0:
                headers["Range"] = f"bytes={resume_from}-"

        r = self.session.get(url, headers=headers, stream=True, timeout=300)
        r.raise_for_status()

        total_size = int(r.headers.get("content-length", size_hint or 0))
        if resume_from and r.status_code == 206 and size_hint:
            total_size = size_hint
        elif resume_from and r.status_code == 200:
            # Range not honored by upstream; restart this file cleanly.
            resume_from = 0
            try:
                os.remove(tmp)
            except OSError:
                pass

        self.progress.file_start(os.path.basename(dest), total_size)
        if resume_from:
            self.progress.file_chunk(resume_from)

        bytes_written = resume_from
        # Download to .tmp file first, rename when done (atomic)
        mode = "ab" if resume_from else "wb"
        with open(tmp, mode) as f:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    # ★ Check pause/cancel every chunk
                    self._check_control()

                    f.write(chunk)
                    sz = len(chunk)
                    bytes_written += sz
                    self.progress.file_chunk(sz)
                    self._emit("file_progress")

        # Atomic rename
        os.replace(tmp, dest)
        self.progress.file_done()
        return {"bytes_written": bytes_written, "skipped": False, "resumed": resume_from > 0}

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

    def list_files_recursive(self, drive_id, folder_id="root", on_file: Optional[Callable] = None):
        items = []
        url = f"{self.GRAPH}/drives/{drive_id}/items/{folder_id}/children"
        params = {"$top": 200}
        while url:
            data = self._get(url, params)
            for item in data.get("value", []):
                if "folder" in item:
                    items.extend(self.list_files_recursive(drive_id, item["id"], on_file=on_file))
                elif "file" in item:
                    if on_file:
                        on_file(item)
                    items.append(item)
            url = data.get("@odata.nextLink")
            params = None
        return items

    def _write_size_cache(self, backup_dir: str):
        try:
            total_size = 0
            for root, _, files in os.walk(backup_dir):
                for filename in files:
                    if filename.endswith(".tmp"):
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
        candidates = []
        for entry in root_path.iterdir():
            if self._is_resumable_backup_dir(entry):
                candidates.append(entry)
        if candidates:
            candidates.sort(key=lambda item: item.stat().st_mtime, reverse=True)
            return str(candidates[0]), True
        return os.path.join(root_dir, f"backup_{ts}"), False

    def _materialize_existing_file(self, source_path: str, dest_path: str) -> bool:
        if not source_path or not os.path.exists(source_path):
            return False
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        if os.path.exists(dest_path):
            return True
        try:
            os.link(source_path, dest_path)
        except OSError:
            shutil.copy2(source_path, dest_path)
        return True

    # ════════════════════════════════════════════════════════════
    # FULL BACKUP (multiple sites)
    # ════════════════════════════════════════════════════════════
    def backup_site(self, site_info, backup_dir):
        name, path = site_info["name"], site_info["path"]
        self.stats["current_site"] = name
        self._emit("site_start", {"site": name})

        try:
            site_id = self.get_site_id(path)
            old_manifest = self.manifest.load(name)
            new_manifest = {}
            libraries = self.get_drives(site_id)
            site_dir = os.path.join(backup_dir, name.replace(" ", "_"))

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
                    dest = os.path.join(site_dir, lib_name, rel, fname)
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
                            if not self._materialize_existing_file(old_entry.get("path", ""), dest):
                                needs_update = True
                            else:
                                new_manifest[fid] = manifest_entry
                                self.progress.files_done += 1
                                self.stats["files_skipped"] += 1
                                self._emit("file_done", {"file": fname, "status": "reused"})
                                return
                        else:
                            new_manifest[fid] = manifest_entry
                            self.progress.files_done += 1
                            self.stats["files_skipped"] += 1
                            self._emit("file_done", {"file": fname, "status": "skipped"})
                            return

                    if not needs_update:
                        self.progress.files_done += 1
                        self.stats["files_skipped"] += 1
                        self._emit("file_done", {"file": fname, "status": "skipped"})
                        return

                    try:
                        dl_url = f"{self.GRAPH}/drives/{drive_id}/items/{fid}/content"
                        dl_result = self._download(dl_url, dest, fsize)
                        if dl_result["skipped"]:
                            self.stats["files_skipped"] += 1
                        else:
                            self.stats["files_downloaded"] += 1
                            if dl_result["resumed"]:
                                self.stats["files_resumed"] += 1
                        self.stats["bytes_downloaded"] += dl_result["bytes_written"]
                        new_manifest[fid] = {
                            "name": fname, "path": dest, "eTag": etag,
                            "lastModified": modified, "size": fsize,
                            "backupTime": datetime.now(timezone.utc).isoformat(),
                        }
                        self.manifest.save(name, new_manifest)
                        self._emit("file_done", {"file": fname})
                    except PauseException:
                        raise
                    except Exception as e:
                        self.stats["errors"].append(f"{fname}: {e}")

                self.list_files_recursive(drive_id, on_file=process_file)

            self.manifest.save(name, new_manifest)
            meta = {
                "site_name": name, "site_path": path, "site_id": site_id,
                "backup_time": datetime.now(timezone.utc).isoformat(),
                "libraries": [{"id": l["id"], "name": l["name"]} for l in libraries],
                "total_files": len(new_manifest),
            }
            os.makedirs(site_dir, exist_ok=True)
            with open(os.path.join(site_dir, "_backup_metadata.json"), "w") as f:
                json.dump(meta, f, indent=2)
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

    def run_backup(self, custom_root: str = None):
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
        self.stats["total_sites"] = len(enabled_sites)
        self._emit("backup_start", {"total": len(enabled_sites), "dest": backup_dir, "resumed_existing_backup": resumed_existing})

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
                "success",
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
                    if lib["name"].lower() == first.lower():
                        target_drive = lib
                        if len(folder_parts) > 1:
                            sub_path = "/".join(folder_parts[1:])
                            try:
                                folder_item = self._get(
                                    f"{self.GRAPH}/drives/{target_drive['id']}/root:/{sub_path}"
                                )
                                target_folder_id = folder_item["id"]
                            except Exception:
                                pass
                        break
                if not target_drive:
                    target_drive = libraries[0]
            else:
                target_drive = libraries[0]

            custom_manifest_path = os.path.join(dest_dir, "_custom_download_manifest.json")
            try:
                with open(custom_manifest_path, "r") as handle:
                    custom_manifest = json.load(handle)
            except Exception:
                custom_manifest = {}

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
                    dest = os.path.join(dest_dir, rel, fname)
                    old_entry = custom_manifest.get(item_id, {})
                    if old_entry.get("eTag") == etag and old_entry.get("lastModified") == modified and os.path.exists(dest):
                        self.progress.bytes_done += fsize
                        self.progress.files_done += 1
                        self.stats["files_skipped"] += 1
                        skipped += 1
                        self._emit("file_done", {"file": fname, "status": "skipped"})
                        return
                    self.progress.bytes_total += fsize
                    self._emit("custom_scanning", {
                        "current_file": f"Downloading {fname}",
                        "dest": dest_dir,
                    })
                    dl_url = f"{self.GRAPH}/drives/{target_drive['id']}/items/{item['id']}/content"
                    dl_result = self._download(dl_url, dest, fsize)
                    if dl_result["skipped"]:
                        skipped += 1
                        self.stats["files_skipped"] += 1
                    else:
                        downloaded += 1
                        self.stats["files_downloaded"] += 1
                        if dl_result["resumed"]:
                            resumed += 1
                            self.stats["files_resumed"] += 1
                    self.stats["bytes_downloaded"] += dl_result["bytes_written"]
                    custom_manifest[item_id] = {
                        "name": fname,
                        "path": dest,
                        "eTag": etag,
                        "lastModified": modified,
                        "size": fsize,
                        "downloadTime": datetime.now(timezone.utc).isoformat(),
                    }
                    with open(custom_manifest_path, "w") as handle:
                        json.dump(custom_manifest, handle, indent=2, default=str)
                    self._emit("file_done", {"file": fname})
                except PauseException:
                    raise
                except Exception as e:
                    self.stats["errors"].append(f"{item.get('name', '?')}: {e}")

            self.list_files_recursive(target_drive["id"], target_folder_id, on_file=process_item)
            self._emit("custom_done")
            if self.task_id:
                TaskController.cleanup(self.task_id)
            return {
                "url": url, "downloaded": downloaded, "total": total_seen,
                "dest": dest_dir, "bytes": self.stats["bytes_downloaded"],
                "skipped": skipped, "resumed": resumed,
                "cancelled": self.stats.get("cancelled", False),
            }

        except PauseException:
            self.stats["cancelled"] = True
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
        self.session = requests.Session()

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
