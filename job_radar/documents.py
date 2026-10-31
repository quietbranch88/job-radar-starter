"""Read and write the jobs JSON envelopes the CLI accepts."""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path
from typing import Any

from job_radar.domain.jobs import Job
from job_radar.lifecycle_merge import observation_as_of


def atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def job_from_dict(item: Any) -> Job:
    if not isinstance(item, dict):
        raise ValueError("each jobs JSON entry must be an object")
    allowed = {field.name for field in fields(Job)}
    normalized = {key: value for key, value in item.items() if key in allowed}
    for field in ("tracks", "skills", "jd_evidence"):
        value = normalized.get(field, ())
        if not isinstance(value, (list, tuple)) or not all(
            isinstance(entry, str) for entry in value
        ):
            raise ValueError(f"job {field} must be a list of strings")
        normalized[field] = tuple(value)
    return Job(**normalized)


def job_items(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        items = raw
    elif (
        isinstance(raw, dict)
        and raw.get("contract_version") == 1
        and isinstance(raw.get("jobs"), list)
    ):
        items = raw["jobs"]
    else:
        raise ValueError(
            "jobs JSON must be a legacy list or versioned scan/lifecycle envelope"
        )
    if not all(isinstance(item, dict) for item in items):
        raise ValueError("jobs JSON entries must be objects")
    return items


def load_jobs(path: Path) -> list[Job]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [job_from_dict(item) for item in job_items(raw)]


def load_jobs_document(path: Path) -> tuple[str, Any, list[Job]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    jobs = load_jobs(path)
    if isinstance(raw, list):
        return "legacy", raw, jobs
    if set(raw) - {"observed_on", "latest_observed_on"} == {
        "contract_version",
        "mode",
        "incomplete",
        "jobs",
        "failures",
    }:
        if "latest_observed_on" in raw:
            if not isinstance(raw["latest_observed_on"], str):
                raise ValueError("latest_observed_on must be an ISO date")
            observation_as_of(
                raw.get("observed_on"),
                raw["jobs"],
                latest_observed_on=raw["latest_observed_on"],
            )
        return "scan", raw, jobs
    if set(raw) == {"contract_version", "jobs", "statuses"}:
        return "lifecycle", raw, jobs
    raise ValueError("jobs JSON has an unsupported versioned envelope")
