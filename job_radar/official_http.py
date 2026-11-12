"""HTTPS JSON transport for documented official job-board APIs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timezone
from email.utils import parsedate_to_datetime
import json
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


MAX_RESPONSE_BYTES = 50 * 1024 * 1024
MAX_RATE_LIMIT_ATTEMPTS = 3
MAX_RETRY_WAIT_SECONDS = 5.0
_MISSING_RETRY_WAIT_SECONDS = 1.0


def _retry_wait_seconds(
    headers: Any,
    now: Callable[[], float],
    *,
    max_wait: float,
) -> float | None:
    """Return a bounded wait, or None when the server asks us not to retry yet."""

    raw = headers.get("Retry-After") if headers is not None and hasattr(headers, "get") else None
    if raw is None:
        wait = _MISSING_RETRY_WAIT_SECONDS
    else:
        text = str(raw).strip()
        if text.isdigit():
            wait = float(text)
        else:
            try:
                moment = parsedate_to_datetime(text)
            except (TypeError, ValueError, OverflowError):
                return None
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            wait = moment.timestamp() - now()
    if wait < 0:
        wait = 0.0
    if wait > max_wait:
        return None
    return wait


def http_get_json(
    url: str,
    *,
    timeout: int = 30,
    sleeper: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    max_attempts: int = MAX_RATE_LIMIT_ATTEMPTS,
    max_retry_wait: float = MAX_RETRY_WAIT_SECONDS,
) -> Any:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("official source URL must use HTTPS")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    if max_retry_wait < 0:
        raise ValueError("max_retry_wait must be zero or positive")
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "job-radar-starter/0.1 (local single-user client)",
        },
    )
    attempt = 0
    while True:
        attempt += 1
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            break
        except HTTPError as error:
            status = error.code
            headers = error.headers
            try:
                error.close()
            except Exception:
                pass
            if status != 429 or attempt >= max_attempts:
                raise
            wait = _retry_wait_seconds(headers, now, max_wait=max_retry_wait)
            if wait is None:
                raise
            if wait:
                sleeper(wait)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("official source response exceeded 50 MiB")
    return json.loads(raw.decode("utf-8"))
