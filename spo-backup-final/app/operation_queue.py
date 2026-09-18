"""Redis-backed operation queue for backup/download/restore dispatching."""
import json
import uuid
from datetime import datetime, timezone
from typing import Optional

import redis as redis_lib


class OperationQueue:
    ITEM_PREFIX = "m365:opqueue:item:"
    QUEUE_PREFIX = "m365:opqueue:list:"
    LOCK_PREFIX = "m365:opqueue:dispatch:"
    GROUPS = ("backup", "download", "restore")

    def __init__(self):
        self.r = redis_lib.Redis(host="redis", port=6379, db=2, decode_responses=True)

    def _item_key(self, item_id: str) -> str:
        return self.ITEM_PREFIX + item_id

    def _queue_key(self, group: str) -> str:
        return self.QUEUE_PREFIX + group

    def _lock_key(self, group: str) -> str:
        return self.LOCK_PREFIX + group

    def enqueue(self, group: str, operation: str, payload: dict, title: str, detail: str = "") -> dict:
        if group not in self.GROUPS:
            raise ValueError(f"Unsupported operation group: {group}")
        item = {
            "id": str(uuid.uuid4()),
            "group": group,
            "operation": operation,
            "payload": payload or {},
            "title": title,
            "detail": detail,
            "status": "queued",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        raw = json.dumps(item)
        self.r.set(self._item_key(item["id"]), raw)
        self.r.rpush(self._queue_key(group), item["id"])
        item["position"] = self.position(item["id"], group=group)
        return item

    def get(self, item_id: str):
        raw = self.r.get(self._item_key(item_id))
        return json.loads(raw) if raw else None

    def length(self, group: str) -> int:
        return int(self.r.llen(self._queue_key(group)) or 0)

    def list(self, group: str | None = None, limit: int = 50) -> list:
        groups = [group] if group else list(self.GROUPS)
        items = []
        for current_group in groups:
            queue_ids = self.r.lrange(self._queue_key(current_group), 0, max(limit - 1, 0))
            for item_id in queue_ids:
                item = self.get(item_id)
                if item:
                    item["position"] = self.position(item_id, group=current_group)
                    items.append(item)
                else:
                    self.r.lrem(self._queue_key(current_group), 0, item_id)
        items.sort(key=lambda item: item.get("created_at", ""))
        return items[:limit]

    def position(self, item_id: str, group: str | None = None):
        groups = [group] if group else list(self.GROUPS)
        for current_group in groups:
            queue_ids = self.r.lrange(self._queue_key(current_group), 0, -1)
            for index, current_id in enumerate(queue_ids, start=1):
                if current_id == item_id:
                    return index
        return None

    def peek_next(self, group: str):
        queue_key = self._queue_key(group)
        queue_ids = self.r.lrange(queue_key, 0, -1)
        for item_id in queue_ids:
            item = self.get(item_id)
            if item:
                item["position"] = self.position(item_id, group=group)
                return item
            self.r.lrem(queue_key, 0, item_id)
        return None

    def pop_next(self, group: str):
        queue_key = self._queue_key(group)
        while True:
            item_id = self.r.lpop(queue_key)
            if not item_id:
                return None
            item = self.get(item_id)
            if item:
                return item

    def requeue_front(self, item: dict, group: Optional[str] = None) -> dict:
        current_group = group or item.get("group")
        if current_group not in self.GROUPS:
            raise ValueError(f"Unsupported operation group: {current_group}")
        self.r.set(self._item_key(item["id"]), json.dumps(item))
        self.r.lpush(self._queue_key(current_group), item["id"])
        item["position"] = self.position(item["id"], group=current_group)
        return item

    def acquire_dispatch_lock(self, group: str, ttl_seconds: int = 30) -> Optional[str]:
        token = str(uuid.uuid4())
        acquired = self.r.set(self._lock_key(group), token, nx=True, ex=ttl_seconds)
        return token if acquired else None

    def release_dispatch_lock(self, group: str, token: Optional[str] = None) -> bool:
        lock_key = self._lock_key(group)
        if token is None:
            return bool(self.r.delete(lock_key))
        current = self.r.get(lock_key)
        if current == token:
            return bool(self.r.delete(lock_key))
        return False

    def remove(self, item_id: str, group: str | None = None) -> bool:
        item = self.get(item_id)
        self.r.delete(self._item_key(item_id))
        groups = [group] if group else ([item["group"]] if item else list(self.GROUPS))
        removed = False
        for current_group in groups:
            if self.r.lrem(self._queue_key(current_group), 0, item_id):
                removed = True
        return removed or bool(item)
