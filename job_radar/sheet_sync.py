"""Opt-in, bounded Sheets delivery for one local writer and one dedicated tab.

Items are full snapshots of the owned fields, not patches. Omitted fields are
empty cells. State retains payloads and delivery receipts until explicit local
export/deletion; it never holds credentials. Deleting local state does not delete
Google's copy. Acknowledgement requires readback. This is not distributed CAS:
other hosts, users and automations must not write to the owned tab concurrently.

The injected transport owns authentication, HTTP status checking, redirect denial,
response byte limits and timeout enforcement. No network transport is installed or
called implicitly. A fresh delivery uses at most seven HTTP calls per item.
"""

from __future__ import annotations

import hashlib
import json as json_module
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.parse import quote, urlencode, urlsplit

from job_radar.local_state import read_state, write_state, writer_guard


FIELD_LIMITS = {
    "company": 200,
    "title": 300,
    "url": 2048,
    "status": 32,
    "applied_at": 40,
    "resume_version": 128,
    "confirmation_evidence": 1000,
}
HEADERS = ("job_id", "payload_hash", *FIELD_LIMITS)
MAX_ITEMS = 500
MAX_DELIVERY_ITEMS = 20
MAX_ATTEMPTS = 3
MAX_REMOTE_ROWS = 1000
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_STATUSES = {
    "interested", "applied", "interview", "offer", "accepted", "rejected",
    "withdrawn", "skip", "dead",
}
_ERRORS = {
    "remote_unavailable", "remote_schema", "remote_formula", "remote_version",
    "remote_conflict", "duplicate_remote_id", "wrong_headers", "wrong_tab",
    "remote_capacity", "readback_mismatch", "retry_exhausted", "target_mismatch",
}
_SECRET = re.compile(
    r"(?i)(?:\bbearer\s|\b(?:access[_-]?token|refresh[_-]?token|api[_-]?key|"
    r"password|authorization|secret|token)\b\s*[:=]|"
    r"\b(?:gh[pousr]_|github_pat_|sk-|ya29\.|AIza)[A-Za-z0-9_-]{8,}|"
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.)"
)


class SheetSyncError(ValueError):
    """A fixed safe code; never a transport exception body or user value."""

    def __init__(self, code: str):
        self.code = code if code in _ERRORS | {
            "invalid_payload", "invalid_target", "invalid_job_id", "invalid_hash",
            "invalid_outbox", "local_state_failed", "outbox_full", "pending_version",
            "invalid_limit",
        } else "remote_unavailable"
        super().__init__(self.code)


def _text(value: object, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise SheetSyncError("invalid_payload")
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise SheetSyncError("invalid_payload")
    if value != value.strip() or value.lstrip(" '").startswith(("=", "+", "-", "@")):
        raise SheetSyncError("invalid_payload")
    if _SECRET.search(value):
        raise SheetSyncError("invalid_payload")
    return value


def _job_id(value: object) -> str:
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value)
            or _SECRET.search(value)):
        raise SheetSyncError("invalid_job_id")
    return value


def _target(value: object) -> str:
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.'%:-]{0,399}", value)
            or _SECRET.search(value)):
        raise SheetSyncError("invalid_target")
    return value


def _hash(value: object, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise SheetSyncError("invalid_hash")
    return value


def _fields(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or not value or set(value) - FIELD_LIMITS.keys():
        raise SheetSyncError("invalid_payload")
    result = {key: _text(item, FIELD_LIMITS[key]) for key, item in value.items()}
    if "status" in result and result["status"] not in _STATUSES:
        raise SheetSyncError("invalid_payload")
    if "url" in result:
        try:
            url = urlsplit(result["url"])
            if (url.scheme not in {"http", "https"} or not url.hostname
                    or url.username is not None or url.password is not None or url.query or url.fragment):
                raise ValueError
            _ = url.port
        except ValueError:
            raise SheetSyncError("invalid_payload") from None
    if "resume_version" in result and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", result["resume_version"]):
        raise SheetSyncError("invalid_payload")
    evidence = {"applied_at", "resume_version", "confirmation_evidence"}
    if evidence & result.keys() or result.get("status") in {"applied", "interview", "offer", "accepted"}:
        if not evidence <= result.keys():
            raise SheetSyncError("invalid_payload")
        applied_at = result["applied_at"]
        if applied_at != "unknown":
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", applied_at):
                raise SheetSyncError("invalid_payload")
            try:
                datetime.fromisoformat(applied_at.replace("Z", "+00:00"))
            except ValueError:
                raise SheetSyncError("invalid_payload") from None
    return dict(sorted(result.items()))


def _digest(value: object) -> str:
    raw = json_module.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _payload_hash(target: str, job_id: str, fields: Mapping[str, str]) -> str:
    return _digest({"target": target, "job_id": job_id, "fields": fields})


def _operation_id(target: str, job_id: str, payload_hash: str, expected: str | None, generation: int = 0) -> str:
    identity = {"target": target, "job_id": job_id, "payload_hash": payload_hash,
                "expected_remote_hash": expected}
    if generation:
        identity['generation'] = generation
    return _digest(identity)


@dataclass(frozen=True)
class SheetRow:
    job_id: str
    payload_hash: str
    fields: Mapping[str, str]


class SheetAdapter(Protocol):
    target: str

    def read_row(self, target: str, job_id: str) -> SheetRow | None:
        """Read by key; reject duplicate IDs, formulas and unknown row versions."""
        ...

    def write_row(self, target: str, row: SheetRow, expected_remote_hash: str | None = None) -> None:
        """Recheck the current row/precondition, then write only owned cells."""
        ...


def _checked_row(row: SheetRow | None, target: str, job_id: str) -> SheetRow | None:
    if row is None:
        return None
    try:
        if not isinstance(row, SheetRow) or row.job_id != job_id:
            raise SheetSyncError("remote_schema")
        fields = _fields(row.fields)
        if _hash(row.payload_hash) != _payload_hash(target, job_id, fields):
            raise SheetSyncError("remote_version")
        return SheetRow(job_id, row.payload_hash, fields)
    except SheetSyncError as error:
        raise SheetSyncError("remote_version" if error.code == "remote_version" else "remote_schema") from None


def _precondition(current: SheetRow | None, row: SheetRow, expected: str | None) -> bool:
    if current is not None and current.payload_hash == row.payload_hash:
        return False
    if (current is None and expected is not None
            or current is not None and (expected is None or current.payload_hash != expected)):
        raise SheetSyncError("remote_conflict")
    return True


class SheetOutbox:
    """Bounded durable receipts; enqueue never performs a remote operation.

    Only one outstanding version per (target, job_id) is permitted. For a changed
    snapshot, explicitly pass the acknowledged prior payload_hash as the expected
    remote version. A blocked item halts that target until operator reconciliation;
    there is no automatic overwrite, dropping, retry loop or receipt pruning.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.guard_path = self.path.with_name(self.path.name + ".lock")

    def _load(self) -> dict[str, Any]:
        try:
            document = read_state(self.path)
        except FileNotFoundError:
            return {"contract_version": 1, "items": []}
        except (ValueError, OSError, UnicodeError):
            raise SheetSyncError("invalid_outbox") from None
        try:
            if set(document) != {"contract_version", "items"} or type(document["contract_version"]) is not int or document["contract_version"] != 1:
                raise ValueError
            items = document["items"]
            if not isinstance(items, list) or len(items) > MAX_ITEMS:
                raise ValueError
            identities: set[str] = set()
            outstanding: set[tuple[str, str]] = set()
            generations: dict[tuple, int] = {}
            for item in items:
                if not isinstance(item, dict) or set(item) - {'generation'} != {
                    "operation_id", "target", "job_id", "fields", "payload_hash",
                    "expected_remote_hash", "state", "attempts", "error",
                }:
                    raise ValueError
                target, job_id, fields = _target(item["target"]), _job_id(item["job_id"]), _fields(item["fields"])
                payload_hash = _payload_hash(target, job_id, fields)
                expected = _hash(item["expected_remote_hash"], optional=True)
                transition = (target, job_id, payload_hash, expected)
                generation = item.get('generation', 0)
                if type(generation) is not int or generation != generations.get(transition, 0):
                    raise ValueError
                generations[transition] = generation + 1
                operation_id = _operation_id(target, job_id, payload_hash, expected, generation)
                if (item["payload_hash"] != payload_hash or item["operation_id"] != operation_id
                        or operation_id in identities or item["state"] not in {"pending", "delivered", "blocked"}
                        or type(item["attempts"]) is not int or not 0 <= item["attempts"] <= MAX_ATTEMPTS
                        or item["error"] is not None and item["error"] not in _ERRORS):
                    raise ValueError
                if item["state"] == "blocked" and item["error"] is None:
                    raise ValueError
                if item["state"] == "delivered" and (item["error"] is not None or item["attempts"] == 0):
                    raise ValueError
                identities.add(operation_id)
                if item["state"] != "delivered":
                    if (target, job_id) in outstanding:
                        raise ValueError
                    outstanding.add((target, job_id))
        except (ValueError, TypeError, KeyError):
            raise SheetSyncError("invalid_outbox") from None
        return document

    def _save(self, document: dict[str, Any]) -> None:
        try:
            write_state(self.path, document)
        except (OSError, ValueError):
            raise SheetSyncError("local_state_failed") from None

    def enqueue(self, job_id: str, fields: Mapping[str, str], target: str,
                expected_remote_hash: str | None = None) -> str:
        job_id, target, fields = _job_id(job_id), _target(target), _fields(fields)
        expected = _hash(expected_remote_hash, optional=True)
        payload_hash = _payload_hash(target, job_id, fields)
        with writer_guard(self.guard_path):
            document = self._load()
            related = [item for item in document['items'] if item['target'] == target and item['job_id'] == job_id]
            if related and related[-1]['payload_hash'] == payload_hash and related[-1]['expected_remote_hash'] == expected:
                return related[-1]['operation_id']
            if any(item['state'] != 'delivered' for item in related):
                raise SheetSyncError("pending_version")
            if len(document["items"]) >= MAX_ITEMS:
                raise SheetSyncError("outbox_full")
            generation = sum(item['payload_hash'] == payload_hash and item['expected_remote_hash'] == expected for item in related)
            operation_id = _operation_id(target, job_id, payload_hash, expected, generation)
            item = {
                "operation_id": operation_id, "target": target, "job_id": job_id,
                "fields": fields, "payload_hash": payload_hash, "expected_remote_hash": expected,
                "state": "pending", "attempts": 0, "error": None,
            }
            if generation:
                item['generation'] = generation
            document["items"].append(item)
            self._save(document)
        return operation_id

    def status(self) -> dict[str, Any]:
        items = self._load()["items"]
        return {"contract_version": 1, "total": len(items),
                **{state: sum(item["state"] == state for item in items)
                   for state in ("pending", "delivered", "blocked")},
                "items": [{key: value for key, value in item.items() if key != "fields"} for item in items]}

    def deliver(self, adapter: SheetAdapter, target: str, max_items: int = 10) -> dict[str, Any]:
        target = _target(target)
        if type(max_items) is not int or not 1 <= max_items <= MAX_DELIVERY_ITEMS:
            raise SheetSyncError("invalid_limit")
        if adapter.target != target:
            raise SheetSyncError("target_mismatch")
        attempted = delivered = 0
        error_code = None
        with writer_guard(self.guard_path):
            document = self._load()
            items = [item for item in document["items"] if item["target"] == target]
            for item in items:
                if item["state"] == "delivered":
                    continue
                if item["state"] == "blocked":
                    error_code = item["error"]
                    break
                if attempted >= max_items:
                    break
                if item["attempts"] >= MAX_ATTEMPTS:
                    item.update(state="blocked", error="retry_exhausted")
                    self._save(document)
                    error_code = "retry_exhausted"
                    break
                item["attempts"] += 1
                self._save(document)  # Intent and attempt are durable before any remote request.
                attempted += 1
                row = SheetRow(item["job_id"], item["payload_hash"], dict(item["fields"]))
                try:
                    current = _checked_row(adapter.read_row(target, row.job_id), target, row.job_id)
                    if _precondition(current, row, item["expected_remote_hash"]):
                        adapter.write_row(target, row, item["expected_remote_hash"])
                        observed = _checked_row(adapter.read_row(target, row.job_id), target, row.job_id)
                        if observed is None or observed.payload_hash != row.payload_hash or observed.fields != row.fields:
                            raise SheetSyncError("readback_mismatch")
                except Exception as error:
                    error_code = error.code if isinstance(error, SheetSyncError) and error.code in _ERRORS else "remote_unavailable"
                    item["error"] = error_code
                    item["state"] = "pending" if error_code == "remote_unavailable" else "blocked"
                    if item["state"] == "pending" and item["attempts"] >= MAX_ATTEMPTS:
                        item.update(state="blocked", error="retry_exhausted")
                        error_code = "retry_exhausted"
                    self._save(document)
                    break
                item.update(state="delivered", error=None)
                self._save(document)
                delivered += 1
            return {"target": target, "attempted": attempted, "delivered": delivered,
                    "pending": sum(item["state"] == "pending" for item in items),
                    "blocked": sum(item["state"] == "blocked" for item in items), "error": error_code}


class RequestTransport(Protocol):
    def __call__(self, method: str, url: str, *, json: dict[str, Any] | None = None,
                 timeout: float = 10.0, max_response_bytes: int = MAX_RESPONSE_BYTES) -> dict[str, Any]:
        """Return parsed 2xx JSON; enforce bounds, deny redirects and raise on errors."""
        ...


class GoogleSheetsAdapter:
    """Google Sheets v4 on a precreated owned tab with exact HEADERS.

    The grid must have exactly nine columns and 2..1001 rows. Metadata is checked
    on every read so the bounded scan covers the entire tab, including duplicate
    keys. Headers, formulas, unrelated rows and oversized grids are never repaired
    automatically. read_row + write_row + read_row costs at most seven requests.
    """

    def __init__(self, spreadsheet_id: str, tab_name: str, transport: RequestTransport):
        if (not isinstance(spreadsheet_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", spreadsheet_id)
                or _SECRET.search(spreadsheet_id)):
            raise SheetSyncError("invalid_target")
        try:
            tab_name = _text(tab_name, 100)
            if any(char in tab_name for char in "[]:*?/\\"):
                raise ValueError
        except ValueError:
            raise SheetSyncError("invalid_target") from None
        self.target = _target(f"sheets:{spreadsheet_id}:{quote(tab_name, safe='')}")
        self.tab_name = tab_name
        self.transport = transport
        self._base = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"

    def _request(self, method: str, url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = self.transport(method, url, json=body, timeout=10.0, max_response_bytes=MAX_RESPONSE_BYTES)
        except Exception:
            raise SheetSyncError("remote_unavailable") from None
        if not isinstance(response, dict) or "error" in response:
            raise SheetSyncError("remote_unavailable")
        return response

    def _check_target(self, target: str) -> None:
        if target != self.target:
            raise SheetSyncError("target_mismatch")

    def _range(self, rows: str) -> str:
        return f"'{self.tab_name.replace(chr(39), chr(39) * 2)}'!{rows}"

    def _snapshot(self) -> tuple[dict[str, tuple[int, SheetRow]], int | None]:
        query = urlencode({"includeGridData": "false", "fields": "sheets(properties(title,gridProperties(rowCount,columnCount)))"})
        metadata = self._request("GET", f"{self._base}?{query}")
        sheets = metadata.get("sheets")
        if not isinstance(sheets, list):
            raise SheetSyncError("remote_schema")
        candidates = [sheet.get("properties") for sheet in sheets
                      if isinstance(sheet, dict) and isinstance(sheet.get("properties"), dict)
                      and sheet["properties"].get("title") == self.tab_name]
        if len(candidates) != 1:
            raise SheetSyncError("wrong_tab")
        grid = candidates[0].get("gridProperties")
        if not isinstance(grid, dict):
            raise SheetSyncError("remote_schema")
        rows, columns = grid.get("rowCount"), grid.get("columnCount")
        if type(rows) is not int or not 2 <= rows <= MAX_REMOTE_ROWS + 1 or type(columns) is not int or columns != len(HEADERS):
            raise SheetSyncError("remote_capacity")
        cell_range = quote(self._range(f"A1:I{rows}"), safe="")
        response = self._request("GET", f"{self._base}/values/{cell_range}?valueRenderOption=FORMULA&majorDimension=ROWS")
        values = response.get("values", [])
        if not isinstance(values, list) or len(values) > rows:
            raise SheetSyncError("remote_schema")
        if not values or values[0] != list(HEADERS):
            raise SheetSyncError("wrong_headers")
        found: dict[str, tuple[int, SheetRow]] = {}
        free = None
        for index in range(1, rows):
            cells = values[index] if index < len(values) else []
            if not isinstance(cells, list) or len(cells) > len(HEADERS) or any(not isinstance(cell, str) for cell in cells):
                raise SheetSyncError("remote_schema")
            if any(cell.lstrip(" '").startswith(("=", "+", "-", "@")) for cell in cells):
                raise SheetSyncError("remote_formula")
            if not any(cells):
                if free is None:
                    free = index + 1
                continue
            padded = cells + [""] * (len(HEADERS) - len(cells))
            job_id = padded[0]
            try:
                _job_id(job_id)
            except SheetSyncError:
                raise SheetSyncError("remote_schema") from None
            if job_id in found:
                raise SheetSyncError("duplicate_remote_id")
            fields = {key: cell for key, cell in zip(FIELD_LIMITS, padded[2:]) if cell}
            row = _checked_row(SheetRow(job_id, padded[1], fields), self.target, job_id)
            found[job_id] = (index + 1, row)
        return found, free

    def read_row(self, target: str, job_id: str) -> SheetRow | None:
        self._check_target(target)
        _job_id(job_id)
        found, _ = self._snapshot()
        return found[job_id][1] if job_id in found else None

    def write_row(self, target: str, row: SheetRow, expected_remote_hash: str | None = None) -> None:
        self._check_target(target)
        row = _checked_row(row, target, _job_id(row.job_id))
        expected = _hash(expected_remote_hash, optional=True)
        found, free = self._snapshot()
        existing = found.get(row.job_id)
        if not _precondition(existing[1] if existing else None, row, expected):
            return
        position = existing[0] if existing else free
        if position is None:
            raise SheetSyncError("remote_capacity")
        cell_range = self._range(f"A{position}:I{position}")
        self._request("PUT", f"{self._base}/values/{quote(cell_range, safe='')}?valueInputOption=RAW", {
            "range": cell_range, "majorDimension": "ROWS",
            "values": [[row.job_id, row.payload_hash, *(row.fields.get(field, "") for field in FIELD_LIMITS)]],
        })
