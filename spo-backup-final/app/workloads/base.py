"""Base workload utilities for Microsoft Graph access."""
import os
import time

import msal
import requests


class BaseWorkload:
    GRAPH = "https://graph.microsoft.com/v1.0"
    workload_type = "base"

    def __init__(self, tenant_config):
        self.tenant = tenant_config
        self.tenant_id = tenant_config["tenant_id"]
        self.client_id = tenant_config["client_id"]
        self.client_secret = tenant_config["client_secret"]
        self.session = requests.Session()
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
        for attempt in range(max_retry):
            response = self.session.get(url, headers=self._headers(), params=params, timeout=60)
            if response.status_code == 429 and attempt < max_retry - 1:
                time.sleep(int(response.headers.get("Retry-After", 30)))
                continue
            response.raise_for_status()
            return response.json()
        return {}

    def _paginate(self, url, params=None):
        while url:
            data = self._get(url, params)
            for item in data.get("value", []):
                yield item
            url = data.get("@odata.nextLink")
            params = None

    def _download(self, url, dest, size_hint=0):
        if os.path.exists(dest) and size_hint > 0 and os.path.getsize(dest) == size_hint:
            return size_hint
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        response = self.session.get(url, headers=self._headers(), stream=True, timeout=300)
        response.raise_for_status()
        tmp = dest + ".tmp"
        bytes_written = 0
        with open(tmp, "wb") as handle:
            for chunk in response.iter_content(chunk_size=65536):
                if chunk:
                    handle.write(chunk)
                    bytes_written += len(chunk)
        os.replace(tmp, dest)
        return bytes_written
