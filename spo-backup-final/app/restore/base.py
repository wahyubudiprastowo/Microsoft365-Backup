"""Base restore class with Graph auth, retry, and task control support."""
import logging
import time
from pathlib import Path

import msal
import requests
from app.http_utils import (
    build_retry_session,
    compute_backoff_delay,
    is_retryable_exception,
    is_retryable_status,
)

log = logging.getLogger("spo_backup")


class BaseRestore:
    WORKLOAD_NAME = "base"
    GRAPH = "https://graph.microsoft.com/v1.0"

    def __init__(self, tenant: dict, backup_path: str, progress_callback=None, task_id: str = None, mode: str = "merge"):
        self.tenant = tenant
        self.backup_path = Path(backup_path)
        self.progress_callback = progress_callback
        self.task_id = task_id
        self.mode = mode
        self._token = None
        self._token_expires_at = 0
        self.session = build_retry_session()
        self.stats = {
            "tenant_id": tenant.get("id"),
            "tenant_name": tenant.get("name"),
            "workload": self.WORKLOAD_NAME,
            "mode": mode,
            "backup_path": str(self.backup_path),
            "start_time": None,
            "end_time": None,
            "items_processed": 0,
            "items_skipped": 0,
            "items_failed": 0,
            "bytes_uploaded": 0,
            "targets_processed": 0,
            "targets_failed": 0,
            "errors": [],
            "cancelled": False,
        }
        self._last_emit = 0

    def get_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expires_at - 60:
            return self._token
        app = msal.ConfidentialClientApplication(
            self.tenant["client_id"],
            authority=f"https://login.microsoftonline.com/{self.tenant['tenant_id']}",
            client_credential=self.tenant["client_secret"],
        )
        result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" not in result:
            raise Exception(f"Auth failed: {result.get('error_description', 'Unknown')}")
        self._token = result["access_token"]
        self._token_expires_at = now + result.get("expires_in", 3600)
        return self._token

    def _headers(self, content_type="application/json") -> dict:
        return {
            "Authorization": f"Bearer {self.get_token()}",
            "Content-Type": content_type,
        }

    def _request(self, method, url, **kwargs):
        response = None
        last_error = None
        for attempt in range(5):
            try:
                response = self.session.request(method, url, **kwargs)
                if response.status_code == 401 and attempt < 4:
                    self._token = None
                    self._token_expires_at = 0
                    time.sleep(1)
                    continue
                if is_retryable_status(response.status_code) and attempt < 4:
                    time.sleep(compute_backoff_delay(attempt, response=response))
                    continue
                response.raise_for_status()
                return response
            except requests.HTTPError as e:
                last_error = e
                if response is not None and response.status_code in (400, 404, 409):
                    raise
                if response is not None and is_retryable_status(response.status_code) and attempt < 4:
                    time.sleep(compute_backoff_delay(attempt, response=response))
                    continue
                if attempt == 4:
                    raise
                time.sleep(compute_backoff_delay(attempt, response=response))
            except Exception as e:
                last_error = e
                if not is_retryable_exception(e) or attempt == 4:
                    raise
                time.sleep(compute_backoff_delay(attempt, response=response))
        if last_error:
            raise last_error
        raise Exception(f"Failed after retries: {method} {url}")

    def _get(self, url, params=None):
        return self._request("GET", url, headers=self._headers(), params=params, timeout=60).json()

    def _post(self, url, json_body=None, data=None, content_type="application/json"):
        response = self._request("POST", url, headers=self._headers(content_type), json=json_body, data=data, timeout=120)
        return response.json() if response.text else {}

    def _put(self, url, data=None, content_type="application/octet-stream"):
        response = self._request("PUT", url, headers=self._headers(content_type), data=data, timeout=300)
        return response.json() if response.text else {}

    def _check_control(self):
        if not self.task_id:
            return
        from app.task_control import PauseException, check_control

        try:
            check_control(self.task_id)
        except PauseException:
            self.stats["cancelled"] = True
            self.emit("cancelled")
            raise

    def emit(self, event: str, extra: dict = None):
        now = time.time()
        important = event in {"restore_start", "restore_done", "target_start", "target_done", "cancelled"}
        if important or (now - self._last_emit > 0.5):
            self._last_emit = now
            if self.progress_callback:
                data = dict(self.stats)
                if extra:
                    data.update(extra)
                data["event"] = event
                self.progress_callback(event, data)

    def restore(self) -> dict:
        raise NotImplementedError()

    def dry_run(self) -> dict:
        raise NotImplementedError()
