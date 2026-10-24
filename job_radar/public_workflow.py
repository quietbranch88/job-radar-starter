from __future__ import annotations

import subprocess
import multiprocessing
import os
import signal
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, fields
from datetime import date
from typing import Any, Protocol

from job_radar.ai_review import (
    DEFAULT_MAX_FAST_CALLS,
    DEFAULT_PER_CALL_TIMEOUT_SECONDS,
    DEFAULT_TOTAL_DEADLINE_SECONDS,
)
from job_radar.application import score_jobs
from job_radar.config import UserConfig
from job_radar.domain.jobs import Job
from job_radar.domain.tracking import stable_job_id
from job_radar.lifecycle_merge import merge_scan_state, observation_as_of
from job_radar.tracking_commands import build_metrics, due_actions
from job_radar.windows_job import WindowsJob
from job_radar.review_projection import ReceiptChecker, project_review_receipts, validate_resume_selection


_TASKKILL_TIMEOUT_SECONDS = 1
_WORKER_JOIN_TIMEOUT_SECONDS = 1


class WorkflowRepository(Protocol):
    def initialize(self) -> dict[str, Any]: ...

    def read(self, kind: str) -> dict[str, Any]: ...

    def write(self, kind: str, document: dict[str, Any]) -> Any: ...


def _review_runner_worker(
    connection: Any,
    start_gate: Any,
    review_runner: Callable[..., dict[str, Any]],
    jobs: list[dict[str, Any]],
    kwargs: dict[str, Any],
) -> None:
    if start_gate is not None:
        start_gate.wait()
        if os.path.normcase(sys._base_executable) != os.path.normcase(sys.executable):
            os.environ.pop("__PYVENV_LAUNCHER__", None)
            inherited_paths = [path for path in sys.path if path]
            existing_pythonpath = os.environ.get("PYTHONPATH")
            if existing_pythonpath:
                inherited_paths.append(existing_pythonpath)
            os.environ["PYTHONPATH"] = os.pathsep.join(inherited_paths)
            base_executable = os.path.join(sys.base_prefix, "python.exe")
            sys.executable = (
                base_executable
                if os.path.isfile(base_executable)
                else sys._base_executable
            )
    if os.name != "nt":
        os.setsid()
    try:
        connection.send(("ready", None))
        connection.send(("ok", review_runner(jobs, **kwargs)))
    except BaseException as error:
        connection.send(("error", type(error).__name__))
    finally:
        connection.close()


def _kill_worker_tree(process: Any, windows_job: WindowsJob | None = None) -> None:
    if windows_job is not None:
        windows_job.terminate()
        process.join(timeout=_WORKER_JOIN_TIMEOUT_SECONDS)
        if process.is_alive():
            process.kill()
            process.join(timeout=_WORKER_JOIN_TIMEOUT_SECONDS)
        windows_job.wait_empty(_WORKER_JOIN_TIMEOUT_SECONDS)
        return
    if not process.is_alive():
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
                text=True,
                timeout=_TASKKILL_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.join(timeout=_WORKER_JOIN_TIMEOUT_SECONDS)
    if process.is_alive():
        process.kill()
        process.join(timeout=_WORKER_JOIN_TIMEOUT_SECONDS)


def run_review_runner_bounded(
    review_runner: Callable[..., dict[str, Any]],
    jobs: list[dict[str, Any]],
    *,
    observed_on: str,
    repository: WorkflowRepository,
    per_call_timeout_seconds: float,
    total_deadline_seconds: float,
    max_fast_calls: int,
) -> dict[str, Any]:
    for field, value in (
        ("per_call_timeout_seconds", per_call_timeout_seconds),
        ("total_deadline_seconds", total_deadline_seconds),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value <= 0
        ):
            raise ValueError(f"{field} must be a positive number")
    if (
        isinstance(max_fast_calls, bool)
        or not isinstance(max_fast_calls, int)
        or max_fast_calls <= 0
    ):
        raise ValueError("max_fast_calls must be a positive integer")
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    start_gate = context.Event() if os.name == "nt" else None
    windows_job = WindowsJob() if os.name == "nt" else None
    process = context.Process(
        target=_review_runner_worker,
        args=(
            child,
            start_gate,
            review_runner,
            jobs,
            {
                "observed_on": observed_on,
                "repository": repository,
                "per_call_timeout_seconds": per_call_timeout_seconds,
                "total_deadline_seconds": total_deadline_seconds,
                "max_fast_calls": max_fast_calls,
            },
        ),
    )
    try:
        process.start()
        if windows_job is not None:
            windows_job.assign_handle(process.sentinel)
            start_gate.set()
    except BaseException:
        if process.pid is not None:
            _kill_worker_tree(process, windows_job)
            process.close()
        if windows_job is not None:
            windows_job.close()
        parent.close()
        child.close()
        raise
    child.close()
    startup_deadline = time.monotonic() + 10
    deadline: float | None = None
    try:
        while True:
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                _kill_worker_tree(process, windows_job)
                raise TimeoutError("review runner exceeded total deadline")
            if deadline is None and now >= startup_deadline:
                _kill_worker_tree(process, windows_job)
                raise TimeoutError("review runner failed to start")
            if parent.poll(0.01):
                kind, payload = parent.recv()
                if kind == "ready":
                    deadline = time.monotonic() + total_deadline_seconds
                    continue
                process.join(timeout=_WORKER_JOIN_TIMEOUT_SECONDS)
                if kind == "ok":
                    return payload
                raise RuntimeError(f"review runner failed: {payload}")
            if not process.is_alive():
                raise RuntimeError("review runner exited without a result")
    finally:
        if windows_job is not None or process.is_alive():
            _kill_worker_tree(process, windows_job)
        if windows_job is not None:
            windows_job.close()
        process.close()
        parent.close()


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


def _disabled_review() -> dict[str, Any]:
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
        normalized = status.casefold()
        if normalized in {"interested", "applied"}:
            statuses[job_id] = normalized
        elif normalized in {"interview", "offer", "accepted"}:
            statuses[job_id] = "applied"
        elif normalized in {"rejected", "withdrawn"}:
            statuses[job_id] = "skip"
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
    receipt_checker: ReceiptChecker | None = None,
    resume_sha256: str | None = None,
    review_asof: str | None = None,
) -> dict[str, Any]:
    validate_resume_selection(resume_sha256)
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
    for item in scored_scan["jobs"]:
        item["tracks"] = list(item["tracks"])
        item["skills"] = list(item["skills"])

    prior_jobs = _document_items(repository, "lifecycle")
    as_of = observation_as_of(observed_on, scored_scan["jobs"])
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
    # This workflow explicitly rescores observations. Refresh the derived fields
    # that the generic lifecycle merge otherwise preserves as curated values.
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
        candidate = scored_by_id.get(job_id)
        if candidate is None or (candidate.get("last_seen") or observed_on) < (item.get("last_seen") or observed_on):
            scored_by_id[job_id] = item
    for item in lifecycle["jobs"]:
        current = scored_by_id.get(item["stable_id"])
        if current is not None and (current.get("last_seen") or observed_on) < item["last_seen"]:
            del scored_by_id[item["stable_id"]]
    for item in lifecycle["jobs"]:
        current = scored_by_id.get(item["stable_id"])
        if current is not None:
            for field in ("score", "summary", "risk", "tracks", "skills", "visa_supported"):
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
        review = _disabled_review()
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
    published, receipt_counts = project_review_receipts(
        published, checker=receipt_checker, resume_sha256=resume_sha256, asof=review_asof,
    )
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
        "operator_review": receipt_counts,
        "view_model": view_model,
        "counts": {
            "scanned": len(jobs),
            "published": len(published),
        },
    }
