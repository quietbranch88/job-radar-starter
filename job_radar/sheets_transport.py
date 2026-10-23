"""Explicit OAuth-header transport; no credential discovery, refresh or persistence."""

from __future__ import annotations

import json as json_module
import math
import os
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class _DenyRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('sheets request failed')


class OAuthSheetsTransport:
    """Use a caller-provisioned token only after explicit CLI write confirmation.

    Each request is bounded; redirects and non-2xx responses fail safely. Retries
    belong to the durable outbox, never to a hidden HTTP loop. No token refresh,
    Google login, target creation, sharing or credential logging is performed.
    """

    def __init__(self):
        token = os.environ.get('JOB_RADAR_SHEETS_ACCESS_TOKEN', '')
        if not token or len(token) > 4096 or any(ord(char) <= 32 or ord(char) >= 127 for char in token):
            raise ValueError('explicit Sheets OAuth token is unavailable')
        self._token = token
        self._opener = build_opener(_DenyRedirect())

    def __call__(self, method, url, *, json=None, timeout=10.0, max_response_bytes=4194304):
        parsed = urlsplit(url)
        if (method not in {'GET', 'PUT'} or parsed.scheme != 'https'
                or parsed.netloc != 'sheets.googleapis.com'
                or not parsed.path.startswith('/v4/spreadsheets/') or parsed.fragment
                or type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 10
                or type(max_response_bytes) is not int or not 1 <= max_response_bytes <= 4194304):
            raise ValueError('invalid Sheets transport request')
        try:
            body = None if json is None else json_module.dumps(json, ensure_ascii=False).encode('utf8')
            if body is not None and len(body) > 1024 * 1024:
                raise ValueError
            request = Request(url, data=body, method=method, headers={
                'Accept': 'application/json', 'Content-Type': 'application/json',
                'Authorization': 'Bearer ' + self._token,
            })
            with self._opener.open(request, timeout=timeout) as response:
                if not 200 <= response.status < 300:
                    raise ValueError
                raw = response.read(max_response_bytes + 1)
            if len(raw) > max_response_bytes:
                raise ValueError
            result = json_module.loads(raw.decode('utf8'))
            if not isinstance(result, dict) or 'error' in result:
                raise ValueError
            return result
        except Exception:
            raise ValueError('sheets request failed') from None
