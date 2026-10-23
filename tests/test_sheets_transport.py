from __future__ import annotations

import io
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from job_radar.sheets_transport import OAuthSheetsTransport, _DenyRedirect


class Response(io.BytesIO):
    status = 200


def transport(monkeypatch, open_fn):
    monkeypatch.setenv('JOB_RADAR_SHEETS_ACCESS_TOKEN', 'synthetic-token-only')
    item = OAuthSheetsTransport()
    item._opener = SimpleNamespace(open=open_fn)
    return item


def test_oauth_is_header_only_and_response_is_bounded(monkeypatch):
    calls = []

    def open_request(request, *, timeout):
        calls.append((request, timeout))
        return Response(b'{"values":[]}')

    item = transport(monkeypatch, open_request)
    assert item('GET', 'https://sheets.googleapis.com/v4/spreadsheets/synthetic', timeout=7) == {'values': []}
    request, timeout = calls[0]
    assert timeout == 7
    assert request.get_header('Authorization') == 'Bearer synthetic-token-only'
    assert 'synthetic-token-only' not in request.full_url and request.data is None


@pytest.mark.parametrize('raw', [b'[]', b'not json', b'{"error":"private body"}', b'x' * 50])
def test_unsafe_response_has_fixed_safe_error(monkeypatch, raw):
    item = transport(monkeypatch, lambda *a, **k: Response(raw))
    with pytest.raises(ValueError, match='^sheets request failed$'):
        item('GET', 'https://sheets.googleapis.com/v4/spreadsheets/synthetic', max_response_bytes=20)


def test_http_error_and_redirect_never_disclose_body_or_forward_credentials(monkeypatch):
    def unavailable(*args, **kwargs):
        raise HTTPError('https://sheets.googleapis.com', 403, 'private body', {}, None)

    item = transport(monkeypatch, unavailable)
    with pytest.raises(ValueError, match='^sheets request failed$'):
        item('GET', 'https://sheets.googleapis.com/v4/spreadsheets/synthetic')
    with pytest.raises(ValueError, match='^sheets request failed$'):
        _DenyRedirect().redirect_request(None, None, 302, 'private', {}, 'https://elsewhere.test')


@pytest.mark.parametrize('url', ['http://sheets.googleapis.com/v4/spreadsheets/a',
    'https://sheets.googleapis.com.evil.test/v4/spreadsheets/a',
    'https://sheets.googleapis.com:443/v4/spreadsheets/a',
    'https://user@sheets.googleapis.com/v4/spreadsheets/a',
    'https://sheets.googleapis.com/v4/other/a',
    'https://sheets.googleapis.com/v4/spreadsheets/a#private'])
def test_wrong_boundary_never_reaches_network(monkeypatch, url):
    calls = []
    item = transport(monkeypatch, lambda *a, **k: calls.append(a))
    with pytest.raises(ValueError, match='^invalid Sheets transport request$'):
        item('GET', url)
    assert calls == []


@pytest.mark.parametrize('options', [{'timeout': 0}, {'timeout': 11}, {'timeout': float('nan')},
    {'max_response_bytes': 4194305}, {'max_response_bytes': True}])
def test_invalid_bounds_fail_before_network(monkeypatch, options):
    calls = []
    item = transport(monkeypatch, lambda *a, **k: calls.append(a))
    with pytest.raises(ValueError):
        item('GET', 'https://sheets.googleapis.com/v4/spreadsheets/a', **options)
    assert calls == []


def test_missing_credentials_do_not_attempt_login_or_network(monkeypatch):
    monkeypatch.delenv('JOB_RADAR_SHEETS_ACCESS_TOKEN', raising=False)
    with pytest.raises(ValueError, match='unavailable'):
        OAuthSheetsTransport()
