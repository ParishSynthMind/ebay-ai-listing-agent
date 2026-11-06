"""One place that sends eBay API requests and retries the ones worth retrying.

eBay says "slow down" with HTTP 429 and has the odd 5xx (errorId 25001 "system
error"). Those are retried with exponential backoff plus jitter, honouring
Retry-After when eBay sends it. A 401 means the access token ran out early:
the token is refreshed once and the call repeated. Every other error is raised
straight away as an EbayApiError carrying eBay's own error list, so the caller
can tell a missing item specific (25002) from a bad policy id.
"""
from __future__ import annotations

import random
import time
from typing import Callable, Optional


class EbayApiError(RuntimeError):
    def __init__(self, status: int, errors: list[dict], url: str = "", method: str = ""):
        self.status = status
        self.errors = errors or [{"errorId": 0, "message": "no error body"}]
        self.url = url
        self.method = method
        first = self.errors[0]
        msg, long = str(first.get("message") or ""), str(first.get("longMessage") or "")
        text = f"{msg} ({long})" if long and long != msg else msg
        super().__init__(f"eBay {status} error {first.get('errorId')}: {text}")

    @property
    def error_ids(self) -> list[int]:
        return [e.get("errorId") for e in self.errors]

    def messages(self) -> list[str]:
        return [str(e.get("longMessage") or e.get("message") or "") for e in self.errors]


def parse_errors(resp) -> list[dict]:
    """eBay's REST error list; tolerant of the OAuth style ({"error": ..., "error_description": ...}) and non-JSON."""
    try:
        body = resp.json()
    except Exception:
        return [{"errorId": 0, "message": (getattr(resp, "text", "") or "")[:200]}]
    if isinstance(body, dict) and isinstance(body.get("errors"), list):
        return body["errors"]
    if isinstance(body, dict) and "error" in body:
        return [{"errorId": 0, "message": str(body.get("error")), "longMessage": str(body.get("error_description", ""))}]
    return [{"errorId": 0, "message": str(body)[:200]}]


def should_retry(resp) -> bool:
    return resp.status_code == 429 or resp.status_code >= 500


class EbayHttp:
    """Wraps any session with a requests-style .request(): requests.Session or the mock.

    token: a callable returning the bearer token for this call; refresh: called once on 401.
    """

    def __init__(self, session, token: Optional[Callable[[], str]] = None, refresh: Optional[Callable[[], None]] = None,
                 max_retries: int = 5, base_delay: float = 1.0, max_delay: float = 30.0,
                 sleep: Callable[[float], None] = time.sleep, timeout: float = 60.0, rng: Optional[random.Random] = None):
        self.session = session
        self.token = token
        self.refresh = refresh
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.sleep = sleep
        self.timeout = timeout
        self.rng = rng or random.Random()
        self.retries = 0
        self.calls = 0

    def request(self, method: str, url: str, headers: Optional[dict] = None, **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        refreshed = False
        attempt = 0
        while True:
            h = {"Accept": "application/json", **(headers or {})}
            if self.token is not None:
                h["Authorization"] = f"Bearer {self.token()}"
            self.calls += 1
            resp = self.session.request(method, url, headers=h, **kwargs)
            if resp.status_code < 400:
                return resp
            if resp.status_code == 401 and self.refresh is not None and not refreshed:
                refreshed = True
                self.refresh()
                continue
            if attempt < self.max_retries and should_retry(resp):
                self.retries += 1
                self.sleep(self._delay(attempt, resp))
                attempt += 1
                continue
            raise EbayApiError(resp.status_code, parse_errors(resp), url, method)

    def json(self, method: str, url: str, **kwargs):
        resp = self.request(method, url, **kwargs)
        if resp.status_code == 204 or not (getattr(resp, "text", "") or "").strip():
            return {}
        return resp.json()

    def _delay(self, attempt: int, resp) -> float:
        retry_after = (getattr(resp, "headers", None) or {}).get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), self.max_delay)
            except ValueError:
                pass
        return min(self.base_delay * (2 ** attempt), self.max_delay) + self.rng.uniform(0, 1)
