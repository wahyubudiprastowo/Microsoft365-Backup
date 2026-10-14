"""
Task Control Manager — Redis-based pause/resume/cancel for backup tasks.

Mechanism:
- Each task has a control key in Redis: spo:task:<task_id>:control
- Worker checks this key during download loop
- States: "running" (default), "paused", "cancelled"
"""
import time
import redis
import logging

log = logging.getLogger("spo_backup")

# Redis connection (reuses Celery broker)
_redis = redis.Redis(host="redis", port=6379, db=2, decode_responses=True)


CONTROL_KEY = "spo:task:{task_id}:control"
PROGRESS_KEY = "spo:task:{task_id}:progress"


class TaskController:
    """Control & monitor a download/backup task."""

    STATE_RUNNING = "running"
    STATE_PAUSED = "paused"
    STATE_CANCELLED = "cancelled"

    @staticmethod
    def set_state(task_id: str, state: str, ttl: int = 86400):
        """Set control state for a task (with 24h TTL)."""
        _redis.setex(CONTROL_KEY.format(task_id=task_id), ttl, state)
        log.info(f"Task {task_id[:8]} → {state}")

    @staticmethod
    def get_state(task_id: str) -> str:
        """Get current control state."""
        state = _redis.get(CONTROL_KEY.format(task_id=task_id))
        return state or TaskController.STATE_RUNNING

    @staticmethod
    def pause(task_id: str):
        TaskController.set_state(task_id, TaskController.STATE_PAUSED)

    @staticmethod
    def resume(task_id: str):
        TaskController.set_state(task_id, TaskController.STATE_RUNNING)

    @staticmethod
    def cancel(task_id: str):
        TaskController.set_state(task_id, TaskController.STATE_CANCELLED)

    @staticmethod
    def cleanup(task_id: str):
        """Remove control key after task completes."""
        _redis.delete(CONTROL_KEY.format(task_id=task_id))


class PauseException(Exception):
    """Raised when task is cancelled by user."""
    pass


def check_control(task_id: str, max_pause_seconds: int = 3600):
    """
    Called inside download loop. Behavior:
    - If "paused": sleep & poll until resumed/cancelled (max 1h)
    - If "cancelled": raise PauseException
    - If "running": return immediately
    """
    state = TaskController.get_state(task_id)

    if state == TaskController.STATE_CANCELLED:
        raise PauseException("Task cancelled by user")

    if state == TaskController.STATE_PAUSED:
        log.info(f"Task {task_id[:8]} paused, waiting...")
        elapsed = 0
        while elapsed < max_pause_seconds:
            time.sleep(1)
            elapsed += 1
            state = TaskController.get_state(task_id)
            if state == TaskController.STATE_RUNNING:
                log.info(f"Task {task_id[:8]} resumed")
                return
            if state == TaskController.STATE_CANCELLED:
                raise PauseException("Task cancelled by user")
        # Timeout after 1h pause
        raise PauseException("Pause timeout exceeded (1h)")
