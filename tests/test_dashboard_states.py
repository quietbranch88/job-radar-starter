from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


@pytest.fixture()
def dashboard() -> ModuleType:
    return importlib.import_module("job_radar.dashboard")


def _job(
    stable_id: str,
    *,
    freshness: str = "active",
    title: str = "Backend Engineer",
    company: str = "Example Systems",
) -> dict[str, Any]:
    return {
        "source": "greenhouse",
        "external_id": stable_id.removeprefix("job_"),
        "stable_id": stable_id,
        "legacy_status_keys": [
            f"greenhouse:{stable_id.removeprefix('job_')}",
            f"https://example.test/jobs/{stable_id}",
        ],
        "company": company,
        "title": title,
        "location": "Toronto, Canada",
        "url": f"https://example.test/jobs/{stable_id}",
        "published_at": "2026-07-01T00:00:00Z",
        "score": 84,
        "country": "CA",
        "category": "backend",
        "summary": "Relevant production backend scope.",
        "risk": "Verify work authorization.",
        "salary": "CAD 120k-150k",
        "tracks": ["backend"],
        "skills": ["Python", "PostgreSQL"],
        "first_seen": "2026-07-01",
        "last_seen": "2026-07-17",
        "freshness": freshness,
        "visa_supported": None,
    }


def _view_model(
    *,
    state: str = "complete",
    incomplete: bool = False,
    failures: list[dict[str, str]] | None = None,
    jobs: list[dict[str, Any]] | None = None,
    rejected: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    jobs = jobs if jobs is not None else [_job("job_01")]
    return {
        "contract_version": 1,
        "scan": {
            "state": state,
            "mode": "best-effort" if incomplete else "atomic",
            "incomplete": incomplete,
            "observed_on": "2026-07-17",
            "failures": failures or [],
        },
        "jobs": jobs,
        "review": {
            "rejected": rejected or [],
            "sampled_rejected_ids": [
                item["stable_id"] for item in (rejected or [])
            ],
        },
        "tracking": {
            "statuses": {"job_01": "interested"},
            "metrics": {
                "contract_version": 1,
                "total": 4,
                "funnel": {"applied": 2, "interview": 1, "rejected": 1},
                "rejection_stages": {"screen": 1},
                "slices": {
                    "resume_version": {},
                    "channel": {},
                    "country": {},
                },
            },
            "due_actions": [
                {
                    "action": "interview_thank_you",
                    "application_id": "app_01",
                    "due_at": "2026-07-17T18:00:00Z",
                    "priority": "normal",
                }
            ],
        },
    }


def _render(
    dashboard: ModuleType,
    tmp_path: Path,
    view_model: dict[str, Any],
) -> str:
    output = tmp_path / "index.html"
    dashboard.render_dashboard_view_model(view_model, output)
    return output.read_text(encoding="utf-8")


def _embedded_json(html: str, element_id: str) -> dict[str, Any]:
    marker = f'<script type="application/json" id="{element_id}">'
    start = html.index(marker) + len(marker)
    end = html.index("</script>", start)
    return json.loads(html[start:end].replace("<\\/", "</"))


def test_versioned_view_model_is_embedded_and_rejects_unknown_contract(
    dashboard: ModuleType,
    tmp_path: Path,
):
    model = _view_model()
    html = _render(dashboard, tmp_path, model)

    embedded = _embedded_json(html, "dashboard-data")
    assert embedded["contract_version"] == model["contract_version"]
    assert embedded["scan"] == model["scan"]
    assert embedded["tracking"] == model["tracking"]
    assert embedded["jobs"][0] == {
        **model["jobs"][0],
        "legacy_status_keys": [],
    }
    assert embedded["review"] == model["review"]
    assert 'data-dashboard-contract="1"' in html

    with pytest.raises(ValueError, match="contract.version|unsupported"):
        _render(dashboard, tmp_path, {**model, "contract_version": 99})


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("loading", "Loading today&#x27;s scan"),
        ("complete", "Scan complete"),
    ],
)
def test_loading_and_complete_states_are_visible_and_accessible(
    dashboard: ModuleType,
    tmp_path: Path,
    state: str,
    expected: str,
):
    html = _render(dashboard, tmp_path, _view_model(state=state))

    assert expected in html
    assert 'id="scanState"' in html
    assert 'aria-live="polite"' in html
    assert f'data-scan-state="{state}"' in html
    if state == "loading":
        assert 'aria-busy="true"' in html
        assert "loading-skeleton" in html


def test_partial_scan_names_failed_sources_without_claiming_complete_success(
    dashboard: ModuleType,
    tmp_path: Path,
):
    html = _render(
        dashboard,
        tmp_path,
        _view_model(
            state="partial",
            incomplete=True,
            failures=[
                {
                    "source": "greenhouse",
                    "company": "Invented Labs",
                    "category": "timeout",
                    "message": "source exceeded 2 seconds",
                }
            ],
        ),
    )

    assert "Partial scan" in html
    assert "Some official sources did not finish" in html
    assert "Invented Labs" in html
    assert "greenhouse" in html
    assert "source exceeded 2 seconds" in html
    assert 'data-scan-state="partial"' in html
    assert 'data-source-failure="timeout"' in html
    assert "Scan complete" not in html


def test_rejected_review_and_lifecycle_states_are_filterable_not_silently_hidden(
    dashboard: ModuleType,
    tmp_path: Path,
):
    rejected = {
        "stable_id": "job_rejected",
        "reason_codes": ["skill_gap", "visa_unknown"],
        "local_fit": 58,
        "country": "CA",
        "hard_excluded": False,
        "observed_on": "2026-07-17",
        "rescued": False,
    }
    html = _render(
        dashboard,
        tmp_path,
        _view_model(
            jobs=[
                _job("job_active"),
                _job("job_stale", freshness="stale"),
                _job("job_expired", freshness="expired"),
            ],
            rejected=[rejected],
        ),
    )

    assert 'data-freshness="stale"' in html
    assert 'data-freshness="expired"' in html
    assert 'data-review-state="rejected"' in html
    assert "skill_gap" in html
    assert "visa_unknown" in html
    for control in (
        'id="freshnessChips"',
        'id="reviewChips"',
        'data-value="stale"',
        'data-value="expired"',
        'data-value="rejected"',
    ):
        assert control in html


def test_tracking_summary_due_actions_and_export_are_local_only(
    dashboard: ModuleType,
    tmp_path: Path,
):
    html = _render(dashboard, tmp_path, _view_model())

    assert 'id="trackingSummary"' in html
    assert "4 tracked applications" in html
    assert "2 applied" in html
    assert "1 interview" in html
    assert "1 rejected" in html
    assert 'id="dueActions"' in html
    assert "Interview thank-you" in html
    assert "2026-07-17" in html
    assert 'id="exportDashboardState"' in html
    assert "job-radar-state.json" in html
    assert "statuses" in html
    assert "tracking" in html
    # Optional manual status sync may fetch, but exporting application tracking
    # remains a local Blob download and must never call that network boundary.
    export_handler = next(line for line in html.splitlines()
                          if "querySelector('#exportDashboardState').addEventListener" in line)
    assert "fetch(" not in export_handler
    assert "new Blob(" in export_handler
    assert "tracking:DASHBOARD.tracking" in export_handler
    assert "XMLHttpRequest" not in html
    assert "/api/statuses" not in html


def test_stable_status_key_migrates_legacy_local_state_and_preserves_interactions(
    dashboard: ModuleType,
    tmp_path: Path,
):
    html = _render(dashboard, tmp_path, _view_model())

    assert "function migrateLegacyState" in html
    assert "legacy_status_keys" in html
    assert "job.stable_id" in html
    assert "localStorage.getItem(STORAGE_KEY)" in html
    assert "localStorage.setItem(STORAGE_KEY" in html
    assert "job-radar-status" in html
    assert "pointerdown" in html
    assert "ArrowLeft" in html
    assert "ArrowRight" in html
    assert "ArrowUp" in html
    assert "slice(0,3)" in html
    for view in ("list", "deck", "matches"):
        assert f'data-view="{view}"' in html


def test_untrusted_dashboard_fields_are_safely_rendered(
    dashboard: ModuleType,
    tmp_path: Path,
):
    malicious = "</script><script>globalThis.PWNED=true</script>"
    failure = {
        "source": "greenhouse",
        "company": malicious,
        "category": "source_error",
        "message": malicious,
    }
    rejected = {
        "stable_id": "job_bad",
        "reason_codes": [malicious],
        "local_fit": 40,
        "country": "CA",
        "hard_excluded": False,
        "observed_on": "2026-07-17",
        "rescued": False,
    }
    html = _render(
        dashboard,
        tmp_path,
        _view_model(
            incomplete=True,
            state="partial",
            failures=[failure],
            jobs=[_job("job_bad", title=malicious, company=malicious)],
            rejected=[rejected],
        ),
    )

    assert "<script>globalThis.PWNED=true</script>" not in html
    assert "<\\/script>" in html
    assert "&lt;/script&gt;&lt;script&gt;globalThis.PWNED=true&lt;/script&gt;" in html
    assert "javascript:" not in html
    assert 'rel="noopener noreferrer"' in html


def test_mobile_contract_prevents_horizontal_overflow_and_keeps_controls_usable(
    dashboard: ModuleType,
    tmp_path: Path,
):
    html = _render(dashboard, tmp_path, _view_model())

    assert "@media(max-width:620px)" in html
    assert "max-width:100%" in html
    assert "overflow-x:hidden" in html
    assert ".command{margin-inline:0" in html
    assert ".view-switcher" in html and "width:100%" in html
    assert ".deck-actions" in html
    assert "min-height:44px" in html
    assert 'name="viewport"' in html
    assert 'content="width=device-width,initial-scale=1"' in html


def test_browser_qa_hooks_are_framework_free_and_deterministic(
    dashboard: ModuleType,
    tmp_path: Path,
):
    html = _render(dashboard, tmp_path, _view_model())

    for hook in (
        'data-testid="scan-state"',
        'data-testid="job-card"',
        'data-testid="tracking-summary"',
        'data-testid="due-actions"',
        'data-testid="export-state"',
    ):
        assert hook in html
    assert "react" not in html.casefold()
    assert "vue" not in html.casefold()
    assert "angular" not in html.casefold()
    assert "document.addEventListener('keydown'" in html


def test_large_dashboard_uses_one_canonical_payload_and_bounded_initial_render(
    dashboard: ModuleType,
    tmp_path: Path,
):
    jobs = [
        _job(
            f"job_{index:05d}",
            title=f"Backend Engineer {index}",
            company=f"Example Systems {index % 50}",
        )
        for index in range(125)
    ]
    html = _render(dashboard, tmp_path, _view_model(jobs=jobs))

    embedded_jobs = _embedded_json(html, "dashboard-data")["jobs"]
    assert embedded_jobs == [
        {**job, "legacy_status_keys": []}
        for job in jobs
    ]
    assert "const JOBS=DASHBOARD.jobs;" in html
    assert "const JOBS=[" not in html
    assert html.count("Backend Engineer 124") == 1
    server_html = html[: html.index('<script type="application/json"')]
    assert server_html.count('data-testid="job-card"') <= 50
    assert 'id="loadMoreJobs"' in html
    assert "const PAGE_SIZE=50" in html


def test_rejected_membership_is_indexed_once_instead_of_scanned_per_job(
    dashboard: ModuleType,
    tmp_path: Path,
):
    html = _render(
        dashboard,
        tmp_path,
        _view_model(
            jobs=[_job("job_rejected"), _job("job_accepted")],
            rejected=[
                {
                    "stable_id": "job_rejected",
                    "reason_codes": ["skill_gap"],
                    "local_fit": 58,
                    "country": "CA",
                    "hard_excluded": False,
                    "observed_on": "2026-07-17",
                    "rescued": False,
                }
            ],
        ),
    )

    assert "const rejectedIds=new Set(" in html
    assert "DASHBOARD.review.rejected.some" not in html


@pytest.mark.parametrize(
    ("job_count", "maximum_bytes"),
    [(1_000, 850_000), (5_000, 3_500_000)],
)
def test_large_dashboard_artifact_size_stays_bounded(
    dashboard: ModuleType,
    tmp_path: Path,
    job_count: int,
    maximum_bytes: int,
):
    output = tmp_path / f"dashboard-{job_count}.html"
    jobs = [
        _job(
            f"job_{index:05d}",
            title=f"Backend Engineer {index}",
            company=f"Example Systems {index % 50}",
        )
        for index in range(job_count)
    ]

    dashboard.render_dashboard_view_model(_view_model(jobs=jobs), output)

    assert output.stat().st_size < maximum_bytes


def test_large_rejected_audit_stays_bounded_and_keeps_client_access(
    dashboard: ModuleType,
    tmp_path: Path,
):
    output = tmp_path / "dashboard-5000-rejected.html"
    jobs = [
        _job(
            f"job_{index:05d}",
            title=f"Backend Engineer {index}",
            company=f"Example Systems {index % 50}",
        )
        for index in range(5_000)
    ]
    rejected = [
        {
            "stable_id": job["stable_id"],
            "reason_codes": ["below_minimum_fit"],
            "local_fit": 50,
            "country": "CA",
            "hard_excluded": False,
            "observed_on": "2026-07-17",
            "rescued": False,
        }
        for job in jobs
    ]

    dashboard.render_dashboard_view_model(
        _view_model(jobs=jobs, rejected=rejected),
        output,
    )

    html = output.read_text(encoding="utf-8")
    server_html = html[: html.index('<script type="application/json"')]
    assert output.stat().st_size < 3_500_000
    assert server_html.count('class="rejected-item"') <= 20
    assert 'id="loadMoreRejected"' in html
    assert "renderRejectedAudit" in html
