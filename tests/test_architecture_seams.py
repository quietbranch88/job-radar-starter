from pathlib import Path

from job_radar.adapters import Job
from job_radar.application import score_jobs
from job_radar.catalog import Catalog, Company, Source
from job_radar.config import Companies, Matching, Preferences, Profile, UserConfig
from job_radar.dashboard import render_dashboard, render_dashboard_view_model
from job_radar.domain.tracking import dashboard_marker
from job_radar.posting_country import posting_country, scope_country
from job_radar.source_orchestration import scan_catalog_resilient


def test_one_market_stays_on_the_posting_and_several_follow_location():
    assert scope_country(("CA",)) == "CA"
    assert scope_country(("US", "SG")) == "SG,US"
    assert posting_country(("CA",), "Toronto") == "CA"
    assert posting_country(("SG", "US"), "Singapore") == "SG"
    assert posting_country(("SG", "US"), "New York") == ""
    assert posting_country("SG,US", "United States") == "US"


def test_browser_markers_fold_application_states():
    assert dashboard_marker("hidden") == "skip"
    assert dashboard_marker("Offer") == "applied"
    assert dashboard_marker("withdrawn") == "skip"
    assert dashboard_marker("dead") == "dead"


def test_empty_country_can_match_a_preferred_location(tmp_path: Path):
    config = UserConfig(
        profile=Profile(skills=(), resume_path=""),
        preferences=Preferences(countries=("SG",), roles=(), tracks=(), visa_required=False),
        companies=Companies(preferred=(), excluded=()),
        matching=Matching(minimum_score=0, must_have=(), exclude_keywords=()),
    )
    scored = score_jobs(
        [
            Job(
                source="ashby",
                external_id="1",
                company="Airwallex",
                title="Engineer",
                location="Singapore",
                url="https://example.test/jobs/1",
                published_at="2026-07-01",
                country="",
            )
        ],
        config,
    )
    assert "country" in scored[0].summary


def test_inline_scan_uses_the_same_result_contract_as_a_resilient_scan(tmp_path: Path):
    catalog = Catalog(
        countries={},
        sources={"ashby": Source("official_api", "https://example.test/terms", True)},
        companies={
            "Airwallex": Company(("SG", "US"), "ashby", "airwallex"),
            "Broken": Company(("US",), "ashby", "broken"),
        },
    )

    def get_json(url: str):
        if url.endswith("/broken"):
            raise RuntimeError("down")
        return {
            "jobs": [
                {
                    "id": "sg-1",
                    "title": "Engineer",
                    "jobUrl": "https://example.test/jobs/sg-1",
                    "location": "Singapore",
                    "isListed": True,
                }
            ]
        }

    result = scan_catalog_resilient(
        catalog,
        get_json,
        mode="best-effort",
        per_source_timeout=5,
        output=tmp_path / "latest.json",
        inline=True,
    )

    assert result["contract_version"] == 1
    assert result["incomplete"] is True
    assert result["failures"][0]["company"] == "Broken"
    assert result["failures"][0]["category"] == "source_error"
    assert result["jobs"][0]["country"] == "SG"
    assert (tmp_path / "latest.json").exists()


def test_removed_helpers_stay_out_of_the_live_modules():
    import job_radar.ai_review as ai_review
    import job_radar.dashboard as dashboard
    import job_radar.domain.tracking as tracking
    import job_radar.windows_job as windows_job

    assert not hasattr(dashboard, "_job_card")
    assert not hasattr(tracking, "_TERMINAL_STATUSES")
    assert not hasattr(ai_review, "TimedReviewProvider")
    assert not hasattr(windows_job.WindowsJob, "assign_current_process")


def test_dashboard_leaves_sync_off_unless_asked(tmp_path: Path):
    job = Job(
        source="greenhouse",
        external_id="7",
        company="Example",
        title="Backend Engineer",
        location="Toronto",
        url="https://example.com/jobs/7",
        published_at="2026-07-13T00:00:00Z",
        score=84,
        country="CA",
    )
    plain = tmp_path / "plain.html"
    render_dashboard([job], plain)
    html = plain.read_text(encoding="utf-8")
    assert "data-dashboard-optional-sync" not in html
    assert "Operator review: pending" not in html
    render_dashboard_view_model(
        {
            "contract_version": 1,
            "scan": {
                "state": "complete",
                "mode": "atomic",
                "incomplete": False,
                "observed_on": "",
                "failures": [],
            },
            "jobs": [
                {
                    "source": "greenhouse",
                    "external_id": "7",
                    "company": "Example",
                    "title": "Backend Engineer",
                    "location": "Toronto",
                    "url": "https://example.com/jobs/7",
                    "stable_id": "job_7",
                    "score": 84,
                    "country": "CA",
                }
            ],
            "review": {"rejected": [], "sampled_rejected_ids": []},
            "tracking": {
                "statuses": {},
                "metrics": {
                    "contract_version": 1,
                    "total": 0,
                    "funnel": {},
                    "rejection_stages": {},
                    "slices": {"resume_version": {}, "channel": {}, "country": {}},
                },
                "due_actions": [],
            },
        },
        tmp_path / "sync.html",
        install_sync=True,
    )
    assert 'data-dashboard-optional-sync="1"' in (tmp_path / "sync.html").read_text(encoding="utf-8")
