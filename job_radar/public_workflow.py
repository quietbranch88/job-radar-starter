from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, fields
from datetime import date
from typing import Any, Protocol

from job_radar.ai_review import (
    DEFAULT_MAX_FAST_CALLS,
    DEFAULT_PER_CALL_TIMEOUT_SECONDS,
    DEFAULT_TOTAL_DEADLINE_SECONDS,
)
from job_radar.application import SCORING_FIELDS, score_jobs
from job_radar.config import UserConfig
from job_radar.domain.jobs import Job
from job_radar.domain.tracking import dashboard_marker, stable_job_id
from job_radar.lifecycle_merge import merge_scan_state, observation_as_of
from job_radar.review_supervisor import run_review_runner_bounded
from job_radar.tracking_commands import build_metrics, due_actions


class WorkflowRepository(Protocol):
    def initialize(self) -> dict[str, Any]: ...

    def read(self, kind: str) -> dict[str, Any]: ...

    def write(self, kind: str, document: dict[str, Any]) -> Any: ...


def _document_items(repository: WorkflowRepository, kind: str) -> list[dict[str, Any]]:
    document = repository.read(kind)
    if (
        not isinstance(document, dict)
        or document.get("contract_version") != 1
        or set(document) != {"contract_version", "items"}
        or not isinstance(document["items"], list)
        or not all(isinstance(item, dict) for item in document["items"])
    ):
        raise ValueError(f"{kind} state must be a versioned items document")
    return document["items"]


def disabled_review_result() -> dict[str, Any]:
    return {
        "contract_version": 1,
        "reviews": [],
        "rejected": [],
        "rejected_sample": [],
        "failures": [],
        "company_facts_used": 0,
        "report": {
            "provider_calls": 0,
            "cache_hits": 0,
            "escalations": 0,
        },
    }


def _dashboard_statuses(applications: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for application in applications:
        job_id = application.get("job_id")
        status = application.get("status")
        if not isinstance(job_id, str) or not isinstance(status, str):
            continue
        try:
            statuses[job_id] = dashboard_marker(status)
        except ValueError:
            continue
    return dict(sorted(statuses.items()))


def project_application_tracking(
    repository: WorkflowRepository, *, as_of: str
) -> dict[str, Any]:
    """Project persisted decisions without scanning or recording a new observation."""
    return {
        "statuses": _dashboard_statuses(_document_items(repository, "applications")),
        "metrics": build_metrics(repository),
        "due_actions": due_actions(repository, as_of=as_of),
    }


def execute_public_workflow(
    *,
    scan_result: Mapping[str, Any],
    config: UserConfig,
    repository: WorkflowRepository,
    observed_on: str,
    aliases: Mapping[str, Any],
    ai_scorer: Callable[[Sequence[Job], UserConfig], list[Job]] | None = None,
    review_runner: Callable[..., dict[str, Any]] | None = None,
    per_call_timeout_seconds: float = DEFAULT_PER_CALL_TIMEOUT_SECONDS,
    total_deadline_seconds: float = DEFAULT_TOTAL_DEADLINE_SECONDS,
    max_fast_calls: int = DEFAULT_MAX_FAST_CALLS,
    on_stage: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """Persist all observations and project only threshold-eligible jobs.

    Counts distinguish current input observations (scanned) from the eligible
    lifecycle projection, including retained history (published).
    The optional stage observer runs in this parent process.
    """

    date.fromisoformat(observed_on)
    repository.initialize()
    if scan_result.get("contract_version") != 1:
        raise ValueError("scan result has unsupported contract version")
    raw_jobs = scan_result.get("jobs")
    if not isinstance(raw_jobs, list) or not all(
        isinstance(item, Mapping) for item in raw_jobs
    ):
        raise ValueError("scan result jobs must be objects")

    jobs = []
    for raw in raw_jobs:
        item = dict(raw)
        item["tracks"] = tuple(item.get("tracks", ()))
        item["skills"] = tuple(item.get("skills", ()))
        jobs.append(Job(**item))
    if ai_scorer is not None:
        scored = ai_scorer(jobs, config)
    else:
        scored = score_jobs(jobs, config)
    scored.sort(
        key=lambda job: (
            -(job.score or 0),
            job.company.casefold(),
            job.title.casefold(),
        )
    )
    scored_scan = {
        "contract_version": 1,
        "observed_on": observed_on,
        "mode": scan_result.get("mode"),
        "incomplete": scan_result.get("incomplete"),
        "jobs": [asdict(job) for job in scored],
        "failures": list(scan_result.get("failures", [])),
    }
    if "latest_observed_on" in scan_result:
        scored_scan["latest_observed_on"] = scan_result["latest_observed_on"]
    for item in scored_scan["jobs"]:
        item["tracks"] = list(item["tracks"])
        item["skills"] = list(item["skills"])

    prior_jobs = _document_items(repository, "lifecycle")
    as_of = observation_as_of(observed_on, scored_scan["jobs"],
                             latest_observed_on=scored_scan.get("latest_observed_on"))
    tracking = project_application_tracking(repository, as_of=f"{as_of}T23:59:59Z")
    application_statuses = tracking["statuses"]
    lifecycle = merge_scan_state(
        prior_state={
            "contract_version": 1,
            "jobs": prior_jobs,
            "statuses": {},
        },
        scan_result=scored_scan,
        observed_on=as_of,
        aliases=aliases,
        statuses=application_statuses,
    )
    # This run's scores win for jobs in the scan. Merge keeps curated fields for
    # history that was not observed again.
    scored_by_id: dict[str, dict[str, Any]] = {}
    identity_aliases = {**aliases["company_aliases"], **aliases["title_aliases"]}
    for item in sorted(
        scored_scan["jobs"],
        key=lambda item: tuple(
            item[field].casefold()
            for field in ("source", "external_id", "company", "title", "url")
        ),
    ):
        job_id = stable_job_id(
            source=item["source"], external_id=item["external_id"],
            company=item["company"], title=item["title"], aliases=identity_aliases,
        )
        scored_by_id[job_id] = item
    for item in lifecycle["jobs"]:
        current = scored_by_id.get(item["stable_id"])
        if current is not None:
            for field in SCORING_FIELDS:
                item[field] = current[field]
    repository.write(
        "lifecycle",
        {"contract_version": 1, "items": lifecycle["jobs"]},
    )
    retained = [job for job in lifecycle["jobs"] if job["stable_id"] not in scored_by_id]
    retained_scored = score_jobs(
        [
            Job(**{
                field.name: job[field.name]
                for field in fields(Job)
                if field.name in job
            })
            for job in retained
        ],
        config,
    )
    retained_projection = {}
    for job, rescored in zip(retained, retained_scored):
        projected = {**job, **asdict(rescored)}
        for field in ("tracks", "skills", "jd_evidence"):
            projected[field] = list(projected[field])
        retained_projection[job["stable_id"]] = projected
    # Historical curation and observation dates stay persisted as-is. Only the
    # projection uses current local preferences; it never calls an AI scorer.
    projected_jobs = [
        retained_projection.get(job["stable_id"], job) for job in lifecycle["jobs"]
    ]
    published = [
        job for job in projected_jobs
        if (job["score"] or 0) >= config.matching.minimum_score
    ]

    if review_runner is None:
        review = disabled_review_result()
        review_mode = "disabled"
        if on_stage is not None:
            on_stage("review", "not_configured")
    else:
        if on_stage is not None:
            on_stage("review", "running")
        try:
            review = run_review_runner_bounded(
                review_runner,
                published,
                observed_on=as_of,
                repository=repository,
                per_call_timeout_seconds=per_call_timeout_seconds,
                total_deadline_seconds=total_deadline_seconds,
                max_fast_calls=max_fast_calls,
            )
            if not isinstance(review, dict) or review.get("contract_version") != 1:
                raise ValueError("review runner must return a versioned review result")
            if not isinstance(review.get("rejected", []), list) or not isinstance(
                review.get("rejected_sample", []), list
            ):
                raise ValueError("review result rejected fields must be lists")
            if not isinstance(review.get("failures", []), list):
                raise ValueError("review result failures must be a list")
        except BaseException:
            if on_stage is not None:
                on_stage("review", "failed")
            raise
        review_mode = "configured"
        if on_stage is not None:
            on_stage("review", "partial" if review.get("failures") else "complete")

    rejected = review.get("rejected", [])
    sampled = review.get("rejected_sample", [])
    sampled_ids = [
        item["stable_id"]
        for item in sampled
        if isinstance(item, dict) and isinstance(item.get("stable_id"), str)
    ]
    failures = scored_scan["failures"]
    view_model = {
        "contract_version": 1,
        "scan": {
            "state": "partial" if scored_scan["incomplete"] else "complete",
            "mode": scored_scan["mode"],
            "incomplete": scored_scan["incomplete"],
            "observed_on": observed_on,
            "failures": failures,
        },
        "jobs": published,
        "review": {
            "rejected": rejected,
            "sampled_rejected_ids": sampled_ids,
        },
        "tracking": {
            "statuses": lifecycle["statuses"],
            "metrics": tracking["metrics"],
            "due_actions": tracking["due_actions"],
        },
    }
    report = review.get("report", {})
    provider_calls = (
        report.get("provider_calls", 0) if isinstance(report, dict) else 0
    )
    return {
        "scan": scored_scan,
        "lifecycle": lifecycle,
        "review_result": review,
        "review": {
            "mode": review_mode,
            "provider_calls": provider_calls,
        },
        "view_model": view_model,
        "counts": {
            "scanned": len(jobs),
            "published": len(published),
        },
    }


def empty_tracking() -> dict[str, Any]:
    return {
        "statuses": {},
        "metrics": {
            "contract_version": 1,
            "total": 0,
            "funnel": {},
            "rejection_stages": {},
            "slices": {
                "resume_version": {},
                "channel": {},
                "country": {},
            },
        },
        "due_actions": [],
    }


def view_model_from_document(kind: str, raw: Any) -> dict[str, Any]:
    if kind == "lifecycle":
        return {
            "contract_version": 1,
            "scan": {
                "state": "complete",
                "mode": "atomic",
                "incomplete": False,
                "observed_on": "",
                "failures": [],
            },
            "jobs": raw["jobs"],
            "review": {"rejected": [], "sampled_rejected_ids": []},
            "tracking": {**empty_tracking(), "statuses": raw["statuses"]},
        }
    if kind == "scan":
        observed_on = raw.get("observed_on", "")
        lifecycle = merge_scan_state(
            prior_state={"contract_version": 1, "jobs": [], "statuses": {}},
            scan_result=raw,
            observed_on=observation_as_of(
                observed_on,
                raw["jobs"],
                latest_observed_on=raw.get("latest_observed_on"),
            ),
            aliases={
                "contract_version": 1,
                "company_aliases": {},
                "title_aliases": {},
            },
            statuses={},
        ) if observed_on else {
            "jobs": [
                {
                    **item,
                    "stable_id": stable_job_id(
                        source=item["source"],
                        external_id=item["external_id"],
                        company=item["company"],
                        title=item["title"],
                    ),
                    "last_seen": "",
                    "freshness": "active",
                    "legacy_status_keys": [],
                }
                for item in raw["jobs"]
            ],
            "statuses": {},
        }
        return {
            "contract_version": 1,
            "scan": {
                "state": "partial" if raw["incomplete"] else "complete",
                "mode": raw["mode"],
                "incomplete": raw["incomplete"],
                "observed_on": observed_on,
                "failures": raw["failures"],
            },
            "jobs": lifecycle["jobs"],
            "review": {"rejected": [], "sampled_rejected_ids": []},
            "tracking": {**empty_tracking(), "statuses": lifecycle["statuses"]},
        }
    raise ValueError("legacy input does not have a versioned Dashboard view model")
