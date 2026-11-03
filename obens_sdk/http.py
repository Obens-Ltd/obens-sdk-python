from __future__ import annotations

import time
from typing import Any, Optional
from urllib.parse import quote

import requests

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class ObensApiError(Exception):
    def __init__(self, status: int, body: Any, method: str, path: str):
        super().__init__(f"{method} {path} failed with {status}" + (f": {body}" if body else ""))
        self.status = status
        self.body = body


class Http:
    """requests wrapper: api key header, JSON, timeout and retries with backoff on transient failures."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 30.0,
        max_retries: int = 3,
        retry_delay: float = 0.5,
        session: Optional[requests.Session] = None,
        account_id: Optional[str] = None,
    ):
        if not base_url:
            raise ValueError("base_url is required")
        if not api_key:
            raise ValueError("api_key is required")
        self.base_url = base_url.rstrip("/")
        self.account_id = account_id
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.session = session or requests.Session()
        self.session.headers.update({"X-API-KEY": api_key, "Accept": "application/json"})

    def request(self, method: str, path: str, body: Any = None) -> Any:
        # a delegated account to act on, instead of the key's own
        if self.account_id:
            path += ("&" if "?" in path else "?") + "account-id=" + quote(self.account_id, safe="")
        attempt = 0
        while True:
            try:
                response = self.session.request(
                    method, self.base_url + path, json=body, timeout=self.timeout
                )
            except requests.RequestException:
                # network error or timeout: safe to repeat because every call is an upsert
                if attempt >= self.max_retries:
                    raise
                time.sleep(self._delay(attempt))
                attempt += 1
                continue
            if response.ok:
                return response.json() if response.text else None
            if response.status_code in RETRYABLE_STATUS and attempt < self.max_retries:
                time.sleep(self._delay(attempt, response.headers.get("Retry-After")))
                attempt += 1
                continue
            try:
                parsed = response.json()
            except ValueError:
                parsed = response.text
            raise ObensApiError(response.status_code, parsed, method, path)

    def _delay(self, attempt: int, retry_after: Optional[str] = None) -> float:
        try:
            seconds = float(retry_after) if retry_after is not None else 0.0
        except ValueError:
            seconds = 0.0
        return seconds if seconds > 0 else self.retry_delay * (2 ** attempt)
