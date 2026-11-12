from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytest
from urllib.error import HTTPError

from job_radar.official_http import http_get_json


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
