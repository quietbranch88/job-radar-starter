from pathlib import Path

from job_radar.adapters import Job
from job_radar.dashboard import render_dashboard


def test_dashboard_renders_normalized_jobs_without_private_profile(tmp_path: Path):
    output = tmp_path / "index.html"
    render_dashboard(
        [
            Job(
                source="greenhouse",
                external_id="7",
                company="Example & Co",
                title="Backend <Engineer>",
                location="Toronto",
                url="https://example.com/jobs/7",
                published_at="2026-07-13T00:00:00Z",
                score=84,
                country="CA",
                category="backend",
                summary="Matches the developer's selected backend skills.",
                risk="Verify work authorization requirements.",
                salary="CAD 120k–150k",
            )
        ],
        output,
    )

    html = output.read_text(encoding="utf-8")
    assert "Example &amp; Co" in html
    assert "Backend &lt;Engineer&gt;" in html
    assert "https://example.com/jobs/7" in html
    assert "greenhouse" in html
    assert "84" in html
    assert "Matches the developer&#x27;s selected backend skills." in html
    assert "Verify work authorization requirements." in html
    assert "CAD 120k–150k" in html
    assert 'data-country="CA"' in html
    assert 'data-category="backend"' in html
    assert "PRIVATE_OWNER_NAME" not in html
    assert "PRIVATE_DEPLOYMENT_HOST" not in html
    assert ".job-secret" not in html


def test_dashboard_has_a_useful_empty_state(tmp_path: Path):
    output = tmp_path / "index.html"
    render_dashboard([], output)

    html = output.read_text(encoding="utf-8")
    assert "No jobs in this scan" in html
    assert "user-data/sources.yaml" in html
    assert "job-radar run --catalog user-data/sources.yaml" in html
    assert "New here? Three things this page does" in html


def test_dashboard_rejects_non_http_job_links(tmp_path: Path):
    output = tmp_path / "index.html"
    render_dashboard(
        [Job("ashby", "bad", "Example", "Unsafe", "Remote", "javascript:alert(1)", "")],
        output,
    )

    html = output.read_text(encoding="utf-8")
    assert "javascript:alert" not in html
    assert 'aria-disabled="true"' in html


def test_dashboard_preserves_the_private_versions_triage_workflow_without_private_data(tmp_path: Path):
    output = tmp_path / "index.html"
    render_dashboard(
        [
            Job(
                source="greenhouse",
                external_id="northstar-backend",
                company="Northstar Robotics",
                title="Senior Backend Engineer",
                location="Toronto, Canada",
                url="https://example.com/jobs/northstar-backend",
                published_at="2026-07-14T00:00:00Z",
                score=91,
                country="CA",
                category="backend",
                summary="Strong backend and distributed-systems overlap.",
                risk="Confirm work authorization support.",
                salary="CAD 148k–176k",
                tracks=("backend",),
                skills=("Go", "Kubernetes"),
                first_seen="2026-07-14",
                visa_supported=True,
            ),
            Job(
                source="ashby",
                external_id="cedar-ai-platform",
                company="Cedarline Systems",
                title="AI Platform Engineer",
                location="Remote",
                url="https://example.com/jobs/cedar-ai-platform",
                published_at="2026-07-13T00:00:00Z",
                score=84,
                country="REMOTE",
                category="ai-platform",
                summary="Combines platform operations with applied AI delivery.",
                tracks=("ai", "backend"),
                skills=("Python", "LLM"),
                first_seen="2026-07-13",
                visa_supported=None,
            ),
        ],
        output,
    )

    html = output.read_text(encoding="utf-8")

    for view in ("list", "deck", "matches"):
        assert f'data-view="{view}"' in html
    for control in (
        'id="recommendations"',
        'id="countryChips"',
        'id="trackChips"',
        'id="categoryChips"',
        'id="skillChips"',
        'id="freshnessChips"',
        'id="sourceChips"',
        'id="statusChips"',
        'id="jobList"',
        'id="swipeDeck"',
        'id="matchesGrid"',
    ):
        assert control in html
    for state in ("interested", "applied", "skip", "dead"):
        assert f'data-state="{state}"' in html

    assert "slice(0,3)" in html
    assert "pointerdown" in html
    assert "ArrowLeft" in html
    assert "ArrowRight" in html
    assert "ArrowUp" in html
    assert "Export status" in html
    assert "job-radar-status" in html
    assert "[hidden]{display:none!important}" in html
    assert ".command{margin-inline:-11px}" in html
    assert "/api/statuses" not in html
    assert "PRIVATE_CREDENTIAL_MARKER" not in html
    assert "PRIVATE_DEPLOYMENT_HOST" not in html
    assert "PRIVATE_OWNER_NAME" not in html
