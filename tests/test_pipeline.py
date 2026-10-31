import json
from io import StringIO
from pathlib import Path

from job_radar.cli import main


def test_run_scans_scores_writes_json_and_builds_dashboard(tmp_path: Path):
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
    stdout = StringIO()
    code = main(
        [
            "--json",
            "run",
            "--catalog",
            str(catalog_file),
            "--profile",
            str(profile_file),
            "--jobs-output",
            str(jobs_out),
            "--dashboard-output",
            str(dashboard_out),
            "--user-data",
            str(tmp_path / "user-data"),
            "--aliases",
            str(tmp_path / "missing-aliases.json"),
            "--observed-on",
            "2026-07-15",
        ],
        get_json=fake_get_json,
        stdout=stdout,
    )

    report = json.loads(stdout.getvalue())
    payload = json.loads(jobs_out.read_text(encoding="utf-8"))
    job = payload["jobs"][0]
    html = dashboard_out.read_text(encoding="utf-8")
    assert code == 0
    assert report["counts"] == {"scanned": 1, "published": 1}
    assert job["country"] == "CA"
    assert job["score"] >= 50
    assert job["external_id"] == "17"
    assert "Senior Go Backend Engineer" in html
    assert 'id="swipeDeck"' in html
