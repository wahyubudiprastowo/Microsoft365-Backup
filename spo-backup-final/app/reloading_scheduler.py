"""Celery beat scheduler that reloads the mounted configuration safely."""
import logging
import os
import time

from celery.beat import PersistentScheduler

from app.config_manager import CONFIG_PATH
from app.tasks_v12 import register_tenant_schedules

log = logging.getLogger("spo_backup")


class ReloadingScheduler(PersistentScheduler):
    def tick(self):
        now = time.monotonic()
        if now - getattr(self, "_last_config_check", 0) >= 30:
            self._last_config_check = now
            try:
                stamp = os.stat(CONFIG_PATH).st_mtime_ns
                if stamp != getattr(self, "_config_stamp", None):
                    register_tenant_schedules(self.app)
                    desired = dict(self.app.conf.beat_schedule or {})
                    for name in list(self.schedule):
                        if name.startswith("scheduled-backup") and name not in desired:
                            del self.schedule[name]
                    self.update_from_dict(desired)
                    self.sync()
                    self._config_stamp = stamp
                    log.info("Celery beat reloaded %s backup schedule(s)", len(desired))
            except Exception as exc:
                log.error("Celery beat schedule reload failed: %s", exc)
        return super().tick()
