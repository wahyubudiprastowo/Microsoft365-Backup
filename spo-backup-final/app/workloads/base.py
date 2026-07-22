"""Base workload utilities for Microsoft Graph access."""
import os
import time

import msal
import requests

from app.http_utils import (
    build_retry_session,
    compute_backoff_delay,
    is_retryable_exception,
    is_retryable_status,
)


class BaseWorkload:
    GRAPH = "https://graph.microsoft.com/v1.0"
    workload_type = "base"

    def __init__(self, tenant_config):
        self.tenant = tenant_config
        self.tenant_id = tenant_config["tenant_id"]
        self.client_id = tenant_config["client_id"]
        self.client_secret = tenant_config["client_secret"]
        self.session = build_retry_session()
        self._token = None
        self._token_expiry = 0

    def get_token(self):
        if self._token and time.time() < self._token_expiry:
            return self._token
        app = msal.ConfidentialClientApplication(
            self.client_id,
            authority=f"https://login.microsoftonline.com/{self.tenant_id}",
            client_credential=self.client_secret,
        )
        result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" not in result:
            raise Exception(f"Auth failed: {result.get('error_description', 'Unknown')}")
        self._token = result["access_token"]
        self._token_expiry = time.time() + 3500
        return self._token

    def _headers(self):
        return {"Authorization": f"Bearer {self.get_token()}", "Content-Type": "application/json"}

    def _get(self, url, params=None, max_retry=3):
        response = None
        last_error = None
        for attempt in range(max_retry):
            try:
                response = self.session.get(url, headers=self._headers(), params=params, timeout=(20, 60))
                if response.status_code == 401 and attempt < max_retry - 1:
                    self._token = None
                    self._token_expiry = 0
                    time.sleep(1)
                    continue
                if is_retryable_status(response.status_code) and attempt < max_retry - 1:
                    time.sleep(compute_backoff_delay(attempt, response=response))
                    continue
                response.raise_for_status()
                return response.json()
            except Exception as e:
                last_error = e
                if not is_retryable_exception(e) or attempt == max_retry - 1:
                    raise
                time.sleep(compute_backoff_delay(attempt, response=response))
        if last_error:
            raise last_error
        return {}

    def _paginate(self, url, params=None):
        while url:
            data = self._get(url, params)
            for item in data.get("value", []):
                yield item
            url = data.get("@odata.nextLink")
            params = None

    def get_target_selection(self):
        raw = (self.tenant.get("workload_target_selection", {}) or {}).get(self.workload_type, {}) or {}
        mode = str(raw.get("mode") or "all").strip().lower()
        selected_ids = []
        for item in raw.get("selected_ids", []) or []:
            value = str(item or "").strip()
            if value:
                selected_ids.append(value)
        if mode != "selected" or not selected_ids:
            mode = "all"
            selected_ids = []
        return {
            "mode": mode,
            "selected_ids": selected_ids,
            "selected_count": len(selected_ids),
        }

    def apply_target_selection(self, targets):
        targets = list(targets or [])
        selection = self.get_target_selection()
        if selection["mode"] != "selected":
            return targets, {
                "mode": "all",
                "available_count": len(targets),
                "selected_count": 0,
                "effective_count": len(targets),
            }

        selected_ids = set(selection["selected_ids"])
        filtered = [
            item for item in targets
            if str(item.get("id") or "").strip() in selected_ids
        ]
        return filtered, {
            "mode": "selected",
            "available_count": len(targets),
            "selected_count": len(selected_ids),
            "effective_count": len(filtered),
        }

    def _download(self, url, dest, size_hint=0):
        if os.path.exists(dest) and size_hint > 0 and os.path.getsize(dest) == size_hint:
            return size_hint
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".tmp"
        last_error = None
        for attempt in range(5):
            response = None
            resume_from = 0
            try:
                headers = self._headers()
                if os.path.exists(tmp):
                    resume_from = os.path.getsize(tmp)
                    if size_hint and resume_from >= size_hint:
                        os.replace(tmp, dest)
                        return size_hint
                    if resume_from > 0:
                        headers["Range"] = f"bytes={resume_from}-"

                response = self.session.get(url, headers=headers, stream=True, timeout=(20, 300))
                if is_retryable_status(response.status_code) and attempt < 4:
                    response.close()
                    time.sleep(compute_backoff_delay(attempt, response=response))
                    continue
                response.raise_for_status()

                bytes_written = resume_from
                mode = "ab" if resume_from else "wb"
                if resume_from and response.status_code == 200:
                    mode = "wb"
                    bytes_written = 0
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                with open(tmp, mode) as handle:
                    for chunk in response.iter_content(chunk_size=65536):
                        if chunk:
                            handle.write(chunk)
                            bytes_written += len(chunk)
                os.replace(tmp, dest)
                return bytes_written
            except Exception as e:
                last_error = e
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        pass
                if not is_retryable_exception(e) or attempt == 4:
                    raise
                time.sleep(compute_backoff_delay(attempt))
        if last_error:
            raise last_error
        raise RuntimeError(f"Download failed for {dest}")
