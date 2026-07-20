"""Restore Manager v2 for multi-workload restore jobs."""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import redis as redis_lib

from app.backup_registry import BackupRegistry
from app.workloads import WORKLOAD_META, get_workload

import logging

log = logging.getLogger("spo_backup")


class RestoreManagerV2:
    JOB_PREFIX = "m365:restore_job:"
    JOB_LIST = "m365:restore_jobs:list"

    def __init__(self):
        self.r = redis_lib.Redis(host="redis", port=6379, db=2, decode_responses=True)

    def _job_key(self, job_id: str) -> str:
        return self.JOB_PREFIX + job_id

    def create_job(self, config: dict) -> dict:
        workload, mode, backup_path = self._validate_config(config)

        job = {
            "id": str(uuid.uuid4()),
            "tenant_id": config["tenant_id"],
            "tenant_name": config.get("tenant_name", ""),
            "workload": workload,
            "backup_path": str(backup_path),
            "source_backup": config["source_backup"],
            "mode": mode,
            "status": "queued",
            "progress": 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "started_at": None,
            "completed_at": None,
            "result": None,
            "error": None,
            "task_id": None,
        }

        if workload == "sharepoint":
            job["target_site_id"] = config.get("target_site_id")
            job["target_site_path"] = config.get("target_site_path")
        elif workload == "onedrive":
            job["user_mapping"] = config.get("user_mapping", {})
            job["target_folder"] = config.get("target_folder", "M365 Restored")
        elif workload == "outlook":
            job["user_mapping"] = config.get("user_mapping", {})
            job["restore_items"] = config.get("restore_items", ["messages", "calendar", "contacts"])
        elif workload == "teams":
            job["export_format"] = config.get("export_format", "all")
            job["export_dir"] = config.get("export_dir")

        self.r.set(self._job_key(job["id"]), json.dumps(job))
        self.r.lpush(self.JOB_LIST, job["id"])
        self.r.ltrim(self.JOB_LIST, 0, 99)
        log.info(f"Restore job created: {job['id'][:8]} — {job['workload']} for {job.get('tenant_name', '?')}")
        return job

    def get_job(self, job_id: str):
        data = self.r.get(self._job_key(job_id))
        return json.loads(data) if data else None

    def list_jobs(self, limit: int = 50):
        ids = self.r.lrange(self.JOB_LIST, 0, max(limit - 1, 0))
        jobs = []
        for job_id in ids:
            job = self.get_job(job_id)
            if job:
                jobs.append(job)
        return jobs

    def update_job(self, job_id: str, updates: dict):
        job = self.get_job(job_id)
        if not job:
            return None
        job.update(updates)
        self.r.set(self._job_key(job_id), json.dumps(job))
        return job

    def delete_job(self, job_id: str) -> bool:
        job = self.get_job(job_id)
        if not job:
            return False
        if job.get("status") in {"queued", "running", "paused"}:
            return False
        self.r.delete(self._job_key(job_id))
        self.r.lrem(self.JOB_LIST, 0, job_id)
        return True

    def execute(self, job_id: str, task_id: str = None) -> dict:
        from app.restore import get_restore
        from app.task_control import PauseException
        from app.tenant_manager import TenantManager

        job = self.get_job(job_id)
        if not job:
            return {"error": "Job not found"}
        self.update_job(job_id, {
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "task_id": task_id,
        })

        tenant = TenantManager().get_tenant(job["tenant_id"], include_secret=True)
        if not tenant:
            self.update_job(job_id, {
                "status": "failed",
                "error": f"Tenant not found: {job['tenant_id']}",
                "completed_at": datetime.now(timezone.utc).isoformat(),
            })
            return {"error": "Tenant not found"}

        def progress_cb(evt, data):
            processed = data.get("items_processed", 0)
            failed = data.get("items_failed", 0)
            total_so_far = processed + failed
            progress = min(99, int((processed / max(total_so_far, 1)) * 100)) if total_so_far else 5
            self.update_job(job_id, {
                "progress": progress,
                "current_target": data.get("target_name", ""),
                "items_processed": processed,
                "items_failed": failed,
                "bytes_uploaded": data.get("bytes_uploaded", 0),
            })

        kwargs = {"mode": job["mode"]}
        if job["workload"] == "sharepoint":
            kwargs["target_site_id"] = job.get("target_site_id")
            kwargs["target_site_path"] = job.get("target_site_path")
        elif job["workload"] == "onedrive":
            kwargs["user_mapping"] = job.get("user_mapping", {})
            kwargs["target_folder"] = job.get("target_folder", "Restored")
        elif job["workload"] == "outlook":
            kwargs["user_mapping"] = job.get("user_mapping", {})
            kwargs["restore_items"] = job.get("restore_items", ["messages", "calendar", "contacts"])
        elif job["workload"] == "teams":
            kwargs["export_format"] = job.get("export_format", "all")
            kwargs["export_dir"] = job.get("export_dir")

        try:
            restorer = get_restore(
                workload=job["workload"],
                tenant=tenant,
                backup_path=job["backup_path"],
                progress_callback=progress_cb,
                task_id=task_id,
                **kwargs,
            )
            result = restorer.restore()
            status = "completed"
            if result.get("cancelled"):
                status = "cancelled"
            elif result.get("targets_failed", 0) > 0 and result.get("targets_processed", 0) == 0:
                status = "failed"
            self.update_job(job_id, {
                "status": status,
                "progress": 100 if status != "cancelled" else job.get("progress", 0),
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "result": result,
            })
            return result
        except PauseException:
            self.update_job(job_id, {
                "status": "cancelled",
                "completed_at": datetime.now(timezone.utc).isoformat(),
            })
            return {"cancelled": True}
        except Exception as e:
            log.error(f"Restore job {job_id[:8]} failed: {e}", exc_info=True)
            self.update_job(job_id, {
                "status": "failed",
                "error": str(e),
                "completed_at": datetime.now(timezone.utc).isoformat(),
            })
            return {"error": str(e)}

    def dry_run(self, config: dict) -> dict:
        from app.restore import get_restore
        from app.tenant_manager import TenantManager

        workload, mode, backup_path = self._validate_config(config)
        tenant = TenantManager().get_tenant(config["tenant_id"], include_secret=True)
        if not tenant:
            return {"error": "Tenant not found"}

        kwargs = {"mode": mode}
        if workload == "sharepoint":
            kwargs["target_site_id"] = config.get("target_site_id")
            kwargs["target_site_path"] = config.get("target_site_path")
        elif workload == "onedrive":
            kwargs["user_mapping"] = config.get("user_mapping", {})
            kwargs["target_folder"] = config.get("target_folder", "M365 Restored")
        elif workload == "outlook":
            kwargs["user_mapping"] = config.get("user_mapping", {})
            kwargs["restore_items"] = config.get("restore_items", ["messages", "calendar", "contacts"])
        elif workload == "teams":
            kwargs["export_format"] = config.get("export_format", "all")
            kwargs["export_dir"] = config.get("export_dir")

        result = get_restore(
            workload=workload,
            tenant=tenant,
            backup_path=str(backup_path),
            **kwargs,
        ).dry_run()
        result["permission_preflight"] = self._check_restore_permission(workload, tenant)
        result["backup_exists"] = True
        result["backup_path"] = str(backup_path)
        return result

    def _classify_permission_error(self, raw_error: str) -> dict:
        text = str(raw_error or "")
        lower = text.lower()
        if "403" in text or "forbidden" in lower:
            return {
                "ready": False,
                "error_type": "permission_denied",
                "message": "Target tenant does not currently expose enough Microsoft Graph permission for this restore workload.",
                "error_detail": text,
            }
        if "auth failed" in lower or "unauthorized" in lower or "401" in text:
            return {
                "ready": False,
                "error_type": "auth_failed",
                "message": "Authentication to Microsoft Graph failed for the selected tenant.",
                "error_detail": text,
            }
        return {
            "ready": False,
            "error_type": "discovery_failed",
            "message": "Restore target readiness could not be confirmed for this tenant.",
            "error_detail": text,
        }

    def _check_restore_permission(self, workload: str, tenant: dict) -> dict:
        base = {
            "ready": True,
            "workload": workload,
            "required_scopes": WORKLOAD_META.get(workload, {}).get("required_scopes", []),
        }
        try:
            targets = get_workload(workload, tenant).list_targets()
            if targets and isinstance(targets, list) and targets[0].get("error"):
                issue = self._classify_permission_error(targets[0]["error"])
                issue["workload"] = workload
                issue["required_scopes"] = base["required_scopes"]
                return issue
            base["targets_discovered"] = len(targets or [])
            return base
        except Exception as e:
            issue = self._classify_permission_error(str(e))
            issue["workload"] = workload
            issue["required_scopes"] = base["required_scopes"]
            return issue

    def _validate_backup_path(self, backup_path: Path):
        registry = BackupRegistry()
        allowed_roots = [registry.legacy_root.resolve(), registry.tenant_root.resolve()]
        if not any(str(backup_path).startswith(str(root)) for root in allowed_roots):
            raise ValueError(f"Backup path not allowed: {backup_path}")

    def _validate_config(self, config: dict):
        for field in ["tenant_id", "workload", "backup_path", "source_backup"]:
            if not config.get(field):
                raise ValueError(f"Missing required field: {field}")

        workload = str(config["workload"]).strip().lower()
        if workload not in {"sharepoint", "onedrive", "outlook", "teams"}:
            raise ValueError(f"Unknown workload: {workload}")

        mode = str(config.get("mode", "merge")).strip().lower()
        if mode not in {"overwrite", "merge", "new_location"}:
            raise ValueError(f"Invalid mode: {mode}")

        backup_path = Path(config["backup_path"]).resolve()
        self._validate_backup_path(backup_path)
        if not backup_path.exists() or not backup_path.is_dir():
            raise ValueError(f"Backup path not found: {backup_path}")

        if workload == "sharepoint":
            target_site_id = (config.get("target_site_id") or "").strip()
            target_site_path = (config.get("target_site_path") or "").strip()
            if not target_site_id and not target_site_path:
                raise ValueError("SharePoint restore requires target_site_path or target_site_id")
        elif workload == "outlook":
            allowed_items = {"messages", "calendar", "contacts"}
            restore_items = config.get("restore_items") or []
            invalid_items = [item for item in restore_items if item not in allowed_items]
            if not restore_items:
                raise ValueError("Outlook restore requires at least one restore item")
            if invalid_items:
                raise ValueError(f"Invalid Outlook restore items: {', '.join(invalid_items)}")
        elif workload == "teams":
            export_format = str(config.get("export_format", "all")).strip().lower()
            if export_format not in {"all", "html", "json", "txt"}:
                raise ValueError(f"Invalid Teams export format: {export_format}")

        return workload, mode, backup_path
