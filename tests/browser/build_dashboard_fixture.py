from __future__ import annotations

import sys
from pathlib import Path

from job_radar.dashboard import render_dashboard_view_model


def job(stable_id: str, freshness: str, score: int) -> dict[str, object]:
    external_id = stable_id.removeprefix("job_")
    return {
        "source": "greenhouse",
        "external_id": external_id,
        "stable_id": stable_id,
        "legacy_status_keys": [
            f"greenhouse:{external_id}",
            f"https://example.test/jobs/{stable_id}",
        ],
        "company": "Example Systems",
        "title": f"{freshness.title()} Backend Engineer",
        "location": "Toronto, Canada",
        "url": f"https://example.test/jobs/{stable_id}",
        "published_at": "2026-07-01T00:00:00Z",
        "score": score,
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


MODEL = {
    "contract_version": 1,
    "scan": {
        "state": "partial",
        "mode": "best-effort",
        "incomplete": True,
        "observed_on": "2026-07-17",
        "failures": [
            {
                "source": "greenhouse",
                "company": "Invented Labs",
                "category": "timeout",
                "message": "source exceeded 2 seconds",
            }
        ],
    },
    "jobs": [
        job("job_active", "active", 91),
        job("job_stale", "stale", 82),
        job("job_expired", "expired", 73),
        {
            **job("job_remote_match", "active", 64),
            "company": "Monzo",
            "title": "Remote Match Backend Engineer",
            "location": "Cardiff, London or Remote (UK)",
            "country": "GB",
            "summary": "Matched local preferences: country: REMOTE; role: backend",
        },
    ],
    "review": {
        "rejected": [
            {
                "stable_id": "job_stale",
                "reason_codes": ["skill_gap"],
                "local_fit": 58,
                "country": "CA",
                "hard_excluded": False,
                "observed_on": "2026-07-17",
                "rescued": False,
            }
        ],
        "sampled_rejected_ids": ["job_stale"],
    },
    "tracking": {
        "statuses": {},
        "metrics": {
            "contract_version": 1,
            "total": 4,
            "funnel": {"applied": 2, "interview": 1, "rejected": 1},
            "rejection_stages": {"screen": 1},
            "slices": {"resume_version": {}, "channel": {}, "country": {}},
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


if __name__ == "__main__":
    model = MODEL
    if len(sys.argv) > 2 and sys.argv[2] == "--large":
        large_jobs = [
            job(f"job_large_{index:03d}", "active", 100 - (index % 40))
            for index in range(125)
        ]
        for index, item in enumerate(large_jobs):
            item["title"] = f"Large Backend Engineer {index}"
        model = {
            **MODEL,
            "jobs": large_jobs,
            "review": {
                "rejected": [
                    {
                        "stable_id": item["stable_id"],
                        "reason_codes": ["below_minimum_fit"],
                        "local_fit": 50,
                        "country": "CA",
                        "hard_excluded": False,
                        "observed_on": "2026-07-17",
                        "rescued": False,
                    }
                    for item in large_jobs
                ],
                "sampled_rejected_ids": [],
            },
        }
    render_dashboard_view_model(model, Path(sys.argv[1]).resolve())
