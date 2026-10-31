"""HTTPS JSON transport for documented official job-board APIs."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


MAX_RESPONSE_BYTES = 50 * 1024 * 1024


def http_get_json(url: str, *, timeout: int = 30) -> Any:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("official source URL must use HTTPS")
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "job-radar-starter/0.1 (local single-user client)",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("official source response exceeded 50 MiB")
    return json.loads(raw.decode("utf-8"))
