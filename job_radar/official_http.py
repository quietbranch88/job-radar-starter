"""HTTPS JSON transport for documented official job-board APIs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timezone
from email.utils import parsedate_to_datetime
import gzip
import json
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


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


class _BoundedReader:
    """Cap how much of a redirect body is consumed before the next request."""

    def __init__(self, raw: Any, limit: int) -> None:
        self._raw = raw
        self._left = limit

    def read(self, size: int = -1) -> bytes:
        if self._left <= 0:
            return b""
        if size is None or size < 0 or size > self._left:
            size = self._left
        chunk = self._raw.read(size)
        if not isinstance(chunk, (bytes, bytearray)):
            chunk = bytes(chunk)
        self._left -= len(chunk)
        return bytes(chunk)

    def close(self) -> None:
        close = getattr(self._raw, "close", None)
        if close is not None:
            close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)


class _HttpsOnlyRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str):
        target = urlsplit(newurl)
        if target.scheme != "https" or not target.hostname:
            raise HTTPError(
                getattr(req, "full_url", newurl),
                code,
                "official source redirect must use HTTPS",
                headers,
                fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)

    def http_error_302(self, req: Request, fp: Any, code: int, msg: str, headers: Any):
        bounded = _BoundedReader(fp, MAX_RESPONSE_BYTES + 1)
        return HTTPRedirectHandler.http_error_302(self, req, bounded, code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


_OPENER = build_opener(_HttpsOnlyRedirectHandler)


def urlopen(request: Request, timeout: int = 30):
    """Open one official request. Tests replace this name."""

    return _OPENER.open(request, timeout=timeout)


def _read_body(response: Any) -> bytes:
    headers = getattr(response, "headers", None)
    encoding = ""
    if headers is not None and hasattr(headers, "get"):
        encoding = str(headers.get("Content-Encoding") or "")
    if "gzip" in encoding.casefold():
        with gzip.GzipFile(fileobj=response) as decoder:
            return decoder.read(MAX_RESPONSE_BYTES + 1)
    return response.read(MAX_RESPONSE_BYTES + 1)


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
            "Accept-Encoding": "gzip",
            "User-Agent": "job-radar-starter/0.1 (local single-user client)",
        },
    )
    attempt = 0
    while True:
        attempt += 1
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = _read_body(response)
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
