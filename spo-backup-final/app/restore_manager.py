"""Job-based restore manager compatible with existing backup layout."""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests


class RestoreJob:
    MODE_OVERWRITE = "overwrite"
    MODE_MERGE = "merge"
    MODE_NEW_LOCATION = "new_location"

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", str(uuid.uuid4()))
        self.tenant_id = kwargs.get("tenant_id")
        self.tenant_name = kwargs.get("tenant_name", "")
        self.workload = kwargs.get("workload", "sharepoint")
        self.source_backup = kwargs.get("source_backup", "")
        self.source_site = kwargs.get("source_site", "")
        self.target_site = kwargs.get("target_site", "")
        self.target_location = kwargs.get("target_location", "")
        self.mode = kwargs.get("mode", self.MODE_MERGE)
        self.status = kwargs.get("status", "queued")
        self.progress = kwargs.get("progress", 0)
        self.files_done = kwargs.get("files_done", 0)
        self.bytes_done = kwargs.get("bytes_done", 0)
        self.errors = kwargs.get("errors", [])
        self.started_at = kwargs.get("started_at")
        self.finished_at = kwargs.get("finished_at")
        self.task_id = kwargs.get("task_id")

    def to_dict(self):
        return self.__dict__


class RestoreManager:
    def __init__(self, jobs_dir="/app/logs/restore_jobs"):
        self.jobs_dir = Path(jobs_dir)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

    def _job_file(self, job_id):
        return self.jobs_dir / f"{job_id}.json"

    def list_jobs(self, limit=50):
        jobs = []
        for job_file in sorted(self.jobs_dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
            try:
                with open(job_file) as handle:
                    jobs.append(json.load(handle))
            except Exception:
                pass
        return jobs

    def get_job(self, job_id):
        job_file = self._job_file(job_id)
        if not job_file.exists():
            return None
        with open(job_file) as handle:
            return json.load(handle)

    def save_job(self, job_data):
        with open(self._job_file(job_data["id"]), "w") as handle:
            json.dump(job_data, handle, indent=2, default=str)

    def create_job(self, **kwargs):
        job = RestoreJob(**kwargs)
        self.save_job(job.to_dict())
        return job

    def update_job(self, job_id, **updates):
        job = self.get_job(job_id)
        if not job:
            return None
        job.update(updates)
        self.save_job(job)
        return job

    def delete_job(self, job_id):
        job_file = self._job_file(job_id)
        if job_file.exists():
            job_file.unlink()
            return True
        return False


class RestoreEngine:
    GRAPH = "https://graph.microsoft.com/v1.0"

    def __init__(self, tenant_config):
        from app.workloads.base import BaseWorkload
        self.tenant = tenant_config
        self._auth = BaseWorkload(tenant_config)

    def _headers(self):
        return {"Authorization": f"Bearer {self._auth.get_token()}", "Content-Type": "application/octet-stream"}

    def restore_sharepoint(self, job, backup_path, progress_callback=None):
        stats = {"uploaded": 0, "skipped": 0, "errors": [], "bytes": 0}
        mode = job.get("mode", RestoreJob.MODE_MERGE)
        host = self.tenant.get("sharepoint_host", "")
        site_path = job.get("target_location") if mode == RestoreJob.MODE_NEW_LOCATION and job.get("target_location") else job.get("target_site")
        try:
            site_url = f"{self.GRAPH}/sites/{host}:/{site_path}" if site_path else f"{self.GRAPH}/sites/{host}"
            site_data = requests.get(site_url, headers=self._headers(), timeout=30).json()
            site_id = site_data["id"]
            drives_data = requests.get(f"{self.GRAPH}/sites/{site_id}/drives", headers=self._headers(), timeout=30).json()
            drives_map = {drive["name"]: drive["id"] for drive in drives_data.get("value", [])}

            backup_root = Path(backup_path)
            all_files = [f for f in backup_root.rglob("*") if f.is_file() and not f.name.startswith("_")]
            total = len(all_files) or 1

            for idx, file_path in enumerate(all_files, start=1):
                try:
                    parts = file_path.relative_to(backup_root).parts
                    if len(parts) < 2:
                        continue
                    lib_name = parts[0]
                    drive_id = drives_map.get(lib_name)
                    if not drive_id:
                        continue
                    rel = "/".join(parts[1:])
                    if mode == RestoreJob.MODE_MERGE:
                        check = requests.get(
                            f"{self.GRAPH}/drives/{drive_id}/root:/{rel}",
                            headers={"Authorization": self._headers()["Authorization"]},
                            timeout=15,
                        )
                        if check.status_code == 200:
                            stats["skipped"] += 1
                            continue
                    with open(file_path, "rb") as handle:
                        response = requests.put(
                            f"{self.GRAPH}/drives/{drive_id}/root:/{rel}:/content",
                            headers=self._headers(),
                            data=handle,
                            timeout=120,
                        )
                        response.raise_for_status()
                    stats["uploaded"] += 1
                    stats["bytes"] += file_path.stat().st_size
                    if progress_callback:
                        progress_callback("file_uploaded", {"file": file_path.name, "progress": int(idx / total * 100), **stats})
                except Exception as e:
                    stats["errors"].append(f"{file_path.name}: {e}")
        except Exception as e:
            stats["errors"].append(f"Restore failed: {e}")
        return stats
