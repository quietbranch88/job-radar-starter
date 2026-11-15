from datetime import datetime, timedelta, timezone
from email.message import Message
from io import BytesIO
import gzip

import pytest
from urllib.error import HTTPError
from urllib.request import Request

from job_radar.official_http import _HttpsOnlyRedirectHandler, http_get_json


class _Response:
    def __init__(self, payload: bytes):
        self.payload = payload

    def read(self, _limit: int) -> bytes:
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None


def _http_error(status: int, headers: dict[str, str] | None = None) -> HTTPError:
    return HTTPError(
        "https://boards.example/jobs",
        status,
        "busy",
        headers or {},
        BytesIO(b""),
    )


def test_rate_limit_retries_only_within_the_retry_after_budget(monkeypatch):
    calls = {"count": 0}
    waits: list[float] = []

    def urlopen(_request, timeout):
        assert timeout == 30
        calls["count"] += 1
        if calls["count"] == 1:
            raise _http_error(429, {"Retry-After": "2"})
        return _Response(b'{"jobs": []}')

    monkeypatch.setattr("job_radar.official_http.urlopen", urlopen)

    assert http_get_json("https://boards.example/jobs", sleeper=waits.append) == {"jobs": []}
    assert calls["count"] == 2
    assert waits == [2.0]


def test_long_retry_after_is_not_called_early(monkeypatch):
    def urlopen(_request, timeout):
        raise _http_error(429, {"Retry-After": "120"})

    monkeypatch.setattr("job_radar.official_http.urlopen", urlopen)
    waits: list[float] = []

    with pytest.raises(HTTPError) as caught:
        http_get_json("https://boards.example/jobs", sleeper=waits.append)
    assert caught.value.code == 429
    assert waits == []


def test_missing_retry_after_waits_one_second_and_http_date_is_honored(monkeypatch):
    origin = datetime(2026, 10, 23, tzinfo=timezone.utc)
    scheduled = (origin + timedelta(seconds=4)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    calls = {"count": 0}
    waits: list[float] = []

    def urlopen(_request, timeout):
        calls["count"] += 1
        if calls["count"] == 1:
            raise _http_error(429)
        if calls["count"] == 2:
            raise _http_error(429, {"Retry-After": scheduled})
        return _Response(b'{"ok": true}')

    monkeypatch.setattr("job_radar.official_http.urlopen", urlopen)

    assert http_get_json(
        "https://boards.example/jobs",
        sleeper=waits.append,
        now=lambda: origin.timestamp(),
    ) == {"ok": True}
    assert waits == [1.0, 4.0]


def test_other_http_errors_and_plain_urls_are_not_retried(monkeypatch):
    calls = {"count": 0}

    def urlopen(_request, timeout):
        calls["count"] += 1
        raise _http_error(503)

    monkeypatch.setattr("job_radar.official_http.urlopen", urlopen)

    with pytest.raises(HTTPError) as caught:
        http_get_json("https://boards.example/jobs", sleeper=lambda _seconds: pytest.fail("slept"))
    assert caught.value.code == 503
    assert calls["count"] == 1
    with pytest.raises(ValueError, match="HTTPS"):
        http_get_json("http://boards.example/jobs")


def test_gzip_response_is_decoded_inside_the_body_cap(monkeypatch):
    payload = gzip.compress(b'{"jobs": []}')
    seen = {}

    class _GzipResponse:
        def __init__(self) -> None:
            self._raw = BytesIO(payload)
            self.headers = {"Content-Encoding": "gzip"}

        def read(self, size: int = -1) -> bytes:
            return self._raw.read(size)

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    def urlopen(request, timeout):
        seen["accept"] = request.get_header("Accept-encoding")
        return _GzipResponse()

    monkeypatch.setattr("job_radar.official_http.urlopen", urlopen)
    assert http_get_json("https://boards.example/jobs") == {"jobs": []}
    assert seen["accept"] == "gzip"


def test_redirect_handler_stays_on_https_and_caps_the_body(monkeypatch):
    monkeypatch.setattr("job_radar.official_http.MAX_RESPONSE_BYTES", 8)
    handler = _HttpsOnlyRedirectHandler()
    opened: dict[str, str] = {}

    class _Parent:
        def open(self, request, timeout=None):
            opened["url"] = request.full_url
            return "followed"

    handler.parent = _Parent()
    request = Request("https://boards.example/jobs")
    request.timeout = 30
    body = BytesIO(b"x" * 100)
    read = {"count": 0}
    original = body.read

    def counting(size=-1):
        chunk = original(size)
        read["count"] += len(chunk)
        return chunk

    body.read = counting  # type: ignore[method-assign]
    headers = Message()
    headers["Location"] = "https://boards.example/next"
    assert handler.http_error_302(request, body, 302, "found", headers) == "followed"
    assert opened["url"] == "https://boards.example/next"
    assert read["count"] == 9

    rejected = BytesIO(b"y" * 40)
    rejected_read = {"count": 0}
    original_rejected = rejected.read

    def counting_rejected(size=-1):
        chunk = original_rejected(size)
        rejected_read["count"] += len(chunk)
        return chunk

    rejected.read = counting_rejected  # type: ignore[method-assign]
    headers = Message()
    headers["Location"] = "http://boards.example/next"
    with pytest.raises(HTTPError, match="HTTPS"):
        handler.http_error_302(request, rejected, 302, "found", headers)
    assert rejected_read["count"] == 0
