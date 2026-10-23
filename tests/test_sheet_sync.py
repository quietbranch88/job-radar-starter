from __future__ import annotations

from copy import deepcopy
import json
import re
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from job_radar.local_state import read_state, writer_guard
from job_radar.sheet_sync import (
    GoogleSheetsAdapter, HEADERS, MAX_ATTEMPTS, MAX_DELIVERY_ITEMS, MAX_ITEMS,
    MAX_RESPONSE_BYTES, SheetOutbox, SheetSyncError,
)


JOB = "greenhouse:public-job-1"
FIELDS = {"company": "Example Systems", "title": "Backend Engineer",
          "url": "https://example.test/jobs/1", "status": "interested"}
CONFIRMED = {**FIELDS, "status": "applied", "applied_at": "2026-10-24T09:00:00+08:00",
             "resume_version": "backend-v2", "confirmation_evidence": "Submission receipt checked locally"}


class GoogleShapedTransport:
    """Substitutes HTTP and Google, not production decision or hashing logic."""

    def __init__(self, tab_name="Owned Jobs"):
        self.tab_name = tab_name
        self.values = [list(HEADERS)]
        self.rows = 8
        self.columns = 9
        self.calls = []
        self.writes = 0
        self.fail_after_write = False
        self.fail_reads = False
        self.ignore_write = False
        self.before_call = None

    def __call__(self, method, url, *, json=None, timeout=10.0, max_response_bytes=MAX_RESPONSE_BYTES):
        self.calls.append((method, url, deepcopy(json)))
        assert timeout == 10.0
        assert max_response_bytes == 4194304
        parsed = urlsplit(url)
        assert parsed.scheme == "https"
        assert parsed.netloc == "sheets.googleapis.com"
        assert parsed.path.startswith("/v4/spreadsheets/sheet-test")
        if self.before_call:
            self.before_call(method, url, json)
        if method == "GET" and self.fail_reads:
            raise RuntimeError("Bearer do-not-disclose; token=remote-private-body")
        if method == "GET" and "/values/" not in parsed.path:
            return {"sheets": [{"properties": {"title": self.tab_name,
                    "gridProperties": {"rowCount": self.rows, "columnCount": self.columns}}}]}
        if method == "GET":
            assert parse_qs(parsed.query) == {"valueRenderOption": ["FORMULA"], "majorDimension": ["ROWS"]}
            return {"range": unquote(parsed.path.split("/values/")[1]),
                    "majorDimension": "ROWS", "values": deepcopy(self.values)}
        assert method == "PUT"
        assert parse_qs(parsed.query) == {"valueInputOption": ["RAW"]}
        assert json["majorDimension"] == "ROWS"
        assert unquote(parsed.path.split("/values/")[1]) == json["range"]
        position = int(re.search(r"!A(\d+):I\d+$", json["range"])[1])
        assert len(json["values"]) == 1 and len(json["values"][0]) == 9
        self.writes += 1
        if not self.ignore_write:
            while len(self.values) < position:
                self.values.append([])
            self.values[position - 1] = deepcopy(json["values"][0])
        if self.fail_after_write:
            self.fail_after_write = False
            raise RuntimeError("Bearer do-not-disclose; token=remote-private-body")
        return {"updatedRows": 1, "updatedColumns": 9, "updatedCells": 9}


@pytest.fixture
def sync(tmp_path):
    outbox = SheetOutbox(tmp_path / "tracking" / "sheets-outbox.json")
    transport = GoogleShapedTransport()
    adapter = GoogleSheetsAdapter("sheet-test", "Owned Jobs", transport)
    return outbox, adapter, transport


def state(outbox):
    return read_state(outbox.path)


def enqueue(sync, fields=None, job_id=JOB):
    outbox, adapter, _ = sync
    return outbox.enqueue(job_id, FIELDS if fields is None else fields, adapter.target)


def test_duplicate_enqueue_is_durable_order_independent_and_write_free(sync):
    outbox, adapter, transport = sync
    first = enqueue(sync)
    persisted = outbox.path.read_bytes()
    second = SheetOutbox(outbox.path).enqueue(JOB, dict(reversed(list(FIELDS.items()))), adapter.target)

    assert first == second
    assert outbox.path.read_bytes() == persisted
    assert transport.calls == []
    assert set(state(outbox)) == {"contract_version", "items"}
    item = state(outbox)["items"][0]
    assert item["operation_id"] == first
    assert item["fields"] == FIELDS
    assert item["attempts"] == 0
    assert item["state"] == "pending"
    assert re.fullmatch("[a-f0-9]{64}", item["payload_hash"])


def test_intent_and_attempt_exist_on_disk_before_first_remote_call(sync):
    outbox, adapter, transport = sync
    operation = enqueue(sync)

    def check_intent(*_):
        item = state(SheetOutbox(outbox.path))["items"][0]
        assert item["operation_id"] == operation
        assert item["state"] == "pending"
        assert item["attempts"] == 1

    transport.before_call = check_intent
    report = outbox.deliver(adapter, adapter.target)
    assert report == {"target": adapter.target, "attempted": 1, "delivered": 1,
                      "pending": 0, "blocked": 0, "error": None}
    assert transport.writes == 1
    assert len(transport.calls) == 7
    assert transport.values[1][0] == JOB
    assert dict(zip(HEADERS[2:], transport.values[1][2:])) == {
        **FIELDS, "applied_at": "", "resume_version": "", "confirmation_evidence": ""}


def test_success_is_acknowledged_only_after_readback_and_reload_is_noop(sync):
    outbox, adapter, transport = sync
    enqueue(sync)
    assert outbox.deliver(adapter, adapter.target)["delivered"] == 1
    assert [call[0] for call in transport.calls] == ["GET", "GET", "GET", "GET", "PUT", "GET", "GET"]
    calls = len(transport.calls)
    reloaded = SheetOutbox(outbox.path)
    assert reloaded.status()["delivered"] == 1
    assert reloaded.deliver(adapter, adapter.target)["attempted"] == 0
    assert len(transport.calls) == calls


def test_uncertain_remote_write_reconciles_after_durable_reload_without_duplicate(sync):
    outbox, adapter, transport = sync
    enqueue(sync)
    transport.fail_after_write = True

    first = outbox.deliver(adapter, adapter.target)
    assert first["error"] == "remote_unavailable"
    assert first["pending"] == 1 and first["delivered"] == 0
    assert transport.writes == 1 and len(transport.values) == 2
    assert state(outbox)["items"][0]["state"] == "pending"
    assert "do-not-disclose" not in outbox.path.read_text(encoding="utf-8")

    second = SheetOutbox(outbox.path).deliver(adapter, adapter.target)
    assert second["error"] is None and second["delivered"] == 1
    assert transport.writes == 1
    assert [row[0] for row in transport.values[1:]] == [JOB]
    assert state(outbox)["items"][0]["attempts"] == 2


def test_process_interruption_after_write_also_reconciles(sync):
    outbox, adapter, transport = sync
    enqueue(sync)

    def stop_after_committed_write(method, *_):
        if method == "GET" and transport.writes:
            raise KeyboardInterrupt

    transport.before_call = stop_after_committed_write
    with pytest.raises(KeyboardInterrupt):
        outbox.deliver(adapter, adapter.target)
    assert state(outbox)["items"][0]["state"] == "pending"
    transport.before_call = None
    assert SheetOutbox(outbox.path).deliver(adapter, adapter.target)["delivered"] == 1
    assert transport.writes == 1


def test_unrelated_remote_version_blocks_without_overwrite(sync, tmp_path):
    outbox, adapter, transport = sync
    other = SheetOutbox(tmp_path / "first-writer.json")
    other.enqueue(JOB, {**FIELDS, "title": "Remote changed title"}, adapter.target)
    assert other.deliver(adapter, adapter.target)["delivered"] == 1
    remote_before = deepcopy(transport.values)
    enqueue(sync)

    report = outbox.deliver(adapter, adapter.target)
    assert report["error"] == "remote_conflict"
    assert report["blocked"] == 1 and report["delivered"] == 0
    assert transport.values == remote_before and transport.writes == 1
    calls = len(transport.calls)
    assert outbox.deliver(adapter, adapter.target)["error"] == "remote_conflict"
    assert len(transport.calls) == calls


def test_explicit_known_version_updates_owned_row_without_append(sync):
    outbox, adapter, transport = sync
    enqueue(sync)
    outbox.deliver(adapter, adapter.target)
    known_hash = outbox.status()["items"][0]["payload_hash"]
    changed = {**FIELDS, "title": "Senior Backend Engineer"}
    outbox.enqueue(JOB, changed, adapter.target, expected_remote_hash=known_hash)

    assert outbox.deliver(adapter, adapter.target)["delivered"] == 1
    assert len(transport.values) == 2 and transport.writes == 2
    assert transport.values[1][3] == "Senior Backend Engineer"
    assert outbox.status()["items"][1]["payload_hash"] != known_hash


@pytest.mark.parametrize("precondition", ["stale", "missing"])
def test_stale_or_missing_expected_version_cannot_overwrite_or_recreate(sync, precondition):
    outbox, adapter, transport = sync
    enqueue(sync)
    outbox.deliver(adapter, adapter.target)
    known_hash = outbox.status()["items"][0]["payload_hash"]
    if precondition == "missing":
        transport.values = [list(HEADERS)]
    expected = "0" * 64 if precondition == "stale" else known_hash
    outbox.enqueue(JOB, {**FIELDS, "title": "Changed"}, adapter.target, expected)
    before = deepcopy(transport.values)
    assert outbox.deliver(adapter, adapter.target)["error"] == "remote_conflict"
    assert transport.values == before and transport.writes == 1


def test_unknown_or_tampered_hash_is_not_trusted(sync):
    outbox, adapter, transport = sync
    enqueue(sync)
    outbox.deliver(adapter, adapter.target)
    known_hash = outbox.status()["items"][0]["payload_hash"]
    transport.values[1][3] = "Untracked remote edit"
    outbox.enqueue(JOB, {**FIELDS, "title": "Requested edit"}, adapter.target, known_hash)
    before = deepcopy(transport.values)
    assert outbox.deliver(adapter, adapter.target)["error"] == "remote_version"
    assert transport.values == before and transport.writes == 1


def test_duplicate_remote_keys_block_even_when_other_job_is_requested(sync):
    outbox, adapter, transport = sync
    enqueue(sync)
    outbox.deliver(adapter, adapter.target)
    transport.values.append(deepcopy(transport.values[1]))
    enqueue(sync, job_id="lever:second")
    before = deepcopy(transport.values)
    report = outbox.deliver(adapter, adapter.target)
    assert report["error"] == "duplicate_remote_id"
    assert transport.values == before and transport.writes == 1


def test_successful_put_with_missing_readback_is_never_acknowledged(sync):
    outbox, adapter, transport = sync
    enqueue(sync)
    transport.ignore_write = True
    report = outbox.deliver(adapter, adapter.target)
    assert report["error"] == "readback_mismatch"
    assert report["blocked"] == 1 and report["delivered"] == 0
    assert state(outbox)["items"][0]["state"] == "blocked"
    assert transport.writes == 1


def test_successful_put_with_old_valid_version_readback_is_never_acknowledged(sync):
    outbox, adapter, transport = sync
    enqueue(sync)
    outbox.deliver(adapter, adapter.target)
    known_hash = outbox.status()["items"][0]["payload_hash"]
    outbox.enqueue(JOB, {**FIELDS, "title": "Changed"}, adapter.target, known_hash)
    transport.ignore_write = True

    report = outbox.deliver(adapter, adapter.target)
    assert report["error"] == "readback_mismatch" and report["delivered"] == 0
    assert report["blocked"] == 1
    assert transport.values[1][3] == "Backend Engineer"


def test_target_identity_isolated_and_wrong_adapter_has_zero_requests(sync):
    outbox, adapter, transport = sync
    other_transport = GoogleShapedTransport()
    other = GoogleSheetsAdapter("sheet-test-2", "Owned Jobs", other_transport)
    first = enqueue(sync)
    second = outbox.enqueue(JOB, FIELDS, other.target)
    assert first != second
    assert outbox.status()["items"][0]["payload_hash"] != outbox.status()["items"][1]["payload_hash"]
    with pytest.raises(SheetSyncError, match="target_mismatch"):
        outbox.deliver(adapter, other.target)
    assert transport.calls == other_transport.calls == []
    assert outbox.deliver(adapter, adapter.target)["delivered"] == 1
    assert outbox.status()["pending"] == 1
    assert other_transport.calls == []
    assert outbox.deliver(other, other.target)["delivered"] == 1
    assert outbox.status()["pending"] == 0
    assert transport.values[1][0] == other_transport.values[1][0] == JOB
    assert transport.values[1][1] != other_transport.values[1][1]


def test_changed_pending_version_cannot_overtake_uncertain_operation(sync):
    outbox, adapter, transport = sync
    enqueue(sync)
    before = outbox.path.read_bytes()
    with pytest.raises(SheetSyncError, match="pending_version"):
        outbox.enqueue(JOB, {**FIELDS, "title": "Newer version"}, adapter.target)
    assert outbox.path.read_bytes() == before
    assert transport.calls == []


@pytest.mark.parametrize("field", ["notes", "resume_contents", "auth_token", "authorization", "email", "raw_jd"])
def test_private_or_unapproved_fields_never_enter_state_or_transport(sync, field):
    outbox, _, transport = sync
    with pytest.raises(SheetSyncError, match="invalid_payload"):
        enqueue(sync, {**FIELDS, field: "must-stay-private"})
    assert not outbox.path.exists() and transport.calls == []


@pytest.mark.parametrize("value", ["=IMPORTDATA(\"https://evil.test\")", "+formula", "-formula", "@formula",
                                  " '=formula", "line\nbreak", "carriage\rreturn", "tab\there", "hidden\x00value",
                                  "hidden\u202evalue", "Bearer secret-value", "token=secret-value",
                                  "api_key: secret-value", "ghp_1234567890abcdefghijkl", "sk-1234567890abcdef"])
def test_formula_control_and_credential_like_text_is_rejected_without_echo(sync, value):
    outbox, _, transport = sync
    with pytest.raises(SheetSyncError) as error:
        enqueue(sync, {**FIELDS, "title": value})
    assert str(error.value) == "invalid_payload"
    assert not outbox.path.exists() and transport.calls == []


@pytest.mark.parametrize("value", ["https://user:secret@example.test/job", "https://example.test/job?token=secret",
                                  "https://example.test/job#secret", "javascript:alert(1)", "not a URL"])
def test_unsafe_job_url_is_rejected(sync, value):
    outbox, _, transport = sync
    with pytest.raises(SheetSyncError, match="invalid_payload"):
        enqueue(sync, {**FIELDS, "url": value})
    assert not outbox.path.exists() and transport.calls == []


@pytest.mark.parametrize("fields", [
    {**FIELDS, "status": "applied"},
    {**CONFIRMED, "confirmation_evidence": ""},
    {key: value for key, value in CONFIRMED.items() if key != "resume_version"},
    {**CONFIRMED, "applied_at": "2026-10-24T09:00:00"},
    {**CONFIRMED, "applied_at": "2026-02-30T09:00:00Z"},
    {**CONFIRMED, "resume_version": "C:/private/resume.pdf"},
])
def test_browser_marker_or_incomplete_application_evidence_is_rejected(sync, fields):
    outbox, _, transport = sync
    with pytest.raises(SheetSyncError, match="invalid_payload"):
        enqueue(sync, fields)
    assert not outbox.path.exists() and transport.calls == []


@pytest.mark.parametrize("applied_at", ["unknown", "2026-10-24T09:00:00+08:00"])
def test_explicit_confirmed_application_payload_can_be_delivered(sync, applied_at):
    outbox, adapter, transport = sync
    enqueue(sync, {**CONFIRMED, "applied_at": applied_at})
    assert outbox.deliver(adapter, adapter.target)["delivered"] == 1
    row = dict(zip(HEADERS, transport.values[1]))
    assert row["status"] == "applied"
    assert row["applied_at"] == applied_at
    assert row["resume_version"] == "backend-v2"
    assert row["confirmation_evidence"] == "Submission receipt checked locally"


@pytest.mark.parametrize("alteration", ["header", "formula", "unrelated", "unknown-version", "too-many-rows", "extra-column", "wrong-tab"])
def test_invalid_remote_tab_stops_without_repair_or_overwrite(sync, alteration):
    outbox, adapter, transport = sync
    enqueue(sync)
    expected = {"header": "wrong_headers", "formula": "remote_formula", "unrelated": "remote_schema",
                "unknown-version": "remote_schema", "too-many-rows": "remote_capacity",
                "extra-column": "remote_capacity", "wrong-tab": "wrong_tab"}[alteration]
    if alteration == "header":
        transport.values[0][0] = "master-sheet-key"
    elif alteration == "formula":
        transport.values.append(["", "", "=IMPORTDATA(\"https://evil.test\")"])
    elif alteration == "unrelated":
        transport.values.append(["", "", "Private master-sheet note"])
    elif alteration == "unknown-version":
        transport.values.append([JOB, "unsupported-v9", "Example Systems"])
    elif alteration == "too-many-rows":
        transport.rows = 1002
    elif alteration == "extra-column":
        transport.columns = 10
    else:
        transport.tab_name = "Other tab"
    before = deepcopy(transport.values)
    report = outbox.deliver(adapter, adapter.target)
    assert report["error"] == expected
    assert report["blocked"] == 1 and transport.writes == 0
    assert transport.values == before


def test_write_rechecks_remote_precondition_after_outbox_read(sync):
    outbox, adapter, transport = sync
    enqueue(sync)

    def conflicting_edit(method, url, _):
        # The outbox's read has finished; change the tab before the adapter writes.
        if len(transport.calls) == 3 and method == "GET":
            transport.values.append(["", "", "Unrelated concurrent edit"])

    transport.before_call = conflicting_edit
    report = outbox.deliver(adapter, adapter.target)
    assert report["error"] == "remote_schema"
    assert transport.writes == 0
    assert transport.values[1][2] == "Unrelated concurrent edit"


def test_blank_slot_is_chosen_by_key_without_touching_other_owned_rows(sync):
    outbox, adapter, transport = sync
    enqueue(sync)
    outbox.deliver(adapter, adapter.target)
    first = deepcopy(transport.values[1])
    transport.values = [list(HEADERS), [], first]
    enqueue(sync, job_id="lever:second")
    assert outbox.deliver(adapter, adapter.target)["delivered"] == 1
    assert transport.values[1][0] == "lever:second"
    assert transport.values[2] == first
    assert transport.writes == 2


@pytest.mark.parametrize('uncertain_last_write', [False, True])
def test_repeated_transition_after_intervening_versions_retains_each_delivery(sync, uncertain_last_write):
    outbox, adapter, transport = sync
    expected = None
    operation_ids = []
    for index, title in enumerate(('Version A', 'Version B', 'Version A', 'Version B')):
        outbox = SheetOutbox(outbox.path)
        operation = outbox.enqueue(JOB, {**FIELDS, 'title': title}, adapter.target, expected_remote_hash=expected)
        operation_ids.append(operation)
        transport.fail_after_write = uncertain_last_write and index == 3
        report = outbox.deliver(adapter, adapter.target)
        if uncertain_last_write and index == 3:
            assert report['error'] == 'remote_unavailable' and report['pending'] == 1
            report = SheetOutbox(outbox.path).deliver(adapter, adapter.target)
        assert report['error'] is None and report['delivered'] == 1
        assert transport.values[1][3] == title
        expected = outbox.status()['items'][-1]['payload_hash']
    assert len(set(operation_ids)) == 4 and transport.writes == 4
    assert len(state(outbox)['items']) == 4
    assert all(item['state'] == 'delivered' for item in state(outbox)['items'])
    before = outbox.path.read_bytes()
    assert outbox.enqueue(JOB, {**FIELDS, 'title': 'Version B'}, adapter.target,
                          expected_remote_hash=state(outbox)['items'][-1]['expected_remote_hash']) == operation_ids[-1]
    assert outbox.path.read_bytes() == before
    assert outbox.deliver(adapter, adapter.target)['attempted'] == 0


def test_delivery_and_http_work_are_bounded(sync):
    outbox, adapter, transport = sync
    for index in range(3):
        enqueue(sync, job_id=f"job:{index}")
    report = outbox.deliver(adapter, adapter.target, max_items=2)
    assert report["attempted"] == report["delivered"] == 2
    assert report["pending"] == 1
    assert len(transport.calls) == 14


def test_full_dedicated_grid_stops_instead_of_appending_outside_bound(sync):
    outbox, adapter, transport = sync
    transport.rows = 2
    enqueue(sync)
    assert outbox.deliver(adapter, adapter.target)["delivered"] == 1
    before = deepcopy(transport.values)
    enqueue(sync, job_id="lever:second")
    report = outbox.deliver(adapter, adapter.target)
    assert report["error"] == "remote_capacity" and report["blocked"] == 1
    assert transport.values == before and transport.writes == 1


def test_returned_remote_error_body_is_not_disclosed(sync):
    outbox, adapter, transport = sync
    enqueue(sync)
    adapter.transport = lambda *args, **kwargs: {"error": {"message": "token=remote-private-body"}}
    report = outbox.deliver(adapter, adapter.target)
    assert report["error"] == "remote_unavailable"
    assert "remote-private-body" not in json.dumps(report) + outbox.path.read_text(encoding="utf-8")
    assert transport.calls == []


@pytest.mark.parametrize("limit", [0, -1, MAX_DELIVERY_ITEMS + 1, True, 1.5])
def test_invalid_delivery_limit_makes_no_request(sync, limit):
    outbox, adapter, transport = sync
    enqueue(sync)
    with pytest.raises(SheetSyncError, match="invalid_limit"):
        outbox.deliver(adapter, adapter.target, max_items=limit)
    assert transport.calls == []


def test_transport_errors_have_bounded_manual_retries_and_no_secret_disclosure(sync):
    outbox, adapter, transport = sync
    enqueue(sync)
    transport.fail_reads = True
    for attempt in range(MAX_ATTEMPTS):
        report = outbox.deliver(adapter, adapter.target)
        assert report["attempted"] == 1
        assert state(outbox)["items"][0]["attempts"] == attempt + 1
    assert report["error"] == "retry_exhausted" and report["blocked"] == 1
    assert len(transport.calls) == MAX_ATTEMPTS
    assert outbox.deliver(adapter, adapter.target)["attempted"] == 0
    assert len(transport.calls) == MAX_ATTEMPTS
    assert "do-not-disclose" not in json.dumps(outbox.status()) + outbox.path.read_text(encoding="utf-8")


def test_queue_limit_includes_delivery_receipts_without_silent_pruning(sync, monkeypatch):
    outbox, adapter, transport = sync
    # Exercise the same boundary with a small deterministic fixture, not 500 writes.
    monkeypatch.setattr("job_radar.sheet_sync.MAX_ITEMS", 2)
    enqueue(sync, job_id="job:1")
    outbox.deliver(adapter, adapter.target)
    enqueue(sync, job_id="job:2")
    before = outbox.path.read_bytes()
    with pytest.raises(SheetSyncError, match="outbox_full"):
        enqueue(sync, job_id="job:3")
    assert outbox.path.read_bytes() == before
    assert outbox.status()["total"] == 2 and outbox.status()["delivered"] == 1
    assert MAX_ITEMS == 500


def test_single_host_guard_prevents_competing_writer_and_status_omits_payload(sync):
    outbox, adapter, transport = sync
    enqueue(sync)
    with writer_guard(outbox.guard_path):
        with pytest.raises(ValueError, match="another writer"):
            SheetOutbox(outbox.path).deliver(adapter, adapter.target)
    assert transport.calls == []
    assert "fields" not in outbox.status()["items"][0]
    assert "Example Systems" not in json.dumps(outbox.status())


@pytest.mark.parametrize("alteration", ["extra-field", "changed-payload", "unknown-state", "duplicate-operation", "wrong-version"])
def test_invalid_local_state_is_preserved_and_never_sent(sync, alteration):
    outbox, adapter, transport = sync
    enqueue(sync)
    document = state(outbox)
    if alteration == "extra-field":
        document["items"][0]["credentials"] = "must-stay-local"
    elif alteration == "changed-payload":
        document["items"][0]["fields"]["title"] = "Untracked local edit"
    elif alteration == "unknown-state":
        document["items"][0]["state"] = "unknown"
    elif alteration == "duplicate-operation":
        document["items"].append(deepcopy(document["items"][0]))
    else:
        document["contract_version"] = 99
    # State corruption fixture only; production writes always use local_state.
    from job_radar.local_state import write_state
    write_state(outbox.path, document)
    before = outbox.path.read_bytes()
    with pytest.raises(SheetSyncError, match="invalid_outbox"):
        outbox.deliver(adapter, adapter.target)
    assert outbox.path.read_bytes() == before and transport.calls == []


def test_local_persistence_failure_occurs_before_network(sync, monkeypatch):
    outbox, adapter, transport = sync
    enqueue(sync)
    before = outbox.path.read_bytes()

    def fail(*_):
        raise OSError("private-path-do-not-echo")

    monkeypatch.setattr("job_radar.sheet_sync.write_state", fail)
    with pytest.raises(SheetSyncError, match="local_state_failed"):
        outbox.deliver(adapter, adapter.target)
    assert transport.calls == [] and outbox.path.read_bytes() == before


def test_failure_to_persist_ack_after_remote_write_reconciles_without_another_put(sync, monkeypatch):
    outbox, adapter, transport = sync
    enqueue(sync)
    from job_radar.local_state import write_state

    def fail_ack(path, document):
        if document["items"][0]["state"] == "delivered":
            raise OSError("synthetic disk unavailable")
        write_state(path, document)

    with monkeypatch.context() as context:
        context.setattr("job_radar.sheet_sync.write_state", fail_ack)
        with pytest.raises(SheetSyncError, match="local_state_failed"):
            outbox.deliver(adapter, adapter.target)
    assert state(outbox)["items"][0]["state"] == "pending"
    assert transport.writes == 1
    assert SheetOutbox(outbox.path).deliver(adapter, adapter.target)["delivered"] == 1
    assert transport.writes == 1


def test_tab_range_escapes_names_and_remains_on_fixed_official_url(tmp_path):
    transport = GoogleShapedTransport("Owner's Jobs")
    adapter = GoogleSheetsAdapter("sheet-test", "Owner's Jobs", transport)
    outbox = SheetOutbox(tmp_path / "outbox.json")
    assert adapter.target == "sheets:sheet-test:Owner%27s%20Jobs"
    outbox.enqueue(JOB, FIELDS, adapter.target)
    assert outbox.deliver(adapter, adapter.target)["delivered"] == 1
    assert any("'Owner''s Jobs'!A2:I2" == call[2]["range"] for call in transport.calls if call[0] == "PUT")


@pytest.mark.parametrize("spreadsheet_id,tab_name", [("https://evil.test", "Owned"), ("id?token=secret", "Owned"),
                                                    ("ghp_1234567890abcdefghijkl", "Owned"),
                                                    ("sheet-test", "Bad!\nTab"), ("sheet-test", "[Master]"),
                                                    ("sheet-test", "=formula")])
def test_invalid_target_never_reaches_transport(spreadsheet_id, tab_name):
    transport = GoogleShapedTransport()
    with pytest.raises(SheetSyncError, match="invalid_target"):
        GoogleSheetsAdapter(spreadsheet_id, tab_name, transport)
    assert transport.calls == []


@pytest.mark.parametrize("kind", ["job_id", "target"])
def test_credential_like_identifier_is_not_persisted(sync, kind):
    outbox, adapter, transport = sync
    credential_fixture = "ghp_" + "1234567890abcdefghijkl"
    with pytest.raises(SheetSyncError, match=f"invalid_{kind}"):
        outbox.enqueue(credential_fixture if kind == "job_id" else JOB, FIELDS,
                       credential_fixture if kind == "target" else adapter.target)
    assert not outbox.path.exists() and transport.calls == []
