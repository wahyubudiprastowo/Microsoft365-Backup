"""Distributed runtime lease to suppress duplicate task execution copies."""
from __future__ import annotations

import os
import uuid
import fcntl

import redis as redis_lib


class TaskRuntimeLease:
    def __init__(self, group: str, task_id: str, ttl_seconds: int = 900):
        self.group = group
        self.task_id = task_id
        self.ttl_seconds = ttl_seconds
        self.token = str(uuid.uuid4())
        self.r = redis_lib.Redis(host="redis", port=6379, db=2, decode_responses=True)

    @property
    def key(self) -> str:
        return f"spo:lease:{self.group}:{self.task_id}"

    def acquire(self) -> bool:
        return bool(self.r.set(self.key, self.token, nx=True, ex=self.ttl_seconds))

    def refresh(self) -> bool:
        if self.r.get(self.key) != self.token:
            return False
        return bool(self.r.expire(self.key, self.ttl_seconds))

    def release(self) -> bool:
        if self.r.get(self.key) != self.token:
            return False
        return bool(self.r.delete(self.key))


class LocalProcessSlot:
    """Host-local non-blocking file lock to prevent duplicate worker processes from running the same slot."""

    def __init__(self, group: str):
        self.group = str(group or "default").strip() or "default"
        self.path = f"/tmp/spo_slot_{self.group}.lock"
        self.handle = None

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.handle = open(self.path, "a+")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.handle.seek(0)
            self.handle.truncate()
            self.handle.write(str(os.getpid()))
            self.handle.flush()
            return True
        except OSError:
            try:
                self.handle.close()
            except Exception:
                pass
            self.handle = None
            return False

    def release(self) -> bool:
        if not self.handle:
            return False
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None
            return True
        except Exception:
            return False
