from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import asdict, fields
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from job_radar.application import score_jobs
from job_radar.catalog import Catalog, load_catalog
from job_radar.config import load_user_config
from job_radar.domain.jobs import Job
from job_radar.dashboard import render_dashboard, render_dashboard_view_model
from job_radar.data.tracking_store import LocalTrackingStore
from job_radar.lifecycle_merge import merge_scan_state, observation_as_of
from job_radar.job_output import write_jobs_json
from job_radar.legacy_scoring import LegacyCommandScorer, score_jobs_with_command
from job_radar.official_sources import scan_catalog
from job_radar.public_workflow import execute_public_workflow, project_application_tracking, run_review_runner_bounded
from job_radar.review_provider import ConfiguredExternalReviewRunner
from job_radar.domain.tracking import stable_job_id
from job_radar.tracking_commands import record_confirmed_application
from job_radar.source_orchestration import (
    AtomicSourceScanFailed,
    scan_catalog_resilient,
)
from job_radar.local_state import writer_guard
from job_radar.run_receipt import RunReceipt


DEFAULT_CATALOG = Path("catalog/sources.yaml")
DEFAULT_PROFILE = Path("user-data/profile.yaml")
DEFAULT_JOBS = Path("scans/latest.json")
DEFAULT_SCORED_JOBS = Path("scans/scored.json")
DEFAULT_DASHBOARD = Path("dashboard/public/index.html")
DEFAULT_USER_DATA = Path("user-data")
DEFAULT_ALIASES = Path("user-data/aliases.json")
MAX_RESPONSE_BYTES = 50 * 1024 * 1024


def _add_catalog_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)


def _add_profile_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)


def _add_checkpoint_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--checkpoint', type=Path, help='opt-in company-level scan progress file')
    parser.add_argument('--resume', action='store_true', help='resume an existing matching checkpoint')
    parser.add_argument('--health-output', type=Path, help='optional separate source-health receipt with per-company timing and cached/fetch scope')


def _add_review_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--per-call-timeout", type=float, default=30)
    parser.add_argument("--total-review-deadline", type=float, default=300)
    parser.add_argument("--max-fast-calls", type=int, default=100)
    parser.add_argument(
        "--provider-command",
        nargs=argparse.REMAINDER,
        help="compact provider executable and arguments; must be the final option",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="job-radar",
        description="Scan official job boards, score locally, and build a private dashboard.",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="check local pipeline readiness")
    _add_catalog_argument(doctor)
    _add_profile_argument(doctor)
    doctor.add_argument("--jobs-output", type=Path, default=DEFAULT_JOBS)
    doctor.add_argument("--dashboard-output", type=Path, default=DEFAULT_DASHBOARD)
    doctor.add_argument("--ai-command", nargs=argparse.REMAINDER)

    catalog = commands.add_parser("catalog", help="inspect the public source catalog")
    catalog_commands = catalog.add_subparsers(dest="catalog_command", required=True)
    catalog_list = catalog_commands.add_parser("list", help="list configured sources")
    _add_catalog_argument(catalog_list)

    scan = commands.add_parser("scan", help="scan enabled official job-board APIs")
    _add_catalog_argument(scan)
    scan.add_argument("--output", type=Path, default=DEFAULT_JOBS)
    scan.add_argument(
        "--mode",
        choices=("atomic", "best-effort"),
        default="atomic",
    )
    scan.add_argument("--source-timeout", type=float, default=30.0)
    scan.add_argument("--max-source-concurrency", type=int, default=4)
    _add_checkpoint_arguments(scan)

    score = commands.add_parser("score", help="score a normalized jobs JSON file")
    score.add_argument("--jobs", type=Path, default=DEFAULT_JOBS)
    _add_profile_argument(score)
    score.add_argument("--output", type=Path, default=DEFAULT_SCORED_JOBS)
    score.add_argument(
        "--ai-command",
        nargs=argparse.REMAINDER,
        help="executable and arguments; must be the final job-radar option",
    )

    run = commands.add_parser("run", help="scan, score, write JSON, and build the dashboard")
    _add_catalog_argument(run)
    _add_profile_argument(run)
    run.add_argument("--jobs-output", type=Path, default=DEFAULT_JOBS)
    run.add_argument("--dashboard-output", type=Path, default=DEFAULT_DASHBOARD)
    run.add_argument("--user-data", type=Path, default=DEFAULT_USER_DATA)
    run.add_argument("--aliases", type=Path, default=DEFAULT_ALIASES)
    run.add_argument("--observed-on", default=date.today().isoformat())
    run.add_argument(
        "--mode",
        choices=("atomic", "best-effort"),
        default="atomic",
    )
    run.add_argument("--source-timeout", type=float, default=30.0)
    run.add_argument("--max-source-concurrency", type=int, default=4)
    _add_checkpoint_arguments(run)
    run.add_argument('--run-manifest', type=Path, help='new per-attempt receipt; existing files are preserved')
    command_group = run.add_mutually_exclusive_group()
    command_group.add_argument(
        "--ai-command",
        nargs=argparse.REMAINDER,
        help="executable and arguments; must be the final job-radar option",
    )

    schedule = commands.add_parser("schedule", help="render a scheduler configuration")
    schedule_commands = schedule.add_subparsers(dest="schedule_command", required=True)
    schedule_render = schedule_commands.add_parser(
        "render", help="print a cron or Windows Task Scheduler configuration"
    )
    schedule_render.add_argument("--platform", choices=("cron", "windows"), required=True)
    schedule_render.add_argument("--daily-at", default="08:00", metavar="HH:MM")
    schedule_render.add_argument("--project-dir", type=Path, default=Path.cwd())
    schedule_render.add_argument("--executable", type=Path)
    schedule_render.add_argument("--output", type=Path)

    build = commands.add_parser("build-dashboard", help="build from normalized jobs JSON")
    build.add_argument("--jobs", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--user-data", type=Path, help="project local application tracking without scanning")
    build.add_argument('--profile', type=Path, help='rescore and filter the display from saved observations, without scanning')

    review = commands.add_parser(
        "review",
        help="review jobs with an explicitly configured provider",
    )
    command_group.add_argument(
        "--provider-command",
        nargs=argparse.REMAINDER,
        help="compact review provider executable; must be the final option",
    )
    run.add_argument("--per-call-timeout", type=float, default=30)
    run.add_argument("--total-review-deadline", type=float, default=300)
    run.add_argument("--max-fast-calls", type=int, default=100)
    review.add_argument("--jobs", type=Path, required=True)
    review.add_argument("--output", type=Path, required=True)
    review.add_argument("--user-data", type=Path, default=DEFAULT_USER_DATA)
    review.add_argument("--observed-on", default=date.today().isoformat())
    _add_review_arguments(review)

    tracking = commands.add_parser(
        "tracking",
        help="initialize, export, or delete local tracking state",
    )
    tracking_commands = tracking.add_subparsers(
        dest="tracking_command",
        required=True,
    )
    for name in ("init", "delete"):
        command = tracking_commands.add_parser(name)
        command.add_argument("--user-data", type=Path, default=DEFAULT_USER_DATA)
    tracking_export = tracking_commands.add_parser("export")
    tracking_export.add_argument("--user-data", type=Path, default=DEFAULT_USER_DATA)
    tracking_export.add_argument("--output", type=Path, required=True)
    recording = tracking_commands.add_parser(
        "record-application", help="explicitly confirm a submitted application from a local dashboard export"
    )
    recording.add_argument("--user-data", type=Path, default=DEFAULT_USER_DATA)
    recording.add_argument("--from-dashboard", type=Path, required=True)
    recording.add_argument("--job-id", required=True)
    recording.add_argument("--applied-at", required=True, help="actual ISO datetime with timezone, or unknown")
    recording.add_argument("--resume-version", required=True)
    recording.add_argument("--channel", required=True)
    recording.add_argument("--confirmation-evidence", required=True)
    recording.add_argument("--confirmed-at", required=True, help="ISO datetime with timezone when the submission was confirmed")
    recording.add_argument("--confirm-submitted", action="store_true")

    receipts = commands.add_parser('review-receipt', help='explicit local attestation bound to JD and resume hashes; never submits applications')
    receipt_commands = receipts.add_subparsers(dest='receipt_command', required=True)
    for name in ('record', 'check'):
        receipt = receipt_commands.add_parser(name)
        receipt.add_argument('--user-data', type=Path, default=DEFAULT_USER_DATA)
        receipt.add_argument('--jobs', type=Path, required=True)
        receipt.add_argument('--job-id', required=True)
        receipt.add_argument('--jd-sha256', required=True)
        receipt.add_argument('--resume-sha256', required=True, help='digest supplied by the operator; resume files are never opened')
        if name == 'record':
            receipt.add_argument('--reviewer', required=True)
            receipt.add_argument('--evidence', required=True)
            receipt.add_argument('--decision', choices=('approve', 'reject'), required=True)
            receipt.add_argument('--reviewed-at', required=True)
            receipt.add_argument('--confirm-reviewed', action='store_true')
    sync = commands.add_parser('sheet-sync', help='opt-in dedicated-tab delivery; never submits applications')
    sync_commands = sync.add_subparsers(dest='sync_command', required=True)
    for name in ('enqueue', 'status', 'deliver'):
        item = sync_commands.add_parser(name)
        item.add_argument('--user-data', type=Path, default=DEFAULT_USER_DATA)
        if name != 'status':
            item.add_argument('--spreadsheet-id', required=True)
            item.add_argument('--tab', required=True)
        if name == 'enqueue':
            item.add_argument('--jobs', type=Path, required=True)
            item.add_argument('--job-id', required=True)
            item.add_argument('--from-application', action='store_true', help='use explicitly confirmed local application evidence, never browser status')
            item.add_argument('--expected-remote-hash', help='known previous acknowledged payload hash for an update')
        if name == 'deliver':
            item.add_argument('--confirm-write', action='store_true', help='authorize minimal metadata sharing with Google on this dedicated target')
            item.add_argument('--max-items', type=int, default=5)
    return parser


def http_get_json(url: str, *, timeout: int = 30) -> Any:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("official source URL must use HTTPS")
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "job-radar-starter/0.1 (local single-user client)",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("official source response exceeded 50 MiB")
    return json.loads(raw.decode("utf-8"))


def _job_from_dict(item: Any) -> Job:
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


def _load_jobs(path: Path) -> list[Job]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = _job_items(raw)
    return [_job_from_dict(item) for item in items]


def _job_items(raw: Any) -> list[dict[str, Any]]:
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


def _load_jobs_document(path: Path) -> tuple[str, Any, list[Job]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    jobs = _load_jobs(path)
    if isinstance(raw, list):
        return "legacy", raw, jobs
    if set(raw) - {"observed_on"} == {
        "contract_version",
        "mode",
        "incomplete",
        "jobs",
        "failures",
    }:
        return "scan", raw, jobs
    if set(raw) == {"contract_version", "jobs", "statuses"}:
        return "lifecycle", raw, jobs
    raise ValueError("jobs JSON has an unsupported versioned envelope")


def _atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _empty_tracking() -> dict[str, Any]:
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


def _in_process_scan_runner(
    catalog: Catalog,
    get_json,
    *,
    mode: str,
    per_source_timeout: float,
    output: Path,
    max_concurrency: int,
) -> dict[str, Any]:
    del per_source_timeout, max_concurrency
    jobs = scan_catalog(catalog, get_json)
    result = {
        "contract_version": 1,
        "observed_on": date.today().isoformat(),
        "mode": mode,
        "incomplete": False,
        "jobs": [asdict(job) for job in jobs],
        "failures": [],
    }
    _atomic_write_json(output, result)
    return result


def _view_model_from_document(kind: str, raw: Any) -> dict[str, Any]:
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
            "tracking": {**_empty_tracking(), "statuses": raw["statuses"]},
        }
    if kind == "scan":
        observed_on = raw.get("observed_on", "")
        lifecycle = merge_scan_state(
            prior_state={"contract_version": 1, "jobs": [], "statuses": {}},
            scan_result=raw,
            observed_on=observation_as_of(observed_on, raw["jobs"]),
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
                    "stable_id": stable_job_id(source=item["source"], external_id=item["external_id"], company=item["company"], title=item["title"]),
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
            "tracking": {**_empty_tracking(), "statuses": lifecycle["statuses"]},
        }
    raise ValueError("legacy input does not have a versioned Dashboard view model")


def _catalog_report(catalog: Catalog) -> dict[str, Any]:
    return {
        "countries": {name: asdict(value) for name, value in catalog.countries.items()},
        "sources": {name: asdict(value) for name, value in catalog.sources.items()},
        "companies": {name: asdict(value) for name, value in catalog.companies.items()},
    }


def _writable_parent(path: Path) -> bool:
    candidate = path.resolve().parent
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate.is_dir() and os.access(candidate, os.W_OK)


def _command_ready(command: list[str] | None) -> dict[str, Any]:
    if not command:
        return {"mode": "local_rules", "ready": True}
    executable = command[0]
    found = shutil.which(executable) or (str(Path(executable).resolve()) if Path(executable).exists() else None)
    return {"mode": "external_command", "ready": found is not None, "executable": executable}


def _doctor(args: argparse.Namespace) -> dict[str, Any]:
    catalog_status: dict[str, Any] = {"path": str(args.catalog.resolve()), "ready": False}
    profile_status: dict[str, Any] = {"path": str(args.profile.resolve()), "ready": False}
    try:
        loaded_catalog = load_catalog(args.catalog)
        catalog_status.update(
            ready=True,
            sources=len(loaded_catalog.sources),
            companies=len(loaded_catalog.companies),
        )
    except (OSError, ValueError) as error:
        catalog_status["error"] = str(error)
    try:
        load_user_config(args.profile)
        profile_status["ready"] = True
    except (OSError, ValueError) as error:
        profile_status["error"] = str(error)

    outputs = {
        "jobs": {
            "path": str(args.jobs_output.resolve()),
            "ready": _writable_parent(args.jobs_output),
        },
        "dashboard": {
            "path": str(args.dashboard_output.resolve()),
            "ready": _writable_parent(args.dashboard_output),
        },
    }
    ai = _command_ready(args.ai_command)
    ready = (
        catalog_status["ready"]
        and profile_status["ready"]
        and all(item["ready"] for item in outputs.values())
        and ai["ready"]
    )
    return {
        "ok": ready,
        "catalog": catalog_status,
        "profile": profile_status,
        "outputs": outputs,
        "network_auth": {"required": False, "reason": "built-in sources are public APIs"},
        "ai": ai,
    }


def _quote_powershell(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _schedule_arguments() -> list[str]:
    return [
        "--json",
        "run",
        "--catalog",
        str(DEFAULT_CATALOG),
        "--profile",
        str(DEFAULT_PROFILE),
        "--jobs-output",
        str(DEFAULT_JOBS),
        "--dashboard-output",
        str(DEFAULT_DASHBOARD),
    ]


def render_schedule(
    *,
    platform: str,
    daily_at: str,
    project_dir: Path,
    executable: Path,
) -> str:
    try:
        parsed_time = datetime.strptime(daily_at, "%H:%M")
    except ValueError as error:
        raise ValueError("daily_at must use 24-hour HH:MM format") from error
    project = project_dir.resolve()
    executable = executable.resolve()
    arguments = _schedule_arguments()
    if platform == "cron":
        command = " ".join(
            [shlex.quote(str(executable)), *(shlex.quote(argument) for argument in arguments)]
        )
        log = shlex.quote(str(project / "scans" / "job-radar.log"))
        return (
            f"{parsed_time.minute} {parsed_time.hour} * * * "
            f"cd {shlex.quote(str(project))} && {command} >> {log} 2>&1\n"
        )
    if platform == "windows":
        argument_text = subprocess.list2cmdline(arguments)
        return "\n".join(
            (
                "$Action = New-ScheduledTaskAction "
                f"-Execute {_quote_powershell(str(executable))} "
                f"-Argument {_quote_powershell(argument_text)} "
                f"-WorkingDirectory {_quote_powershell(str(project))}",
                "$Trigger = New-ScheduledTaskTrigger -Daily "
                f"-At {_quote_powershell(daily_at)}",
                "Register-ScheduledTask -TaskName 'Job Radar Daily Scan' "
                "-Action $Action -Trigger $Trigger -Description 'Scan official job APIs and rebuild the local dashboard'",
                "",
            )
        )
    raise ValueError("platform must be cron or windows")


def _emit(payload: dict[str, Any], *, as_json: bool, stdout: TextIO) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=stdout)
        return
    if payload.get("ok") is False:
        print(f"not ready: {payload}", file=stdout)
        return
    operation = payload.get("operation", "doctor")
    counts = payload.get("counts")
    suffix = f" ({counts})" if counts else ""
    print(f"{operation}: ok{suffix}", file=stdout)


def _validate_output_boundary(output: Path, *, inputs=(), user_data: Path | None = None) -> None:
    target = output.resolve()
    if target.name.casefold().endswith('.guard') or target in {path.resolve() for path in inputs if path is not None}:
        raise ValueError('output must be separate from inputs, other outputs and reserved guard files')
    if user_data is not None:
        state_root = user_data.resolve()
        if any(target.is_relative_to(state_root / name) for name in ('tracking', 'review')):
            raise ValueError('output cannot replace managed tracking or review state')


def _validate_review_snapshot(args) -> None:
    _kind, raw, _jobs = _load_jobs_document(args.jobs)
    matches = {
        item.get('jd_hash', '') for item in _job_items(raw)
        if (item.get('stable_id') or stable_job_id(source=item['source'], external_id=item['external_id'],
            company=item['company'], title=item['title'])) == args.job_id
    }
    if matches != {args.jd_sha256} or not args.jd_sha256:
        raise ValueError('review JD hash must match one unambiguous current job snapshot')


def main(
    argv: list[str] | None = None,
    *,
    get_json=None,
    command_runner=subprocess.run,
    scan_runner=None,
    review_runner=None,
    sheet_transport=None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    args = _parser().parse_args(argv)
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    injected_get_json = get_json is not None
    get_json = get_json or http_get_json
    default_scan_runner = scan_runner is None
    if scan_runner is None:
        scan_runner = scan_catalog_resilient
    run_receipt: RunReceipt | None = None
    try:
        if args.command == 'sheet-sync':
            from job_radar.sheet_sync import GoogleSheetsAdapter, SheetOutbox

            store = LocalTrackingStore(args.user_data.absolute())
            outbox = SheetOutbox(store.path_for('sheet_outbox'))
            if args.sync_command == 'status':
                report = {'ok': True, 'operation': 'sheet-sync.status', 'result': outbox.status()}
            else:
                if args.sync_command == 'deliver' and not args.confirm_write:
                    raise ValueError('Sheets delivery requires explicit --confirm-write; local deletion will not delete Google copies')
                transport = sheet_transport
                if args.sync_command == 'deliver' and transport is None:
                    from job_radar.sheets_transport import OAuthSheetsTransport
                    transport = OAuthSheetsTransport()
                adapter = GoogleSheetsAdapter(args.spreadsheet_id, args.tab, transport)
                if args.sync_command == 'enqueue':
                    from job_radar.sheet_commands import enqueue_snapshot
                    if args.jobs.resolve() == outbox.path.resolve():
                        raise ValueError('job snapshot must be separate from outbox state')
                    _kind, raw, _jobs = _load_jobs_document(args.jobs)
                    operation_id = enqueue_snapshot(outbox, store, jobs=_job_items(raw), job_id=args.job_id,
                        target=adapter.target, from_application=args.from_application,
                        expected_remote_hash=args.expected_remote_hash)
                    report = {'ok': True, 'operation': 'sheet-sync.enqueue', 'operation_id': operation_id,
                              'target': adapter.target}
                else:
                    result = outbox.deliver(adapter, adapter.target, max_items=args.max_items)
                    report = {'ok': result['error'] is None, 'operation': 'sheet-sync.deliver', 'result': result}
            if args.json:
                _emit(report, as_json=True, stdout=stdout)
            else:
                print(f"{report['operation']}: {'ok' if report['ok'] else 'failed'}", file=stdout)
            return 0 if report['ok'] else 1
        if args.command == 'review-receipt':
            from job_radar.review_receipts import ReviewReceiptStore

            store = LocalTrackingStore(args.user_data.absolute())
            if args.jobs.resolve() == store.path_for('review_receipts'):
                raise ValueError('review snapshot must be separate from receipt state')
            _validate_review_snapshot(args)
            receipts = ReviewReceiptStore(store.path_for('review_receipts'))
            asof = datetime.now(timezone.utc).isoformat()
            if args.receipt_command == 'record':
                result = receipts.record(args.job_id, args.jd_sha256, args.resume_sha256, args.reviewer,
                    args.evidence, args.decision, args.reviewed_at, confirmed=args.confirm_reviewed, asof=asof)
            else:
                result = receipts.check(args.job_id, args.jd_sha256, args.resume_sha256, asof=asof)
            _emit({'ok': True, 'operation': 'review-receipt.' + args.receipt_command, 'result': result},
                  as_json=args.json, stdout=stdout)
            return 0
        if args.command == "doctor":
            report = _doctor(args)
            _emit(report, as_json=args.json, stdout=stdout)
            return 0 if report["ok"] else 1

        if args.command == "catalog" and args.catalog_command == "list":
            catalog = load_catalog(args.catalog)
            report = {"ok": True, "operation": "catalog.list", **_catalog_report(catalog)}
            _emit(report, as_json=args.json, stdout=stdout)
            return 0

        if args.command == "scan":
            _validate_output_boundary(args.output, inputs=(args.catalog, args.checkpoint))
            if args.health_output is not None:
                _validate_output_boundary(args.health_output, inputs=(args.catalog, args.output, args.checkpoint))
            checkpoint_options = {}
            if args.checkpoint is not None or args.resume:
                checkpoint_options = dict(checkpoint=args.checkpoint, resume=args.resume)
            if args.health_output is not None:
                checkpoint_options['health_output'] = args.health_output
            result = scan_runner(
                load_catalog(args.catalog),
                get_json,
                mode=args.mode,
                per_source_timeout=args.source_timeout,
                output=args.output,
                max_concurrency=args.max_source_concurrency,
                **checkpoint_options,
            )
            report = {
                "ok": True,
                "operation": "scan",
                "counts": {
                    "failures": len(result["failures"]),
                    "scanned": len(result["jobs"]),
                },
                "output": str(args.output.resolve()),
                "incomplete": result["incomplete"],
                "mode": args.mode,
            }
            if args.health_output is not None:
                report['source_health_output'] = str(args.health_output.resolve())
            _emit(report, as_json=args.json, stdout=stdout)
            return 0

        if args.command == "score":
            kind, raw, jobs = _load_jobs_document(args.jobs)
            config = load_user_config(args.profile)
            if args.ai_command:
                scored = score_jobs_with_command(
                    jobs,
                    config,
                    args.ai_command,
                    runner=command_runner,
                )
            else:
                scored = score_jobs(jobs, config)
            if kind == "scan":
                payload = dict(raw)
                payload["jobs"] = [asdict(job) for job in scored]
                _atomic_write_json(args.output, payload)
            elif kind == "lifecycle":
                by_key = {
                    (job.source, job.external_id): asdict(job) for job in scored
                }
                payload = dict(raw)
                payload["jobs"] = [
                    {
                        **item,
                        **by_key[(str(item["source"]), str(item["external_id"]))],
                    }
                    for item in raw["jobs"]
                ]
                _atomic_write_json(args.output, payload)
            else:
                write_jobs_json(scored, args.output)
            report = {
                "ok": True,
                "operation": "score",
                "counts": {"scored": len(scored)},
                "output": str(args.output.resolve()),
                "mode": "ai_command" if args.ai_command else "local_rules",
            }
            _emit(report, as_json=args.json, stdout=stdout)
            return 0

        if args.command == "run":
            inputs = (args.catalog, args.profile, args.aliases, args.checkpoint, args.health_output)
            _validate_output_boundary(args.jobs_output, inputs=(*inputs, args.dashboard_output), user_data=args.user_data)
            _validate_output_boundary(args.dashboard_output, inputs=(*inputs, args.jobs_output), user_data=args.user_data)
            if args.run_manifest is not None:
                _validate_output_boundary(args.run_manifest, inputs=(*inputs, args.jobs_output, args.dashboard_output), user_data=args.user_data)
            if args.health_output is not None:
                _validate_output_boundary(args.health_output, inputs=(args.catalog, args.profile, args.aliases,
                    args.checkpoint, args.jobs_output, args.dashboard_output, args.run_manifest), user_data=args.user_data)
            active_scan_runner = (_in_process_scan_runner if injected_get_json and default_scan_runner
                                  and args.checkpoint is None and not args.resume and args.health_output is None else scan_runner)
            reserved = [args.jobs_output, args.dashboard_output, args.catalog, args.profile, args.aliases]
            if args.checkpoint is not None:
                checkpoint_path = args.checkpoint.resolve()
                checkpoint_guard = checkpoint_path.with_name(checkpoint_path.name + '.guard')
                if any(path.resolve() in (checkpoint_path, checkpoint_guard) for path in reserved):
                    raise ValueError('checkpoint and its guard must be separate from inputs and outputs')
                reserved.extend((checkpoint_path, checkpoint_guard))
            if args.run_manifest is not None and args.run_manifest.resolve() in {path.resolve() for path in reserved}:
                raise ValueError('run receipt must be separate from inputs and outputs')
            with writer_guard(args.jobs_output.resolve().with_name(args.jobs_output.name + '.run.guard')):
                run_receipt = RunReceipt(args.jobs_output.resolve(), path=args.run_manifest)
                try:
                    run_receipt.stage('scan', 'running')
                    checkpoint_options = {}
                    if args.checkpoint is not None or args.resume:
                        checkpoint_options = dict(checkpoint=args.checkpoint, resume=args.resume,
                                                  observed_on=args.observed_on, on_progress=run_receipt.scan_progress)
                    if args.health_output is not None:
                        checkpoint_options['health_output'] = args.health_output
                        run_receipt.document['source_health_path'] = str(args.health_output.resolve())
                    scan_result = active_scan_runner(
                        load_catalog(args.catalog), get_json, mode=args.mode,
                        per_source_timeout=args.source_timeout, output=args.jobs_output,
                        max_concurrency=args.max_source_concurrency, **checkpoint_options,
                    )
                    run_receipt.stage('scan', 'partial' if scan_result['incomplete'] else 'complete')
                    aliases = json.loads(args.aliases.read_text(encoding='utf-8')) if args.aliases.exists() else {
                        'contract_version': 1, 'company_aliases': {}, 'title_aliases': {},
                    }
                    observation = scan_result.get('observed_on', args.observed_on) if args.checkpoint is not None else args.observed_on
                    run_receipt.document['observed_on'] = observation
                    run_receipt.stage('workflow', 'running')
                    workflow = execute_public_workflow(
                        scan_result=scan_result, config=load_user_config(args.profile),
                        repository=LocalTrackingStore(args.user_data.resolve()), observed_on=observation,
                        aliases=aliases,
                        ai_scorer=LegacyCommandScorer(args.ai_command, runner=command_runner) if args.ai_command else None,
                        review_runner=review_runner or (ConfiguredExternalReviewRunner(args.provider_command) if args.provider_command else None),
                        per_call_timeout_seconds=args.per_call_timeout,
                        total_deadline_seconds=args.total_review_deadline, max_fast_calls=args.max_fast_calls,
                        on_stage=run_receipt.stage,
                    )
                    _atomic_write_json(args.jobs_output, workflow['scan'])
                    run_receipt.stage('workflow', 'complete')
                    run_receipt.stage('build', 'running')
                    render_dashboard_view_model(workflow['view_model'], args.dashboard_output)
                    run_receipt.stage('build', 'complete')
                    run_receipt.finish()
                except BaseException as error:
                    run_receipt.finish(error)
                    raise
                report = {
                    'ok': True, 'contract_version': 1, 'operation': 'run',
                    'workflow': ['scan', 'score', 'review', 'lifecycle', 'tracking', 'dashboard'],
                    'counts': workflow['counts'], 'jobs_output': str(args.jobs_output.resolve()),
                    'dashboard_output': str(args.dashboard_output.resolve()),
                    'mode': 'ai_command' if args.ai_command else 'local_rules', 'review': workflow['review'],
                    'run_manifest': str(run_receipt.path.resolve()), 'stages': run_receipt.document['stages'],
                }
                if args.health_output is not None:
                    report['source_health_output'] = str(args.health_output.resolve())
            _emit(report, as_json=args.json, stdout=stdout)
            if not args.json:
                print('stages: ' + ', '.join(f'{name}={state}' for name, state in report['stages'].items()), file=stdout)
                print(f"run receipt: {report['run_manifest']}", file=stdout)
            return 0

        if args.command == "schedule" and args.schedule_command == "render":
            executable = args.executable
            if executable is None:
                executable = Path(sys.executable).with_name(
                    "job-radar.exe" if os.name == "nt" else "job-radar"
                )
            rendered = render_schedule(
                platform=args.platform,
                daily_at=args.daily_at,
                project_dir=args.project_dir,
                executable=executable,
            )
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(rendered, encoding="utf-8")
            if args.json:
                _emit(
                    {
                        "ok": True,
                        "operation": "schedule.render",
                        "platform": args.platform,
                        "installed": False,
                        "output": str(args.output.resolve()) if args.output else None,
                        "configuration": rendered,
                    },
                    as_json=True,
                    stdout=stdout,
                )
            else:
                print(rendered, end="", file=stdout)
            return 0

        if args.command == "build-dashboard":
            _validate_output_boundary(args.output, inputs=(args.jobs, args.profile), user_data=args.user_data)
            kind, raw, jobs = _load_jobs_document(args.jobs)
            if kind == "legacy" and args.user_data is None and args.profile is None:
                render_dashboard(jobs, args.output)
            else:
                model = _view_model_from_document(
                    "scan" if kind == "legacy" else kind,
                    {"contract_version": 1, "mode": "atomic", "incomplete": False, "jobs": raw, "failures": []}
                    if kind == "legacy" else raw,
                )
                if args.user_data is not None:
                    model["tracking"] = project_application_tracking(
                        LocalTrackingStore(args.user_data.resolve()),
                        as_of=datetime.now(timezone.utc).isoformat(),
                    )
                if args.profile is not None:
                    config = load_user_config(args.profile)
                    rescored = {(job.source, job.external_id): asdict(job) for job in score_jobs(jobs, config)}
                    scoring_fields = ('score', 'summary', 'risk', 'tracks', 'skills', 'visa_supported')
                    model['jobs'] = [dict(item, **{field: rescored[(str(item['source']), str(item['external_id']))][field]
                                                  for field in scoring_fields})
                                     for item in model['jobs']
                                     if (rescored[(str(item['source']), str(item['external_id']))]['score'] or 0) >= config.matching.minimum_score]
                render_dashboard_view_model(model, args.output)
            _emit(
                {
                    "ok": True,
                    "operation": "build-dashboard",
                    "counts": {"jobs": len(jobs)},
                    "output": str(args.output.resolve()),
                },
                as_json=args.json,
                stdout=stdout,
            )
            return 0

        if args.command == "review":
            raw = json.loads(args.jobs.read_text(encoding="utf-8"))
            items = _job_items(raw)
            store = LocalTrackingStore(args.user_data.resolve())
            store.initialize()
            active_review_runner = (
                review_runner
                or (
                    ConfiguredExternalReviewRunner(args.provider_command)
                    if args.provider_command
                    else None
                )
            )
            if active_review_runner is not None:
                result = run_review_runner_bounded(
                    active_review_runner,
                    items,
                    observed_on=args.observed_on,
                    repository=store,
                    per_call_timeout_seconds=args.per_call_timeout,
                    total_deadline_seconds=args.total_review_deadline,
                    max_fast_calls=args.max_fast_calls,
                )
                mode = "configured"
            else:
                result = {
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
                mode = "disabled"
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            _emit(
                {
                    "ok": True,
                    "operation": "review",
                    "mode": mode,
                    "counts": {
                        "cache_hits": result["report"]["cache_hits"],
                        "escalations": result["report"]["escalations"],
                        "provider_calls": result["report"]["provider_calls"],
                        "reviewed": len(result["reviews"]),
                    },
                    "output": str(args.output.resolve()),
                },
                as_json=args.json,
                stdout=stdout,
            )
            return 0

        if args.command == "tracking":
            store = LocalTrackingStore(args.user_data.absolute())
            if args.tracking_command == "record-application":
                with args.from_dashboard.open("rb") as handoff:
                    content = handoff.read(8 * 1024 * 1024 + 1)
                if len(content) > 8 * 1024 * 1024:
                    raise ValueError("dashboard export exceeds 8 MiB")
                exported = json.loads(content.decode("utf-8"))
                application = record_confirmed_application(
                    store, dashboard_state=exported, job_id=args.job_id,
                    applied_at=args.applied_at, resume_version=args.resume_version,
                    channel=args.channel, confirmation_evidence=args.confirmation_evidence,
                    confirmed_at=args.confirmed_at, confirm_submitted=args.confirm_submitted,
                )
                report = {
                    "ok": True, "operation": "tracking.record-application",
                    "application_id": application["application_id"],
                    "job_id": application["job_id"], "status": application["status"],
                }
            elif args.tracking_command == "init":
                paths = store.initialize()
                report = {
                    "ok": True,
                    "operation": "tracking.init",
                    "counts": {"documents": len(paths)},
                    "user_data": str(store.root),
                }
            elif args.tracking_command == "export":
                store.initialize()
                destination = store.export_to(args.output)
                report = {
                    "ok": True,
                    "operation": "tracking.export",
                    "output": str(destination),
                }
            elif args.tracking_command == "delete":
                removed = store.delete_tracking_data()
                report = {
                    "ok": True,
                    "operation": "tracking.delete",
                    "counts": {"removed": len(removed)},
                    "user_data": str(store.root),
                }
            else:
                raise AssertionError(
                    f"unhandled tracking command: {args.tracking_command}"
                )
            _emit(report, as_json=args.json, stdout=stdout)
            return 0
        raise AssertionError(f"unhandled command: {args.command}")
    except AtomicSourceScanFailed as error:
        report = {
            "ok": False,
            "error": {
                "type": type(error).__name__,
                "message": str(error),
            },
            **error.result,
        }
        if run_receipt is not None:
            report.update(run_manifest=str(run_receipt.path.resolve()), stages=run_receipt.document['stages'])
        if args.json:
            print(json.dumps(report, ensure_ascii=False, sort_keys=True), file=stdout)
        else:
            print(f"job-radar: {type(error).__name__}: {error}", file=stderr)
        return error.exit_code
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        report = {"ok": False, "error": {"type": type(error).__name__, "message": str(error)}}
        if run_receipt is not None:
            report.update(run_manifest=str(run_receipt.path.resolve()), stages=run_receipt.document['stages'])
        if args.json:
            print(json.dumps(report, ensure_ascii=False, sort_keys=True), file=stdout)
        else:
            print(f"job-radar: {type(error).__name__}: {error}", file=stderr)
        return 2
