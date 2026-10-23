from __future__ import annotations

import hashlib
import json
import os
import socket
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from job_radar.adapters import parse_greenhouse
from job_radar.config import Companies, Matching, Preferences, Profile, UserConfig
from job_radar.data.tracking_store import LocalTrackingStore
from job_radar.domain.tracking import stable_job_id
from job_radar.public_workflow import execute_public_workflow
from job_radar.tracking_commands import upsert_application


ALIASES = {"contract_version": 1, "company_aliases": {}, "title_aliases": {}}
OBSERVED_ON = "2026-10-23"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("observation recovery must not access the network")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def _config(minimum_score: int = 50, *, role: str = "backend") -> UserConfig:
    return UserConfig(
        Profile((), "unused-resume.txt"),
        Preferences(("CA",), (role,), (), False),
        Companies((), ()),
        Matching(minimum_score, (), ()),
    )


def _job(
    external_id: str,
    *,
    title: str = "Backend Engineer",
    description: str = "Maintain public services.",
    source: str = "greenhouse",
) -> dict[str, Any]:
    parsed = parse_greenhouse(
        {"jobs": [{
            "id": external_id,
            "title": title,
            "location": {"name": "Toronto"},
            "absolute_url": f"https://example.test/jobs/{external_id}",
            "updated_at": "2026-10-01T00:00:00Z",
            "content": description,
        }]},
        "Invented Systems",
    )[0]
    item = asdict(replace(parsed, source=source, country="CA"))
    for field in ("tracks", "skills", "jd_evidence"):
        item[field] = list(item[field])
    return item


def _scan(*jobs: dict[str, Any], incomplete: bool = False) -> dict[str, Any]:
    return {
        "contract_version": 1,
        "mode": "best-effort" if incomplete else "atomic",
        "incomplete": incomplete,
        "jobs": list(jobs),
        "failures": [{
            "source": "greenhouse",
            "company": "Unavailable Board",
            "category": "timeout",
            "message": "Synthetic source timeout",
        }] if incomplete else [],
    }


def _run(store: LocalTrackingStore, scan: dict[str, Any], **kwargs):
    return execute_public_workflow(
        scan_result=scan,
        config=kwargs.pop("config", _config()),
        repository=store,
        observed_on=kwargs.pop("observed_on", OBSERVED_ON),
        aliases=ALIASES,
        **kwargs,
    )


def _ids(jobs) -> set[str]:
    return {job["external_id"] for job in jobs}


def _seed(store: LocalTrackingStore, *jobs: dict[str, Any]) -> None:
    store.initialize()
    items = []
    for raw in jobs:
        item = deepcopy(raw)
        item.update(
            stable_id=stable_job_id(
                source=item["source"], external_id=item["external_id"],
                company=item["company"], title=item["title"],
            ),
            first_seen="2026-10-01",
            last_seen="2026-10-01",
            freshness="active",
            legacy_status_keys=sorted([
                f"{item['source'].casefold()}:{item['external_id']}", item["url"],
            ]),
        )
        items.append(item)
    store.write("lifecycle", {"contract_version": 1, "items": items})


@dataclass
class OfflineReviewRunner:
    evidence: Path
    result: Any
    raise_error: bool = False

    def __call__(self, jobs, **kwargs):
        self.evidence.write_text(json.dumps({
            "pid": os.getpid(),
            "jobs": jobs,
            "observed_on": kwargs["observed_on"],
        }), encoding="utf-8")
        if self.raise_error:
            raise ValueError("synthetic offline reviewer failure")
        return self.result


def _review_result(*, failures=None) -> dict[str, Any]:
    return {
        "contract_version": 1,
        "reviews": [],
        "rejected": [],
        "rejected_sample": [],
        "failures": [] if failures is None else failures,
        "company_facts_used": 0,
        "report": {"provider_calls": 0, "cache_hits": 0, "escalations": 0},
    }


@pytest.mark.parametrize("incomplete", [False, True])
def test_all_scored_observations_survive_output_and_local_persistence(tmp_path, incomplete):
    store = LocalTrackingStore(tmp_path / "state")
    scan = _scan(
        _job("visible"), _job("hidden", title="Operations Coordinator"),
        incomplete=incomplete,
    )
    original = deepcopy(scan)

    result = _run(store, scan)

    assert _ids(result["scan"]["jobs"]) == {"visible", "hidden"}
    persisted = LocalTrackingStore(store.root).read("lifecycle")["items"]
    assert _ids(persisted) == {"visible", "hidden"}
    assert _ids(result["lifecycle"]["jobs"]) == {"visible", "hidden"}
    assert _ids(result["view_model"]["jobs"]) == {"visible"}
    assert result["counts"] == {"scanned": 2, "published": 1}
    assert set(result["scan"]) == {
        "contract_version", "observed_on", "mode", "incomplete", "jobs", "failures",
    }
    assert result["scan"]["observed_on"] == OBSERVED_ON
    assert result["scan"]["failures"] == original["failures"]
    assert result["view_model"]["scan"]["state"] == ("partial" if incomplete else "complete")
    assert all(job["last_seen"] == OBSERVED_ON for job in persisted)
    assert scan == original


def test_low_scoring_existing_observation_refreshes_dates_and_jd_but_not_application(tmp_path):
    store = LocalTrackingStore(tmp_path / "state")
    old = _job("existing", title="Operations Coordinator", description="File paper records.")
    old["score"] = 40
    _seed(store, old)
    job_id = store.read("lifecycle")["items"][0]["stable_id"]
    upsert_application(store, {
        "job_id": job_id, "company": "Invented Systems", "title": old["title"],
        "status": "interview", "applied_at": "2026-10-02T09:00:00Z",
        "updated_at": "2026-10-03T09:00:00Z", "resume_version": "example-v1",
        "channel": "official-site", "country": "CA",
    })
    application_bytes = store.path_for("applications").read_bytes()
    event_bytes = store.path_for("events").read_bytes()
    updated = _job("existing", title=old["title"], description="Maintain digital records.")

    result = _run(store, _scan(updated))

    persisted = LocalTrackingStore(store.root).read("lifecycle")["items"][0]
    assert persisted["last_seen"] == OBSERVED_ON
    assert persisted["first_seen"] == "2026-10-01"
    assert persisted["freshness"] == "active"
    assert persisted["jd_evidence"] == ["Maintain digital records."]
    assert persisted["jd_hash"] == hashlib.sha256(b"Maintain digital records.").hexdigest()
    assert persisted["jd_hash"] != old["jd_hash"]
    assert persisted["jd_evidence_source"] == "greenhouse:content"
    assert persisted["source"] == "greenhouse"
    assert result["view_model"]["jobs"] == []
    assert result["view_model"]["tracking"]["statuses"] == {job_id: "applied"}
    assert result["view_model"]["tracking"]["metrics"]["funnel"] == {"interview": 1}
    assert store.path_for("applications").read_bytes() == application_bytes
    assert store.path_for("events").read_bytes() == event_bytes


def test_saved_scan_recovers_hidden_job_after_profile_change_without_network(tmp_path):
    store = LocalTrackingStore(tmp_path / "state")
    original = _run(store, _scan(_job("operations", title="Operations Coordinator")))
    assert original["view_model"]["jobs"] == []
    path = tmp_path / "retained-scan.json"
    path.write_text(json.dumps(original["scan"]), encoding="utf-8")
    saved_bytes = path.read_bytes()

    recovered = _run(
        LocalTrackingStore(store.root), json.loads(path.read_text(encoding="utf-8")),
        config=_config(role="operations"),
    )

    assert _ids(recovered["view_model"]["jobs"]) == {"operations"}
    assert _ids(recovered["scan"]["jobs"]) == {"operations"}
    assert recovered["counts"] == {"scanned": 1, "published": 1}
    stored = store.read("lifecycle")["items"][0]
    assert stored["score"] == 55
    assert stored["first_seen"] == OBSERVED_ON
    assert stored["last_seen"] == OBSERVED_ON
    assert stored["jd_evidence_source"] == "greenhouse:content"
    assert path.read_bytes() == saved_bytes


def test_rescored_existing_job_can_leave_and_reenter_display_without_rescanning(tmp_path):
    store = LocalTrackingStore(tmp_path / "state")
    first = _run(store, _scan(_job("backend")))
    original_id = first["lifecycle"]["jobs"][0]["stable_id"]

    hidden = _run(LocalTrackingStore(store.root), first["scan"], config=_config(role="operations"))

    assert hidden["view_model"]["jobs"] == []
    assert hidden["counts"] == {"scanned": 1, "published": 0}
    assert store.read("lifecycle")["items"][0]["score"] == 40
    restored = _run(LocalTrackingStore(store.root), hidden["scan"])
    assert _ids(restored["view_model"]["jobs"]) == {"backend"}
    assert restored["lifecycle"]["jobs"][0]["stable_id"] == original_id
    assert restored["lifecycle"]["jobs"][0]["first_seen"] == OBSERVED_ON
    assert restored["lifecycle"]["jobs"][0]["last_seen"] == OBSERVED_ON
    assert restored["view_model"]["tracking"]["statuses"] == {}
    assert store.read("applications")["items"] == []


def test_partial_scan_preserves_failed_source_history_without_showing_retained_low_scores(tmp_path):
    store = LocalTrackingStore(tmp_path / "state")
    history = [
        {**_job("old-high"), "score": 55},
        {**_job("old-low", title="Operations Coordinator"), "score": 40},
        {**_job("healthy-history", source="ashby"), "score": 55},
    ]
    _seed(store, *history)

    result = _run(store, _scan(_job("new-high"), incomplete=True))

    persisted = {job["external_id"]: job for job in store.read("lifecycle")["items"]}
    assert set(persisted) == {"old-high", "old-low", "healthy-history", "new-high"}
    assert _ids(result["scan"]["jobs"]) == {"new-high"}
    assert _ids(result["view_model"]["jobs"]) == {"old-high", "healthy-history", "new-high"}
    assert result["counts"] == {"scanned": 1, "published": 3}
    assert persisted["old-high"]["freshness"] == "active"
    assert persisted["old-low"]["freshness"] == "active"
    assert persisted["healthy-history"]["freshness"] == "stale"
    for external_id in ("old-high", "old-low", "healthy-history"):
        assert persisted[external_id]["last_seen"] == "2026-10-01"
    assert persisted["new-high"]["last_seen"] == OBSERVED_ON
    assert result["view_model"]["scan"]["state"] == "partial"


def test_empty_complete_scan_keeps_but_hides_low_history_and_does_not_refresh_it(tmp_path):
    store = LocalTrackingStore(tmp_path / "state")
    _seed(
        store, {**_job("old-high"), "score": 55},
        {**_job("old-low", title="Operations Coordinator"), "score": 40},
    )

    result = _run(store, _scan())

    assert result["scan"]["jobs"] == []
    assert _ids(result["lifecycle"]["jobs"]) == {"old-high", "old-low"}
    assert _ids(result["view_model"]["jobs"]) == {"old-high"}
    assert result["counts"] == {"scanned": 0, "published": 1}
    assert all(job["last_seen"] == "2026-10-01" for job in result["lifecycle"]["jobs"])
    assert all(job["freshness"] == "stale" for job in result["lifecycle"]["jobs"])


@pytest.mark.parametrize("change", ["company-excluded", "keyword-excluded", "role-changed"])
def test_retained_high_cached_score_cannot_bypass_current_profile_in_partial_scan(tmp_path, change):
    store = LocalTrackingStore(tmp_path / "state")
    historical = {
        **_job("history", description="Legacy requirements sentinel."),
        "score": 95, "summary": "Previous curated recommendation.",
        "risk": "Previous curated risk.",
    }
    _seed(store, historical)
    original = deepcopy(store.read("lifecycle")["items"][0])
    config = _config()
    current = {**_job("current"), "company": "Eligible Systems"}
    if change == "company-excluded":
        config = replace(config, companies=Companies((), ("Invented Systems",)))
    elif change == "keyword-excluded":
        config = replace(config, matching=Matching(50, (), ("Legacy requirements sentinel",)))
    else:
        config = _config(role="operations")
        current["title"] = "Operations Coordinator"
    evidence = tmp_path / "current-profile-review.json"

    result = _run(
        store, _scan(current, incomplete=True), config=config,
        review_runner=OfflineReviewRunner(evidence, _review_result()),
    )

    assert _ids(result["view_model"]["jobs"]) == {"current"}
    assert _ids(json.loads(evidence.read_text(encoding="utf-8"))["jobs"]) == {"current"}
    assert result["counts"] == {"scanned": 1, "published": 1}
    retained = next(job for job in store.read("lifecycle")["items"] if job["external_id"] == "history")
    assert retained == original
    assert retained["last_seen"] == "2026-10-01"


def test_retained_job_can_become_eligible_under_current_profile_without_becoming_a_new_observation(tmp_path):
    store = LocalTrackingStore(tmp_path / "state")
    historical = {**_job("history", title="Operations Coordinator"), "score": 40}
    _seed(store, historical)
    original = deepcopy(store.read("lifecycle")["items"][0])

    result = _run(store, _scan(incomplete=True), config=_config(role="operations"))

    assert _ids(result["view_model"]["jobs"]) == {"history"}
    assert result["view_model"]["jobs"][0]["score"] == 55
    assert result["view_model"]["jobs"][0]["last_seen"] == "2026-10-01"
    assert result["scan"]["jobs"] == []
    assert result["counts"] == {"scanned": 0, "published": 1}
    assert LocalTrackingStore(store.root).read("lifecycle")["items"] == [original]


@pytest.mark.parametrize(("threshold", "expected"), [
    (0, {"zero", "below", "boundary", "above", "maximum"}),
    (50, {"boundary", "above", "maximum"}),
    (100, {"maximum"}),
])
def test_display_threshold_is_inclusive_while_all_scores_are_persisted(tmp_path, threshold, expected):
    jobs = [
        {**_job(external_id), "score": score}
        for external_id, score in [("zero", 0), ("below", 49), ("boundary", 50), ("above", 51), ("maximum", 100)]
    ]
    store = LocalTrackingStore(tmp_path / "state")

    result = _run(store, _scan(*jobs), config=_config(threshold), ai_scorer=lambda items, config: list(items))

    assert _ids(result["view_model"]["jobs"]) == expected
    assert _ids(result["scan"]["jobs"]) == {"zero", "below", "boundary", "above", "maximum"}
    assert _ids(store.read("lifecycle")["items"]) == {"zero", "below", "boundary", "above", "maximum"}
    assert result["counts"] == {"scanned": 5, "published": len(expected)}


def test_external_review_receives_only_current_and_retained_eligible_jobs(tmp_path):
    store = LocalTrackingStore(tmp_path / "state")
    _seed(
        store, {**_job("old-high"), "score": 55},
        {**_job("old-low", title="Operations Coordinator"), "score": 40},
    )
    evidence = tmp_path / "review-input.json"
    report = _review_result()

    result = _run(
        store, _scan(_job("new-high"), _job("new-low", title="Operations Coordinator")),
        review_runner=OfflineReviewRunner(evidence, report),
    )

    reviewed = json.loads(evidence.read_text(encoding="utf-8"))
    assert _ids(reviewed["jobs"]) == {"old-high", "new-high"}
    assert reviewed["pid"] != os.getpid()
    assert reviewed["observed_on"] == OBSERVED_ON
    assert _ids(result["view_model"]["jobs"]) == {"old-high", "new-high"}
    assert _ids(store.read("lifecycle")["items"]) == {"old-high", "old-low", "new-high", "new-low"}
    assert result["review_result"] == report
    assert result["review"] == {"mode": "configured", "provider_calls": 0}


@pytest.mark.parametrize(("failures", "terminal"), [
    ([], "complete"),
    ([{"stable_id": "synthetic", "category": "provider_timeout"}], "partial"),
])
def test_review_stage_callback_runs_in_parent_and_preserves_report_shape(tmp_path, failures, terminal):
    parent_pid = os.getpid()
    events = []

    def on_stage(stage, status):
        assert os.getpid() == parent_pid
        events.append((stage, status))

    evidence = tmp_path / "runner.json"
    report = _review_result(failures=failures)
    result = _run(
        LocalTrackingStore(tmp_path / "state"), _scan(_job("visible")),
        review_runner=OfflineReviewRunner(evidence, report), on_stage=on_stage,
    )

    assert events == [("review", "running"), ("review", terminal)]
    assert json.loads(evidence.read_text(encoding="utf-8"))["pid"] != parent_pid
    assert result["review_result"] == report
    assert result["review"] == {"mode": "configured", "provider_calls": 0}


def test_disabled_review_reports_not_configured_without_changing_legacy_shape(tmp_path):
    events = []
    result = _run(
        LocalTrackingStore(tmp_path / "state"), _scan(_job("visible")),
        on_stage=lambda stage, status: events.append((stage, status)),
    )

    assert events == [("review", "not_configured")]
    assert result["review_result"] == _review_result()
    assert result["review"] == {"mode": "disabled", "provider_calls": 0}


@pytest.mark.parametrize("invalid", [
    None,
    {"contract_version": 2},
    {**_review_result(), "rejected": "invalid"},
    {**_review_result(), "rejected_sample": None},
    {**_review_result(), "failures": "invalid"},
])
def test_invalid_review_result_reports_failed_and_retains_complete_observations(tmp_path, invalid):
    events = []
    store = LocalTrackingStore(tmp_path / "state")

    with pytest.raises(ValueError, match="review"):
        _run(
            store, _scan(_job("visible"), _job("hidden", title="Operations Coordinator")),
            review_runner=OfflineReviewRunner(tmp_path / "runner.json", invalid),
            on_stage=lambda stage, status: events.append((stage, status)),
        )

    assert events == [("review", "running"), ("review", "failed")]
    assert _ids(LocalTrackingStore(store.root).read("lifecycle")["items"]) == {"visible", "hidden"}


def test_review_runner_exception_reports_failed_in_parent_after_persistence(tmp_path):
    events = []
    store = LocalTrackingStore(tmp_path / "state")
    parent_pid = os.getpid()

    def on_stage(stage, status):
        assert os.getpid() == parent_pid
        events.append((stage, status))

    with pytest.raises(RuntimeError, match="review runner failed: ValueError"):
        _run(
            store, _scan(_job("hidden", title="Operations Coordinator")),
            review_runner=OfflineReviewRunner(tmp_path / "runner.json", None, raise_error=True),
            on_stage=on_stage,
        )

    assert events == [("review", "running"), ("review", "failed")]
    assert _ids(store.read("lifecycle")["items"]) == {"hidden"}
