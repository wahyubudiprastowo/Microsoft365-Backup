"""v13 additive Celery restore task registration."""
import logging

from app.operation_dispatcher import dispatch_next_queued_operation
from app.task_runtime_lease import TaskRuntimeLease

log = logging.getLogger("spo_backup")


def register_v13_tasks(celery_app):
    @celery_app.task(bind=True, name="app.tasks.execute_restore_job_v2")
    def execute_restore_job_v2(self, job_id: str):
        from app.restore_manager_v2 import RestoreManagerV2

        mgr = RestoreManagerV2()
        runtime_lease = TaskRuntimeLease("restore", self.request.id, ttl_seconds=900)
        if not runtime_lease.acquire():
            log.warning(f"Duplicate restore task execution suppressed for {self.request.id[:8]}")
            return {"status": "duplicate_ignored", "job_id": job_id}
        log.info(f"Restore v2 job {job_id[:8]} started")
        try:
            return mgr.execute(job_id, task_id=self.request.id)
        finally:
            try:
                runtime_lease.release()
            except Exception:
                pass
            dispatch_next_queued_operation("restore")

    return execute_restore_job_v2
