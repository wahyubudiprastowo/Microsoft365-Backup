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
        return min(100, int(self.bytes_done / self.bytes_total * 100)) if self.bytes_total else 0

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
        if self.speed_bps == 0:
            return "calculating..."
        remaining = self.bytes_total - self.bytes_done
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
                return size_hint

        os.makedirs(os.path.dirname(dest), exist_ok=True)
        r = self.session.get(url, headers=self._headers(), stream=True, timeout=300)
        r.raise_for_status()

        total_size = int(r.headers.get("content-length", size_hint or 0))
        self.progress.file_start(os.path.basename(dest), total_size)

        bytes_written = 0
        # Download to .tmp file first, rename when done (atomic)
        tmp = dest + ".tmp"
        with open(tmp, "wb") as f:
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
        return bytes_written

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

    def list_files_recursive(self, drive_id, folder_id="root"):
        items = []
        url = f"{self.GRAPH}/drives/{drive_id}/items/{folder_id}/children"
        params = {"$top": 200}
        while url:
            data = self._get(url, params)
            for item in data.get("value", []):
                if "folder" in item:
                    items.extend(self.list_files_recursive(drive_id, item["id"]))
                elif "file" in item:
                    items.append(item)
            url = data.get("@odata.nextLink")
            params = None
        return items

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

            all_files_per_lib = []
            for lib in libraries:
                files = self.list_files_recursive(lib["id"])
                all_files_per_lib.append((lib, files))
                self.progress.files_total += len(files)
                for f in files:
                    if self.manifest.needs_update(old_manifest, f["id"], f.get("eTag", ""), f.get("lastModifiedDateTime", "")):
                        self.progress.bytes_total += f.get("size", 0)

            for lib, files in all_files_per_lib:
                drive_id, lib_name = lib["id"], lib["name"]
                for item in files:
                    self._check_control()  # ★ Check before each file
                    fid = item["id"]
                    fname = item["name"]
                    fpath = item.get("parentReference", {}).get("path", "")
                    etag = item.get("eTag", "")
                    modified = item.get("lastModifiedDateTime", "")
                    fsize = item.get("size", 0)
                    rel = fpath.split("root:")[-1].lstrip("/")
                    dest = os.path.join(site_dir, lib_name, rel, fname)

                    if not self.manifest.needs_update(old_manifest, fid, etag, modified):
                        self.stats["files_skipped"] += 1
                        new_manifest[fid] = old_manifest[fid]
                        continue

                    try:
                        dl_url = f"{self.GRAPH}/drives/{drive_id}/items/{fid}/content"
                        self._download(dl_url, dest, fsize)
                        self.stats["files_downloaded"] += 1
                        self.stats["bytes_downloaded"] += fsize
                        new_manifest[fid] = {
                            "name": fname, "path": dest, "eTag": etag,
                            "lastModified": modified, "size": fsize,
                            "backupTime": datetime.now(timezone.utc).isoformat(),
                        }
                        self._emit("file_done", {"file": fname})
                    except PauseException:
                        raise
                    except Exception as e:
                        self.stats["errors"].append(f"{fname}: {e}")

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
        backup_dir = os.path.join(root_dir, f"backup_{ts}")
        os.makedirs(backup_dir, exist_ok=True)

        enabled_sites = [s for s in self.config["sites"] if s.get("enabled", True)]
        self.stats["total_sites"] = len(enabled_sites)
        self._emit("backup_start", {"total": len(enabled_sites), "dest": backup_dir})

        try:
            for site in enabled_sites:
                self.backup_site(site, backup_dir)
        except PauseException:
            self.stats["cancelled"] = True
            log.warning("Backup cancelled by user")

        self.stats["end_time"] = datetime.now(timezone.utc)
        if self.task_id:
            TaskController.cleanup(self.task_id)
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
        full_path = unquote(qs["id"][0]) if "id" in qs else unquote(parsed.path)
        full_path = full_path.strip("/")
        parts = full_path.split("/")
        if len(parts) >= 2 and parts[0] in ("sites", "teams"):
            site_path = f"{parts[0]}/{parts[1]}"
            folder_path = "/".join(parts[2:]) if len(parts) > 2 else ""
        else:
            site_path, folder_path = "", full_path
        return {
            "host": parsed.hostname, "site_path": site_path,
            "folder_path": folder_path, "full_url": url,
        }

    def download_custom_url(self, url, dest_dir: str = None):
        """
        Download from custom SharePoint URL.
        ★ NEW dest_dir parameter — can be:
          - Full path: /volume3/my-backups/project-x
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

            all_files = self.list_files_recursive(target_drive["id"], target_folder_id)
            self.progress.files_total = len(all_files)
            self.progress.bytes_total = sum(f.get("size", 0) for f in all_files)
            self._emit("custom_files_enumerated", {"total_files": len(all_files), "total_bytes": self.progress.bytes_total})

            downloaded = 0
            for item in all_files:
                try:
                    self._check_control()  # ★ Check pause/cancel
                    fname = item["name"]
                    fpath = item.get("parentReference", {}).get("path", "")
                    fsize = item.get("size", 0)
                    rel = fpath.split("root:")[-1].lstrip("/")
                    dest = os.path.join(dest_dir, rel, fname)
                    dl_url = f"{self.GRAPH}/drives/{target_drive['id']}/items/{item['id']}/content"
                    self._download(dl_url, dest, fsize)
                    downloaded += 1
                    self.stats["files_downloaded"] += 1
                    self.stats["bytes_downloaded"] += fsize
                    self._emit("file_done", {"file": fname})
                except PauseException:
                    raise
                except Exception as e:
                    self.stats["errors"].append(f"{item.get('name', '?')}: {e}")

            self._emit("custom_done")
            if self.task_id:
                TaskController.cleanup(self.task_id)
            return {
                "url": url, "downloaded": downloaded, "total": len(all_files),
                "dest": dest_dir, "bytes": self.stats["bytes_downloaded"],
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
