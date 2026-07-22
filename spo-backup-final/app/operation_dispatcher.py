"""Queue dispatcher helpers for queued backup/download/restore operations."""
import logging

from app.operation_queue import OperationQueue

log = logging.getLogger("spo_backup")


def dispatch_next_queued_operation(group: str):
    queue = OperationQueue()
    lock_token = queue.acquire_dispatch_lock(group)
    if not lock_token:
        return {"status": "busy", "group": group}

    item = queue.pop_next(group)
    if not item:
        queue.release_dispatch_lock(group, lock_token)
        return None

    payload = item.get("payload") or {}
    operation = item.get("operation")

    try:
        if group == "backup":
            from app.tasks import run_backup_task

            if operation == "legacy_backup":
                task = run_backup_task.delay(custom_root=payload.get("custom_root"))
            elif operation == "tenant_backup":
                task = run_backup_task.delay(
                    custom_root=payload.get("custom_root"),
                    tenant_id=payload.get("tenant_id"),
                    workloads=payload.get("workloads") or [],
                )
            else:
                raise ValueError(f"Unknown queued backup operation: {operation}")
        elif group == "download":
            from app.tasks import download_custom_url_task

            task = download_custom_url_task.delay(
                payload.get("url"),
                dest_dir=payload.get("dest_dir"),
            )
        elif group == "restore":
            if operation != "restore_v2":
                raise ValueError(f"Unknown queued restore operation: {operation}")
            from app.restore_manager_v2 import RestoreManagerV2
            from app.tasks import celery_app

            job_id = payload.get("job_id")
            mgr = RestoreManagerV2()
            job = mgr.get_job(job_id)
            if not job or job.get("status") != "queued" or job.get("task_id"):
                queue.remove(item["id"], group=group)
                return {"status": "skipped", "group": group, "queue_item_id": item["id"]}
            task = celery_app.send_task("app.tasks.execute_restore_job_v2", args=[job_id], queue="restore")
            mgr.update_job(job_id, {"task_id": task.id, "status": "queued"})
        else:
            raise ValueError(f"Unsupported queue group: {group}")
    except Exception as e:
        queue.requeue_front(item, group=group)
        log.error(f"Failed to dispatch queued {group} item {item.get('id', '?')[:8]}: {e}", exc_info=True)
        return {"error": str(e), "item": item}
    finally:
        queue.release_dispatch_lock(group, lock_token)

    queue.remove(item["id"], group=group)
    log.info(f"Queued {group} item {item['id'][:8]} dispatched as task {task.id[:8]}")
    return {
        "group": group,
        "queue_item_id": item["id"],
        "task_id": task.id,
        "operation": operation,
    }
