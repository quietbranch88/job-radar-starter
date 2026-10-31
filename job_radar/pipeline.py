"""Daily scan, score, and dashboard projection.

The CLI parses arguments and prints reports. This module runs the work.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from job_radar.application import SCORING_FIELDS, score_jobs
from job_radar.catalog import load_catalog
from job_radar.config import load_user_config
from job_radar.dashboard import render_dashboard, render_dashboard_view_model
from job_radar.data.tracking_store import LocalTrackingStore
from job_radar.documents import atomic_write_json, job_items, load_jobs_document
from job_radar.job_output import write_jobs_json
from job_radar.legacy_scoring import LegacyCommandScorer, score_jobs_with_command
from job_radar.local_state import writer_guard
from job_radar.public_workflow import (
    disabled_review_result,
    execute_public_workflow,
    project_application_tracking,
    view_model_from_document,
)
from job_radar.review_provider import ConfiguredExternalReviewRunner
from job_radar.review_supervisor import run_review_runner_bounded
from job_radar.tracking_commands import record_confirmed_application
from job_radar.run_receipt import RunReceipt


class RunReceiptSlot:
    def __init__(self) -> None:
        self.receipt: RunReceipt | None = None


def score_jobs_file(
    *,
    jobs: Path,
    profile: Path,
    output: Path,
    ai_command: Sequence[str] | None,
    command_runner: Callable[..., Any],
) -> dict[str, Any]:
    kind, raw, loaded = load_jobs_document(jobs)
    config = load_user_config(profile)
    if ai_command:
        scored = score_jobs_with_command(
            loaded,
            config,
            ai_command,
            runner=command_runner,
        )
    else:
        scored = score_jobs(loaded, config)
    if kind == "scan":
        payload = dict(raw)
        payload["jobs"] = [asdict(job) for job in scored]
        atomic_write_json(output, payload)
    elif kind == "lifecycle":
        by_key = {(job.source, job.external_id): asdict(job) for job in scored}
        payload = dict(raw)
        payload["jobs"] = [
            {
                **item,
                **by_key[(str(item["source"]), str(item["external_id"]))],
            }
            for item in raw["jobs"]
        ]
        atomic_write_json(output, payload)
    else:
        write_jobs_json(scored, output)
    return {
        "ok": True,
        "operation": "score",
        "counts": {"scored": len(scored)},
        "output": str(output.resolve()),
        "mode": "ai_command" if ai_command else "local_rules",
    }


def build_dashboard(
    *,
    jobs: Path,
    output: Path,
    user_data: Path | None,
    profile: Path | None,
    install_sync: bool,
) -> dict[str, Any]:
    kind, raw, loaded = load_jobs_document(jobs)
    if kind == "legacy" and user_data is None and profile is None:
        render_dashboard(loaded, output)
    else:
        model = view_model_from_document(
            "scan" if kind == "legacy" else kind,
            {
                "contract_version": 1,
                "mode": "atomic",
                "incomplete": False,
                "jobs": raw,
                "failures": [],
            }
            if kind == "legacy"
            else raw,
        )
        if user_data is not None:
            model["tracking"] = project_application_tracking(
                LocalTrackingStore(user_data.resolve()),
                as_of=datetime.now(timezone.utc).isoformat(),
            )
        if profile is not None:
            config = load_user_config(profile)
            rescored = {
                (job.source, job.external_id): asdict(job)
                for job in score_jobs(loaded, config)
            }
            model["jobs"] = [
                dict(
                    item,
                    **{
                        field: rescored[(str(item["source"]), str(item["external_id"]))][field]
                        for field in SCORING_FIELDS
                    },
                )
                for item in model["jobs"]
                if (rescored[(str(item["source"]), str(item["external_id"]))]["score"] or 0)
                >= config.matching.minimum_score
            ]
        render_dashboard_view_model(model, output, install_sync=install_sync)
    return {
        "ok": True,
        "operation": "build-dashboard",
        "counts": {"jobs": len(loaded)},
        "output": str(output.resolve()),
    }


def run_daily(
    *,
    catalog: Path,
    profile: Path,
    aliases: Path,
    user_data: Path,
    jobs_output: Path,
    dashboard_output: Path,
    run_manifest: Path | None,
    health_output: Path | None,
    checkpoint: Path | None,
    resume: bool,
    observed_on: str,
    mode: str,
    source_timeout: float,
    max_source_concurrency: int,
    ai_command: Sequence[str] | None,
    provider_command: Sequence[str] | None,
    per_call_timeout: float,
    total_review_deadline: float,
    max_fast_calls: int,
    install_sync: bool,
    get_json: Callable[..., Any],
    command_runner: Callable[..., Any],
    scan_runner: Callable[..., Any],
    review_runner: Callable[..., Any] | None,
    injected_get_json: bool,
    default_scan_runner: bool,
    receipt_slot: RunReceiptSlot,
) -> dict[str, Any]:
    with writer_guard(jobs_output.resolve().with_name(jobs_output.name + ".run.guard")):
        receipt = RunReceipt(jobs_output.resolve(), path=run_manifest)
        receipt_slot.receipt = receipt
        try:
            receipt.stage("scan", "running")
            checkpoint_options: dict[str, Any] = {}
            if checkpoint is not None or resume:
                checkpoint_options = dict(
                    checkpoint=checkpoint,
                    resume=resume,
                    observed_on=observed_on,
                    on_progress=receipt.scan_progress,
                )
            if health_output is not None:
                checkpoint_options["health_output"] = health_output
                receipt.document["source_health_path"] = str(health_output.resolve())
            scan_options = dict(
                mode=mode,
                per_source_timeout=source_timeout,
                output=jobs_output,
                max_concurrency=max_source_concurrency,
                **checkpoint_options,
            )
            if (
                injected_get_json
                and default_scan_runner
                and checkpoint is None
                and not resume
                and health_output is None
            ):
                scan_options["inline"] = True
            scan_result = scan_runner(load_catalog(catalog), get_json, **scan_options)
            receipt.stage("scan", "partial" if scan_result["incomplete"] else "complete")
            alias_document = (
                json.loads(aliases.read_text(encoding="utf-8"))
                if aliases.exists()
                else {
                    "contract_version": 1,
                    "company_aliases": {},
                    "title_aliases": {},
                }
            )
            observation = (
                scan_result.get("observed_on", observed_on)
                if checkpoint is not None
                else observed_on
            )
            receipt.document["observed_on"] = observation
            receipt.stage("workflow", "running")
            workflow = execute_public_workflow(
                scan_result=scan_result,
                config=load_user_config(profile),
                repository=LocalTrackingStore(user_data.resolve()),
                observed_on=observation,
                aliases=alias_document,
                ai_scorer=LegacyCommandScorer(ai_command, runner=command_runner) if ai_command else None,
                review_runner=review_runner
                or (ConfiguredExternalReviewRunner(provider_command) if provider_command else None),
                per_call_timeout_seconds=per_call_timeout,
                total_deadline_seconds=total_review_deadline,
                max_fast_calls=max_fast_calls,
                on_stage=receipt.stage,
            )
            atomic_write_json(jobs_output, workflow["scan"])
            receipt.stage("workflow", "complete")
            receipt.stage("build", "running")
            render_dashboard_view_model(
                workflow["view_model"],
                dashboard_output,
                install_sync=install_sync,
            )
            receipt.stage("build", "complete")
            receipt.finish()
        except BaseException as error:
            receipt.finish(error)
            raise
        report = {
            "ok": True,
            "contract_version": 1,
            "operation": "run",
            "workflow": ["scan", "score", "review", "lifecycle", "tracking", "dashboard"],
            "counts": workflow["counts"],
            "jobs_output": str(jobs_output.resolve()),
            "dashboard_output": str(dashboard_output.resolve()),
            "mode": "ai_command" if ai_command else "local_rules",
            "review": workflow["review"],
            "run_manifest": str(receipt.path.resolve()),
            "stages": receipt.document["stages"],
        }
        if health_output is not None:
            report["source_health_output"] = str(health_output.resolve())
    return report


def review_jobs_file(
    *,
    jobs: Path,
    output: Path,
    user_data: Path,
    observed_on: str,
    provider_command: Sequence[str] | None,
    per_call_timeout: float,
    total_review_deadline: float,
    max_fast_calls: int,
    review_runner: Callable[..., Any] | None,
) -> dict[str, Any]:
    raw = json.loads(jobs.read_text(encoding="utf-8"))
    items = job_items(raw)
    store = LocalTrackingStore(user_data.resolve())
    store.initialize()
    active_review_runner = review_runner or (
        ConfiguredExternalReviewRunner(provider_command) if provider_command else None
    )
    if active_review_runner is not None:
        result = run_review_runner_bounded(
            active_review_runner,
            items,
            observed_on=observed_on,
            repository=store,
            per_call_timeout_seconds=per_call_timeout,
            total_deadline_seconds=total_review_deadline,
            max_fast_calls=max_fast_calls,
        )
        mode = "configured"
    else:
        result = disabled_review_result()
        mode = "disabled"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "ok": True,
        "operation": "review",
        "mode": mode,
        "counts": {
            "cache_hits": result["report"]["cache_hits"],
            "escalations": result["report"]["escalations"],
            "provider_calls": result["report"]["provider_calls"],
            "reviewed": len(result["reviews"]),
        },
        "output": str(output.resolve()),
    }


def run_tracking(
    *,
    command: str,
    user_data: Path,
    output: Path | None = None,
    from_dashboard: Path | None = None,
    job_id: str | None = None,
    applied_at: str | None = None,
    resume_version: str | None = None,
    channel: str | None = None,
    confirmation_evidence: str | None = None,
    confirmed_at: str | None = None,
    confirm_submitted: bool = False,
) -> dict[str, Any]:
    store = LocalTrackingStore(user_data.absolute())
    if command == "record-application":
        if from_dashboard is None:
            raise ValueError("dashboard export is required")
        with from_dashboard.open("rb") as handoff:
            content = handoff.read(8 * 1024 * 1024 + 1)
        if len(content) > 8 * 1024 * 1024:
            raise ValueError("dashboard export exceeds 8 MiB")
        exported = json.loads(content.decode("utf-8"))
        application = record_confirmed_application(
            store,
            dashboard_state=exported,
            job_id=job_id,
            applied_at=applied_at,
            resume_version=resume_version,
            channel=channel,
            confirmation_evidence=confirmation_evidence,
            confirmed_at=confirmed_at,
            confirm_submitted=confirm_submitted,
        )
        return {
            "ok": True,
            "operation": "tracking.record-application",
            "application_id": application["application_id"],
            "job_id": application["job_id"],
            "status": application["status"],
        }
    if command == "init":
        paths = store.initialize()
        return {
            "ok": True,
            "operation": "tracking.init",
            "counts": {"documents": len(paths)},
            "user_data": str(store.root),
        }
    if command == "export":
        if output is None:
            raise ValueError("tracking export output is required")
        store.initialize()
        destination = store.export_to(output)
        return {
            "ok": True,
            "operation": "tracking.export",
            "output": str(destination),
        }
    if command == "delete":
        removed = store.delete_tracking_data()
        return {
            "ok": True,
            "operation": "tracking.delete",
            "counts": {"removed": len(removed)},
            "user_data": str(store.root),
        }
    raise AssertionError(f"unhandled tracking command: {command}")
