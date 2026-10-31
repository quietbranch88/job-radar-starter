from __future__ import annotations

import json
from dataclasses import asdict
from io import StringIO
from pathlib import Path

import pytest

from job_radar.adapters import parse_ashby, parse_greenhouse, parse_lever
from job_radar.cli import main
from job_radar.documents import load_jobs
from job_radar.config import Companies, Matching, Preferences, Profile, UserConfig
from job_radar.data.tracking_store import LocalTrackingStore
from job_radar.public_workflow import execute_public_workflow, view_model_from_document
from job_radar.review_provider import compact_review_jobs
from job_radar.tracking_commands import build_metrics, due_actions, record_application_event


CONFIG = UserConfig(
    Profile(("PRIVATE_PROFILE_SENTINEL",), "unused-private-resume.txt"),
    Preferences(("CA",), ("backend",), (), False),
    Companies((), ()),
    Matching(1, (), ()),
)
ALIASES = {"contract_version": 1, "company_aliases": {}, "title_aliases": {}}


def posting(source: str, requirement: str):
    common = {"id": "role-1", "title": "Backend Engineer", "location": "Toronto"}
    if source == "greenhouse":
        return parse_greenhouse({"jobs": [{
            **common, "location": {"name": "Toronto"},
            "absolute_url": "https://example.test/role-1", "content": requirement,
        }]}, "Example Systems")[0]
    if source == "lever":
        return parse_lever([{
            "id": "role-1", "text": "Backend Engineer",
            "categories": {"location": "Toronto"},
            "hostedUrl": "https://example.test/role-1", "descriptionPlain": requirement,
        }], "Example Systems")[0]
    return parse_ashby({"jobs": [{
        **common, "jobUrl": "https://example.test/role-1", "descriptionPlain": requirement,
    }]}, "Example Systems")[0]


def workflow(store, job, observed_on="2026-10-23"):
    return execute_public_workflow(
        scan_result={
            "contract_version": 1, "mode": "atomic", "incomplete": False,
            "jobs": [asdict(job)], "failures": [],
        }, config=CONFIG, repository=store, observed_on=observed_on, aliases=ALIASES,
    )


@pytest.mark.parametrize("source", ["greenhouse", "lever", "ashby"])
def test_official_requirement_change_survives_storage_and_review(source, tmp_path):
    store = LocalTrackingStore(tmp_path / "state")
    before = workflow(store, posting(source, "Required: Go and PostgreSQL."))
    after = workflow(store, posting(source, "Required: Rust and SQLite."))
    first, second = before["lifecycle"]["jobs"][0], after["lifecycle"]["jobs"][0]
    assert first.get("jd_hash"), "official description must produce a content digest"
    assert first["jd_hash"] != second["jd_hash"]
    assert "Rust and SQLite" in " ".join(second["jd_evidence"])
    assert second["jd_evidence_source"].startswith(source + ":")
    stored = store.read("lifecycle")["items"][0]
    assert stored["jd_hash"] == second["jd_hash"]
    scan = tmp_path / "scan.json"
    scan.write_text(json.dumps(after["scan"]), encoding="utf-8")
    reloaded = load_jobs(scan)[0]
    assert asdict(reloaded)["jd_hash"] == second["jd_hash"]
    reviewed = compact_review_jobs([stored])[0]
    assert "Rust and SQLite" in " ".join(reviewed["jd_evidence"])
    assert compact_review_jobs([first])[0]["jd_hash"] != reviewed["jd_hash"]
    assert "PRIVATE_PROFILE_SENTINEL" not in json.dumps(reviewed["jd_evidence"])


def test_jd_evidence_is_bounded_and_hashes_content_beyond_the_excerpt():
    first = asdict(posting("greenhouse", "<p>Go &amp; SQL</p><script>bad()</script>" + "x" * 6000))
    second = asdict(posting("greenhouse", "<p>Go &amp; SQL</p><script>bad()</script>" + "x" * 6000 + " Rust"))
    assert first.get("jd_evidence"), "bounded source evidence is missing"
    assert len(first["jd_evidence"]) <= 8
    assert all(len(item) <= 500 for item in first["jd_evidence"])
    assert "Go & SQL" in " ".join(first["jd_evidence"])
    assert "bad()" not in " ".join(first["jd_evidence"])
    assert first["jd_hash"] != second["jd_hash"]


def test_rendering_a_retained_scan_does_not_invent_a_new_observation(tmp_path):
    store = LocalTrackingStore(tmp_path / "state")
    saved = workflow(store, posting("ashby", "Required: Go."), "2026-07-17")["scan"]
    model = view_model_from_document("scan", saved)
    assert model["jobs"][0]["last_seen"] == "2026-07-17"
    assert model["scan"]["observed_on"] == "2026-07-17"


def record_args(tmp_path, applied_at="2026-10-22T16:30:00+08:00"):
    store = LocalTrackingStore(tmp_path / "user-data")
    result = workflow(store, posting("ashby", "Required: Go."))
    job_id = result["lifecycle"]["jobs"][0]["stable_id"]
    state = tmp_path / "dashboard-state.json"
    state.write_text(json.dumps({
        "contract_version": 1, "exported_at": "2026-10-23T00:00:00Z",
        "statuses": {job_id: "applied"}, "tracking": result["view_model"]["tracking"],
    }), encoding="utf-8")
    return store, job_id, [
        "--json", "tracking", "record-application", "--user-data", str(store.root),
        "--from-dashboard", str(state), "--job-id", job_id,
        "--applied-at", applied_at, "--resume-version", "backend-v2",
        "--channel", "official-site", "--confirmation-evidence", "Submission receipt checked locally",
        "--confirmed-at", "2026-10-23T00:05:00Z", "--confirm-submitted",
    ]


def invoke(args):
    stdout, stderr = StringIO(), StringIO()
    try:
        code = main(args, stdout=stdout, stderr=stderr)
    except SystemExit as error:
        code = error.code
    return code, stdout.getvalue(), stderr.getvalue()


@pytest.mark.parametrize("applied_at", ["2026-10-22T16:30:00+08:00", "unknown"])
def test_local_confirmed_application_bridge_is_durable_and_retry_safe(tmp_path, applied_at):
    store, job_id, args = record_args(tmp_path, applied_at)
    code, output, error = invoke(args)
    assert code == 0, (output, error)
    assert invoke(args)[0] == 0
    applications = LocalTrackingStore(store.root).read("applications")["items"]
    assert len(applications) == 1
    application = applications[0]
    assert application["job_id"] == job_id
    assert application["status"] == "applied"
    assert application["applied_at"] == applied_at
    assert application["resume_version"] == "backend-v2"
    assert application["channel"] == "official-site"
    assert application["confirmation_evidence"] == "Submission receipt checked locally"
    assert build_metrics(store)["funnel"] == {"applied": 1}
    if applied_at == "unknown":
        assert due_actions(store, as_of="2026-11-30T00:00:00Z") == []
    rebuilt = workflow(LocalTrackingStore(store.root), posting("ashby", "Required: Go."))
    assert rebuilt["view_model"]["tracking"]["statuses"][job_id] == "applied"
    assert rebuilt["view_model"]["tracking"]["metrics"]["total"] == 1


@pytest.mark.parametrize("alteration", ["unconfirmed", "naive-time", "missing-evidence", "unknown-job", "not-applied"])
def test_status_import_does_not_infer_a_submission(tmp_path, alteration):
    store, job_id, args = record_args(tmp_path)
    if alteration == "unconfirmed":
        args.remove("--confirm-submitted")
    elif alteration == "naive-time":
        args[args.index("--applied-at") + 1] = "2026-10-22T16:30:00"
    elif alteration == "missing-evidence":
        args[args.index("--confirmation-evidence") + 1] = ""
    elif alteration == "unknown-job":
        args[args.index("--job-id") + 1] = "ashby:missing"
    else:
        path = Path(args[args.index("--from-dashboard") + 1])
        data = json.loads(path.read_text(encoding="utf-8"))
        data["statuses"][job_id] = "interested"
        path.write_text(json.dumps(data), encoding="utf-8")
    code, output, error = invoke(args)
    assert code == 2, (output, error)
    message = json.loads(output)["error"]["message"]
    expected = {
        "unconfirmed": "--confirm-submitted is required",
        "naive-time": "ISO datetime",
        "missing-evidence": "confirmation_evidence must not be empty",
        "unknown-job": "Applied marker",
        "not-applied": "Applied marker",
    }
    assert expected[alteration] in message
    assert store.read("applications")["items"] == []


def test_confirmed_tracking_can_rebuild_offline_without_refreshing_observation(tmp_path):
    store, job_id, args = record_args(tmp_path, "unknown")
    assert invoke(args)[0] == 0
    result = workflow(store, posting("ashby", "Required: Go."), "2026-10-23")
    saved = tmp_path / "saved-scan.json"
    saved.write_text(json.dumps(result["scan"]), encoding="utf-8")
    output = tmp_path / "dashboard.html"
    code, stdout, stderr = invoke([
        "build-dashboard", "--jobs", str(saved), "--output", str(output),
        "--user-data", str(store.root),
    ])
    assert code == 0, (stdout, stderr)
    html = output.read_text(encoding="utf-8")
    assert "1 tracked applications" in html
    assert f'"{job_id}":"applied"' in html
    assert '"last_seen":"2026-10-23"' in html


def test_confirmation_replay_cannot_downgrade_or_rewrite_an_application(tmp_path):
    store, job_id, args = record_args(tmp_path)
    assert invoke(args)[0] == 0
    application = store.read("applications")["items"][0]
    record_application_event(
        store, application_id=application["application_id"], event_type="interview_scheduled",
        occurred_at="2026-10-23T01:00:00Z", details={}, status="interview",
    )
    before = store.read("applications")
    assert invoke(args)[0] == 0
    assert store.read("applications") == before
    assert before["items"][0]["job_id"] == job_id
    assert before["items"][0]["status"] == "interview"
    args[args.index("--resume-version") + 1] = "conflicting-v3"
    code, output, _ = invoke(args)
    assert code == 2
    assert "conflicting confirmed application replay" in json.loads(output)["error"]["message"]
    assert store.read("applications") == before
