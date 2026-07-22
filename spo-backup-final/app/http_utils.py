"""HTTP resilience helpers for long-running Microsoft 365 operations."""
from __future__ import annotations

import random
import time
from email.utils import parsedate_to_datetime
from typing import Iterable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
RETRYABLE_EXCEPTIONS = (
    requests.Timeout,
    requests.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ContentDecodingError,
)


def build_retry_session(
    total: int = 5,
    connect: int = 5,
    read: int = 5,
    status: int = 5,
    backoff_factor: float = 1.0,
    pool_connections: int = 20,
    pool_maxsize: int = 20,
    allowed_methods: Iterable[str] | None = None,
) -> requests.Session:
    retry = Retry(
        total=total,
        connect=connect,
        read=read,
        status=status,
        backoff_factor=backoff_factor,
        allowed_methods=frozenset(allowed_methods or {"GET", "HEAD", "OPTIONS"}),
        status_forcelist=RETRYABLE_STATUS_CODES,
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=pool_connections,
        pool_maxsize=pool_maxsize,
    )
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def is_retryable_status(status_code: int | None) -> bool:
    return int(status_code or 0) in RETRYABLE_STATUS_CODES


def is_retryable_exception(exc: Exception) -> bool:
    return isinstance(exc, RETRYABLE_EXCEPTIONS)


def parse_retry_after(response: requests.Response | None, default_seconds: float = 5.0) -> float:
    if response is None:
        return default_seconds
    raw = response.headers.get("Retry-After")
    if not raw:
        return default_seconds
    raw = raw.strip()
    if raw.isdigit():
        return float(raw)
    try:
        dt = parsedate_to_datetime(raw)
        return max(0.0, dt.timestamp() - time.time())
    except Exception:
        return default_seconds


def compute_backoff_delay(attempt: int, response: requests.Response | None = None, cap_seconds: float = 60.0) -> float:
    if response is not None and response.status_code == 429:
        return min(cap_seconds, max(1.0, parse_retry_after(response, default_seconds=5.0)))
    base = min(cap_seconds, float(2 ** max(attempt, 0)))
    jitter = min(cap_seconds * 0.1, random.random())
    return min(cap_seconds, base + jitter)
