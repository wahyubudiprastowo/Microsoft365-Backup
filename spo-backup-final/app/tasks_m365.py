"""Supplemental Celery tasks for multi-tenant restore jobs."""
from datetime import datetime, timezone
from pathlib import Path

from app.restore_manager import RestoreEngine, RestoreManager
from app.tasks import celery_app
from app.tenant_manager import TenantManager


@celery_app.task(bind=True, name="app.tasks_m365.execute_restore_job")
def execute_restore_job(self, job_id):
    rm = RestoreManager()
    tm = TenantManager()
    job = rm.get_job(job_id)
    if not job:
        return {"error": "Job not found"}

    rm.update_job(job_id, status="running", started_at=datetime.now(timezone.utc).isoformat())
    tenant = tm.get_tenant(job["tenant_id"])
    if not tenant:
        rm.update_job(job_id, status="failed", errors=["Tenant not found"])
        raise Exception("Tenant not found")

    from app.config_manager import load_config
    config = load_config()
    backup_root = Path(config["backup"]["root_dir"]) / job["source_backup"]
    if job.get("source_site"):
        backup_root = backup_root / job["source_site"].replace(" ", "_")
    if not backup_root.exists():
        rm.update_job(job_id, status="failed", errors=[f"Backup path not found: {backup_root}"])
        raise Exception(f"Backup path not found: {backup_root}")

    engine = RestoreEngine(tenant)

    def progress_cb(evt, data):
        rm.update_job(job_id, progress=data.get("progress", 0), files_done=data.get("uploaded", 0), bytes_done=data.get("bytes", 0))
        self.update_state(state="PROGRESS", meta={"event": evt, **data})

    result = engine.restore_sharepoint(job, str(backup_root), progress_cb)
    status = "completed" if not result.get("errors") else "failed"
    rm.update_job(
        job_id,
        status=status,
        progress=100 if status == "completed" else rm.get_job(job_id).get("progress", 0),
        files_done=result.get("uploaded", 0),
        bytes_done=result.get("bytes", 0),
        errors=result.get("errors", []),
        finished_at=datetime.now(timezone.utc).isoformat(),
    )
    return result
