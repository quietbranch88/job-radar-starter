from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from job_radar.cli import main
from job_radar.documents import load_jobs
from job_radar.data.tracking_store import LocalTrackingStore
from job_radar.domain.tracking import stable_application_id, stable_job_id


class RecordingReviewRunner:
    def __init__(self, evidence_path: Path):
        self.evidence_path = evidence_path

    def __call__(
        self,
        jobs: list[dict[str, Any]],
        *,
        observed_on: str,
        repository: object,
        per_call_timeout_seconds: int,
        total_deadline_seconds: int,
        max_fast_calls: int,
    ) -> dict[str, Any]:
        self.evidence_path.write_text(
            json.dumps(
                {
                    "reviewed": [job["stable_id"] for job in jobs],
                    "observed_on": observed_on,
                    "repository_configured": repository is not None,
                    "limits": {
                        "per_call_timeout_seconds": per_call_timeout_seconds,
                        "total_deadline_seconds": total_deadline_seconds,
                        "max_fast_calls": max_fast_calls,
                    },
                }
            ),
            encoding="utf-8",
        )
        return {
            "contract_version": 1,
            "reviews": [],
            "rejected": [],
            "rejected_sample": [],
            "failures": [],
            "company_facts_used": 0,
            "report": {
                "provider_calls": 1,
                "cache_hits": 0,
                "escalations": 0,
            },
        }


def _job(**overrides: Any) -> dict[str, Any]:
    item = {
        "source": "greenhouse",
        "external_id": "backend-1",
        "company": "Example Systems",
        "title": "Backend Engineer",
        "location": "Toronto, Canada",
        "url": "https://example.com/jobs/backend-1",
        "published_at": "2026-07-17T00:00:00Z",
        "score": None,
        "country": "CA",
        "category": "backend",
        "summary": "",
        "risk": "",
        "salary": "",
        "tracks": ["backend"],
        "skills": ["Go"],
        "first_seen": "",
        "visa_supported": None,
    }
    item.update(overrides)
    return item


def _scan_envelope(*, incomplete: bool = False) -> dict[str, Any]:
    failures = (
        [
            {
                "source": "lever",
                "company": "Invented Labs",
                "category": "timeout",
                "message": "source timed out",
            }
        ]
        if incomplete
        else []
    )
    return {
        "contract_version": 1,
        "mode": "best-effort" if incomplete else "atomic",
        "incomplete": incomplete,
        "jobs": [_job()],
        "failures": failures,
    }


def _write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _profile(path: Path) -> Path:
    path.write_text(
        """
profile: {skills: [Go]}
preferences:
  countries: [CA]
  roles: [backend]
  tracks: [backend]
  visa_required: false
matching: {minimum_score: 50, must_have: [backend]}
""".strip(),
        encoding="utf-8",
    )
    return path


def _catalog(path: Path) -> Path:
    path.write_text(
        """
countries:
  CA: {name: Canada, sources: [greenhouse]}
sources:
  greenhouse:
    kind: official_api
    terms_url: https://developer.greenhouse.io/job-board.html
    enabled: true
companies:
  Example Systems:
    countries: [CA]
    source: greenhouse
    board: example
""".strip(),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    "payload",
    [
        [_job()],
        _scan_envelope(),
        {
            "contract_version": 1,
            "jobs": [
                _job(
                    stable_id="job_123",
                    last_seen="2026-07-17",
                    freshness="active",
                    legacy_status_keys=["greenhouse:backend-1"],
                )
            ],
            "statuses": {},
        },
    ],
)
def test_load_jobs_accepts_legacy_scan_and_lifecycle_contracts(
    tmp_path: Path,
    payload: object,
):
    loaded = load_jobs(_write_json(tmp_path / "jobs.json", payload))

    assert [(job.source, job.external_id) for job in loaded] == [
        ("greenhouse", "backend-1")
    ]


def test_score_and_build_dashboard_accept_the_resilient_scan_envelope(
    tmp_path: Path,
):
    source = _write_json(tmp_path / "scan.json", _scan_envelope(incomplete=True))
    scored = tmp_path / "scored.json"
    dashboard = tmp_path / "index.html"

    assert (
        main(
            [
                "score",
                "--jobs",
                str(source),
                "--profile",
                str(_profile(tmp_path / "profile.yaml")),
                "--output",
                str(scored),
            ]
        )
        == 0
    )
    scored_payload = json.loads(scored.read_text(encoding="utf-8"))
    assert scored_payload["contract_version"] == 1
    assert scored_payload["incomplete"] is True
    assert scored_payload["failures"][0]["category"] == "timeout"
    assert scored_payload["jobs"][0]["score"] >= 50

    assert (
        main(
            [
                "build-dashboard",
                "--jobs",
                str(scored),
                "--output",
                str(dashboard),
            ]
        )
        == 0
    )
    html = dashboard.read_text(encoding="utf-8")
    assert 'data-dashboard-contract="1"' in html
    assert "Partial scan" in html
    assert "source timed out" in html


def test_run_executes_one_versioned_local_workflow_with_disabled_review(
    tmp_path: Path,
):
    jobs_output = tmp_path / "scans" / "latest.json"
    dashboard_output = tmp_path / "dashboard" / "index.html"
    user_data = (tmp_path / "user-data").resolve()
    aliases = _write_json(
        tmp_path / "aliases.json",
        {"contract_version": 1, "company_aliases": {}, "title_aliases": {}},
    )

    def fake_scan(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return _scan_envelope()

    stdout = StringIO()
    status = main(
        [
            "--json",
            "run",
            "--catalog",
            str(_catalog(tmp_path / "catalog.yaml")),
            "--profile",
            str(_profile(tmp_path / "profile.yaml")),
            "--jobs-output",
            str(jobs_output),
            "--dashboard-output",
            str(dashboard_output),
            "--user-data",
            str(user_data),
            "--aliases",
            str(aliases),
            "--observed-on",
            "2026-07-17",
            "--mode",
            "atomic",
        ],
        scan_runner=fake_scan,
        stdout=stdout,
    )

    report = json.loads(stdout.getvalue())
    assert status == 0
    assert report["contract_version"] == 1
    assert report["workflow"] == [
        "scan",
        "score",
        "review",
        "lifecycle",
        "tracking",
        "dashboard",
    ]
    assert report["review"]["mode"] == "disabled"
    assert report["review"]["provider_calls"] == 0
    assert json.loads(jobs_output.read_text(encoding="utf-8"))["contract_version"] == 1
    lifecycle = json.loads(
        (user_data / "tracking" / "lifecycle.json").read_text(encoding="utf-8")
    )
    assert lifecycle["items"][0]["stable_id"]
    assert lifecycle["items"][0]["first_seen"] == "2026-07-17"
    assert "Scan complete" in dashboard_output.read_text(encoding="utf-8")


def test_release_gate_covers_run_export_and_delete(tmp_path: Path):
    user_data = (tmp_path / "user-data").resolve()
    export_output = tmp_path / "tracking-export.json"

    def fake_scan(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return _scan_envelope()

    common = [
        "--catalog",
        str(_catalog(tmp_path / "catalog.yaml")),
        "--profile",
        str(_profile(tmp_path / "profile.yaml")),
        "--jobs-output",
        str(tmp_path / "scan.json"),
        "--dashboard-output",
        str(tmp_path / "index.html"),
        "--user-data",
        str(user_data),
        "--aliases",
        str(
            _write_json(
                tmp_path / "aliases.json",
                {
                    "contract_version": 1,
                    "company_aliases": {},
                    "title_aliases": {},
                },
            )
        ),
        "--observed-on",
        "2026-07-17",
    ]
    assert main(["run", *common], scan_runner=fake_scan) == 0
    assert (
        main(
            [
                "tracking",
                "export",
                "--user-data",
                str(user_data),
                "--output",
                str(export_output),
            ]
        )
        == 0
    )
    exported = json.loads(export_output.read_text(encoding="utf-8"))
    assert exported["contract_version"] == 1
    assert exported["state"]["lifecycle"]["items"]

    assert main(["tracking", "delete", "--user-data", str(user_data)]) == 0
    assert not (user_data / "tracking").exists()
    assert export_output.is_file()


def test_run_uses_an_explicit_review_boundary_when_configured(tmp_path: Path):
    evidence_path = tmp_path / "review-evidence.json"

    def fake_scan(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return _scan_envelope()

    stdout = StringIO()
    status = main(
        [
            "--json",
            "run",
            "--catalog",
            str(_catalog(tmp_path / "catalog.yaml")),
            "--profile",
            str(_profile(tmp_path / "profile.yaml")),
            "--jobs-output",
            str(tmp_path / "scan.json"),
            "--dashboard-output",
            str(tmp_path / "index.html"),
            "--user-data",
            str((tmp_path / "user-data").resolve()),
            "--aliases",
            str(
                _write_json(
                    tmp_path / "aliases.json",
                    {
                        "contract_version": 1,
                        "company_aliases": {},
                        "title_aliases": {},
                    },
                )
            ),
            "--observed-on",
            "2026-07-17",
            "--per-call-timeout",
            "7",
            "--total-review-deadline",
            "19",
            "--max-fast-calls",
            "3",
        ],
        scan_runner=fake_scan,
        review_runner=RecordingReviewRunner(evidence_path),
        stdout=stdout,
    )

    report = json.loads(stdout.getvalue())
    assert status == 0
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["reviewed"]
    assert evidence["observed_on"] == "2026-07-17"
    assert evidence["repository_configured"] is True
    assert evidence["limits"] == {
        "per_call_timeout_seconds": 7,
        "total_deadline_seconds": 19,
        "max_fast_calls": 3,
    }
    assert report["review"] == {"mode": "configured", "provider_calls": 1}


def test_run_projects_existing_application_tracking_into_the_dashboard(
    tmp_path: Path,
):
    user_data = (tmp_path / "user-data").resolve()
    store = LocalTrackingStore(user_data)
    store.initialize()
    job_id = stable_job_id(
        source="greenhouse",
        external_id="backend-1",
        company="Example Systems",
        title="Backend Engineer",
    )
    store.write(
        "applications",
        {
            "contract_version": 1,
            "items": [
                {
                    "application_id": stable_application_id(job_id),
                    "job_id": job_id,
                    "company": "Example Systems",
                    "title": "Backend Engineer",
                    "status": "applied",
                    "applied_at": "2026-07-16T09:00:00Z",
                    "updated_at": "2026-07-16T09:00:00Z",
                    "resume_version": "public-example",
                    "channel": "official-site",
                    "country": "CA",
                }
            ],
        },
    )

    def fake_scan(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return _scan_envelope()

    dashboard = tmp_path / "index.html"
    status = main(
        [
            "run",
            "--catalog",
            str(_catalog(tmp_path / "catalog.yaml")),
            "--profile",
            str(_profile(tmp_path / "profile.yaml")),
            "--jobs-output",
            str(tmp_path / "scan.json"),
            "--dashboard-output",
            str(dashboard),
            "--user-data",
            str(user_data),
            "--aliases",
            str(
                _write_json(
                    tmp_path / "aliases.json",
                    {
                        "contract_version": 1,
                        "company_aliases": {},
                        "title_aliases": {},
                    },
                )
            ),
            "--observed-on",
            "2026-07-17",
        ],
        scan_runner=fake_scan,
    )

    html = dashboard.read_text(encoding="utf-8")
    assert status == 0
    assert "1 tracked applications" in html
    assert f'"statuses":{{"{job_id}":"applied"}}' in html


def test_tracking_cli_initializes_exports_and_deletes_only_local_state(
    tmp_path: Path,
):
    user_data = (tmp_path / "user-data").resolve()
    export = tmp_path / "exports" / "tracking.json"
    unrelated = user_data / "keep.txt"

    assert main(["tracking", "init", "--user-data", str(user_data)]) == 0
    unrelated.write_text("keep", encoding="utf-8")
    assert (
        main(
            [
                "tracking",
                "export",
                "--user-data",
                str(user_data),
                "--output",
                str(export),
            ]
        )
        == 0
    )
    assert json.loads(export.read_text(encoding="utf-8"))["contract_version"] == 1
    assert main(["tracking", "delete", "--user-data", str(user_data)]) == 0
    assert unrelated.read_text(encoding="utf-8") == "keep"
