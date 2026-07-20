"""v13 additive Celery restore task registration."""
import logging

log = logging.getLogger("spo_backup")


def register_v13_tasks(celery_app):
    @celery_app.task(bind=True, name="app.tasks.execute_restore_job_v2")
    def execute_restore_job_v2(self, job_id: str):
        from app.restore_manager_v2 import RestoreManagerV2

        mgr = RestoreManagerV2()
        log.info(f"Restore v2 job {job_id[:8]} started")
        return mgr.execute(job_id, task_id=self.request.id)

    return execute_restore_job_v2
