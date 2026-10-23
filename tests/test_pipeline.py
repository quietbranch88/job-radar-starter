import json
from pathlib import Path

from job_radar.catalog import load_catalog
from job_radar.config import load_user_config
from job_radar.legacy_pipeline import run_pipeline


def test_run_pipeline_scans_scores_writes_json_and_builds_dashboard(tmp_path: Path):
    catalog_file = tmp_path / "catalog.yaml"
    catalog_file.write_text(
        """
countries:
  CA:
    name: Canada
    sources: [greenhouse]
sources:
  greenhouse:
    kind: official_api
    terms_url: https://developer.greenhouse.io/job-board.html
    enabled: true
companies:
  Northstar Robotics:
    countries: [CA]
    source: greenhouse
    board: northstar
""".strip(),
        encoding="utf-8",
    )
    profile_file = tmp_path / "profile.yaml"
    profile_file.write_text(
        """
profile:
  skills: [Go]
preferences:
  countries: [CA]
  roles: [backend]
  tracks: [backend]
  visa_required: false
companies:
  preferred: [Northstar Robotics]
matching:
  minimum_score: 50
  must_have: [backend]
""".strip(),
        encoding="utf-8",
    )

    def fake_get_json(url):
        assert url == "https://boards-api.greenhouse.io/v1/boards/northstar/jobs?content=true"
        return {
            "jobs": [
                {
                    "id": 17,
                    "title": "Senior Go Backend Engineer",
                    "absolute_url": "https://example.com/jobs/17",
                    "location": {"name": "Toronto, Canada"},
                    "updated_at": "2026-07-15T00:00:00Z",
                }
            ]
        }

    jobs_out = tmp_path / "scans" / "latest.json"
    dashboard_out = tmp_path / "dashboard" / "index.html"
    rendered = {}

    def fake_dashboard_renderer(jobs, output):
        rendered["jobs"] = list(jobs)
        rendered["output"] = output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            "<html><body><div id=\"swipeDeck\">Senior Go Backend Engineer</div></body></html>",
            encoding="utf-8",
        )

    result = run_pipeline(
        load_catalog(catalog_file),
        load_user_config(profile_file),
        get_json=fake_get_json,
        jobs_output=jobs_out,
        dashboard_output=dashboard_out,
        dashboard_renderer=fake_dashboard_renderer,
    )

    jobs = json.loads(jobs_out.read_text(encoding="utf-8"))
    assert result == {"scanned": 1, "published": 1}
    assert jobs[0]["country"] == "CA"
    assert jobs[0]["score"] >= 50
    assert jobs[0]["external_id"] == "17"
    assert rendered["output"] == dashboard_out
    assert rendered["jobs"][0].title == "Senior Go Backend Engineer"
    html = dashboard_out.read_text(encoding="utf-8")
    assert "Senior Go Backend Engineer" in html
    assert 'id="swipeDeck"' in html
