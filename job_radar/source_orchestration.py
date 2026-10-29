from __future__ import annotations

import json
import multiprocessing
import os
import tempfile
import time
from collections import deque
from dataclasses import asdict, replace
from datetime import date
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError

from job_radar.adapters import (
    Job,
    SourcePayloadTooLarge,
    fetch_ashby,
    fetch_greenhouse,
    fetch_lever,
)
from job_radar.catalog import Catalog
from job_radar.local_state import writer_guard
from job_radar.scan_checkpoint import ScanCheckpoint
from job_radar.source_health import SourceHealth


_FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
}
_WORKER_STARTUP_TIMEOUT_SECONDS = 10
_WORKER_TERMINATE_GRACE_SECONDS = 1
_WORKER_KILL_GRACE_SECONDS = 1


class AtomicSourceScanFailed(RuntimeError):
    def __init__(self, result: dict[str, Any], *, exit_code: int = 2):
        self.result = result
        self.exit_code = exit_code
        super().__init__("atomic source scan failed")


def _source_worker(
    result_connection: Connection,
    source_name: str,
    board: str,
    company_name: str,
    country: str,
    get_json: Callable[[str], Any],
    max_jobs_per_source: int,
) -> None:
    try:
        result_connection.send(("ready", time.monotonic()))
        fetch = _FETCHERS[source_name]
        fetched = fetch(
            board,
            company_name,
            get_json,
            max_jobs=max_jobs_per_source,
        )
        result_connection.send(
            (
                "ok",
                [replace(job, country=country) for job in fetched],
            )
        )
    except HTTPError as error:
        result_connection.send(("http_error", error.code))
    except TimeoutError:
        result_connection.send(("network_timeout", None))
    except URLError as error:
        kind = 'network_timeout' if isinstance(error.reason, TimeoutError) else 'network_error'
        result_connection.send((kind, None))
    except ConnectionError:
        result_connection.send(("network_error", None))
    except SourcePayloadTooLarge:
        result_connection.send(("oversized", None))
    except (KeyError, TypeError, ValueError):
        result_connection.send(("malformed", None))
    except Exception:
        result_connection.send(("error", None))
    finally:
        result_connection.close()


def _failure(
    source: str,
    company: str,
    category: str,
    message: str,
) -> dict[str, str]:
    return {
        "source": source,
        "company": company,
        "category": category,
        "message": message,
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{path.stem}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _job_key(job: Job) -> tuple[str, str]:
    return (job.source.casefold(), job.external_id.casefold())


def _job_order(job: Job) -> tuple[str, str, str, str]:
    return (
        job.source.casefold(),
        job.external_id.casefold(),
        job.company.casefold(),
        job.title.casefold(),
    )


def _close_worker(worker: dict[str, Any]) -> None:
    process = worker["process"]
    connection = worker["connection"]
    try:
        if process.is_alive():
            process.terminate()
            process.join(timeout=_WORKER_TERMINATE_GRACE_SECONDS)
        if process.is_alive():
            process.kill()
            process.join(timeout=_WORKER_KILL_GRACE_SECONDS)
        try:
            process.close()
        except ValueError:
            pass
    finally:
        connection.close()


def _close_workers(workers: list[dict[str, Any]]) -> None:
    for worker in workers:
        _close_worker(worker)


def _enabled_companies(catalog: Catalog) -> list[tuple[str, str, str, str]]:
    enabled: list[tuple[str, str, str, str]] = []
    for company_name, company in sorted(
        catalog.companies.items(),
        key=lambda item: (
            item[1].source.casefold(),
            item[0].casefold(),
            item[1].board.casefold(),
        ),
    ):
        source = catalog.sources[company.source]
        if not source.enabled:
            continue
        country = company.countries[0] if len(company.countries) == 1 else ""
        enabled.append((company_name, company.source, company.board, country))
    return enabled


def _start_worker(
    context: Any,
    company: tuple[str, str, str, str],
    get_json: Callable[[str], Any],
    max_jobs_per_source: int,
) -> dict[str, Any]:
    company_name, source_name, board, country = company
    parent_connection, child_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=_source_worker,
        args=(
            child_connection,
            source_name,
            board,
            company_name,
            country,
            get_json,
            max_jobs_per_source,
        ),
    )
    try:
        process.start()
    except Exception:
        parent_connection.close()
        child_connection.close()
        raise
    child_connection.close()
    return {
        "company_scope": company,
        "company_name": company_name,
        "source_name": source_name,
        "process": process,
        "connection": parent_connection,
        "startup_deadline": time.monotonic() + _WORKER_STARTUP_TIMEOUT_SECONDS,
        "execution_deadline": None,
        "ready": False,
    }


def scan_catalog_resilient(
    catalog: Catalog,
    get_json: Callable[[str], Any],
    *,
    mode: str,
    per_source_timeout: float,
    output: Path,
    max_jobs_per_source: int = 10_000,
    max_concurrency: int = 4,
    checkpoint: Path | None = None,
    resume: bool = False,
    observed_on: str | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    health_output: Path | None = None,
) -> dict[str, Any]:
    if mode not in {"atomic", "best-effort"}:
        raise ValueError("mode must be atomic or best-effort")
    if (
        isinstance(per_source_timeout, bool)
        or not isinstance(per_source_timeout, (int, float))
        or per_source_timeout <= 0
    ):
        raise ValueError("per_source_timeout must be positive")
    if (
        isinstance(max_jobs_per_source, bool)
        or not isinstance(max_jobs_per_source, int)
        or max_jobs_per_source <= 0
    ):
        raise ValueError("max_jobs_per_source must be a positive integer")
    if (
        isinstance(max_concurrency, bool)
        or not isinstance(max_concurrency, int)
        or max_concurrency <= 0
    ):
        raise ValueError("max_concurrency must be a positive integer")

    if resume and checkpoint is None:
        raise ValueError('--resume requires --checkpoint')
    if output.name.casefold().endswith('.guard'):
        raise ValueError('scan output must be separate from reserved guard files')
    companies = _enabled_companies(catalog)
    if health_output is not None:
        health_output = Path(health_output).resolve()
        reserved = {output.resolve()}
        if checkpoint is not None:
            saved = Path(checkpoint).resolve()
            reserved.update((saved, saved.with_name(saved.name + '.guard')))
        if health_output in reserved or health_output.name.casefold().endswith('.guard'):
            raise ValueError('health output must be separate from jobs, checkpoint and guards')
    observed = observed_on or date.today().isoformat()
    date.fromisoformat(observed)
    options = dict(mode=mode, per_source_timeout=per_source_timeout, output=output,
                   max_jobs_per_source=max_jobs_per_source, max_concurrency=max_concurrency,
                   on_progress=on_progress, health_output=health_output)
    if checkpoint is not None:
        checkpoint = Path(checkpoint).resolve()
        checkpoint_guard = checkpoint.with_name(checkpoint.name + '.guard')
        if output.resolve() in (checkpoint, checkpoint_guard):
            raise ValueError('checkpoint, guard and scan output must be separate paths')
        with writer_guard(checkpoint_guard):
            state = ScanCheckpoint(checkpoint, companies, mode=mode, max_jobs=max_jobs_per_source,
                                   observed_on=observed, resume=resume)
            if on_progress is not None:
                on_progress(state.progress())
            return _scan_companies(companies, get_json, checkpoint_state=state,
                                   observed_on=state.observed_on, **options)
    return _scan_companies(companies, get_json, checkpoint_state=None,
                           observed_on=observed, **options)


def _scan_companies(companies, get_json, *, checkpoint_state, observed_on, mode,
                    per_source_timeout, output, max_jobs_per_source, max_concurrency, on_progress,
                    health_output):
    context = multiprocessing.get_context("spawn")
    jobs: list[Job] = checkpoint_state.retained_jobs() if checkpoint_state else []
    failures: list[dict[str, str]] = []
    pending = deque(checkpoint_state.pending() if checkpoint_state else companies)
    workers: list[dict[str, Any]] = []
    health = SourceHealth(health_output, companies, checkpoint=checkpoint_state) if health_output is not None else None
    completed_normally = False

    try:
        while pending or workers:
            while pending and len(workers) < max_concurrency:
                company = pending.popleft()
                if health is not None:
                    health.begin(company)
                workers.append(
                    _start_worker(
                        context,
                        company,
                        get_json,
                        max_jobs_per_source,
                    )
                )

            completed: list[dict[str, Any]] = []
            now = time.monotonic()
            for worker in workers:
                process = worker["process"]
                connection = worker["connection"]
                failures_before = len(failures)
                if connection.poll():
                    try:
                        kind, payload = connection.recv()
                    except (EOFError, OSError, ValueError):
                        kind, payload = "error", None
                    if kind == "ready" and isinstance(payload, (int, float)):
                        worker["ready"] = True
                        worker["execution_deadline"] = (
                            time.monotonic() + per_source_timeout
                        )
                        continue
                    http_status = payload if kind == 'http_error' and type(payload) is int and 100 <= payload <= 599 else None
                    if http_status is not None:
                        category = ('rate_limited' if http_status == 429 else 'access_denied' if http_status in (401, 403)
                                    else 'not_found' if http_status == 404 else 'upstream_error' if http_status >= 500
                                    else 'http_error')
                        failures.append(_failure(worker['source_name'], worker['company_name'], category,
                                                 f'official source returned HTTP {http_status}'))
                    elif kind == 'network_timeout':
                        failures.append(_failure(worker['source_name'], worker['company_name'], 'timeout',
                                                 'official source connection timed out'))
                    elif kind == 'network_error':
                        failures.append(_failure(worker['source_name'], worker['company_name'], 'network_error',
                                                 'official source connection failed'))
                    elif kind == "oversized":
                        failures.append(
                            _failure(
                                worker["source_name"],
                                worker["company_name"],
                                "oversized",
                                f"source exceeded {max_jobs_per_source} jobs",
                            )
                        )
                    elif kind == "malformed":
                        failures.append(
                            _failure(
                                worker["source_name"],
                                worker["company_name"],
                                "malformed",
                                "source returned malformed data",
                            )
                        )
                    elif kind != "ok":
                        failures.append(
                            _failure(
                                worker["source_name"],
                                worker["company_name"],
                                "source_error",
                                "source failed",
                            )
                        )
                    elif not isinstance(payload, list) or not all(
                        isinstance(job, Job) for job in payload
                    ):
                        failures.append(
                            _failure(
                                worker["source_name"],
                                worker["company_name"],
                                "malformed",
                                "source returned malformed data",
                            )
                        )
                    elif len(payload) > max_jobs_per_source:
                        failures.append(
                            _failure(
                                worker["source_name"],
                                worker["company_name"],
                                "oversized",
                                f"source exceeded {max_jobs_per_source} jobs",
                            )
                        )
                    else:
                        if checkpoint_state is not None:
                            payload = checkpoint_state.complete(worker['company_scope'], payload)
                        jobs.extend(payload)
                        if health is not None:
                            fetched = None
                            if checkpoint_state is not None:
                                fetched = checkpoint_state.document['companies'][checkpoint_state.companies.index(worker['company_scope'])]['fetched_at']
                            health.complete(worker['company_scope'], len(payload), fetched_at=fetched)
                    if health is not None and len(failures) > failures_before:
                        health_category = 'network_timeout' if kind == 'network_timeout' else failures[-1]['category']
                        health.fail(worker['company_scope'], health_category, http_status=http_status)
                    if checkpoint_state is not None and len(failures) > failures_before:
                        checkpoint_state.fail(worker['company_scope'], failures[-1])
                    if checkpoint_state is not None and on_progress is not None:
                        on_progress(checkpoint_state.progress())
                    completed.append(worker)
                    continue

                deadline = (
                    worker["execution_deadline"]
                    if worker["ready"]
                    else worker["startup_deadline"]
                )
                if now >= deadline:
                    failures.append(
                        _failure(
                            worker["source_name"],
                            worker["company_name"],
                            "timeout" if worker["ready"] else "source_error",
                            (
                                f"source exceeded {per_source_timeout:g} seconds"
                                if worker["ready"]
                                else "source failed"
                            ),
                        )
                    )
                    if health is not None:
                        health.fail(worker['company_scope'], failures[-1]['category'])
                    completed.append(worker)
                elif not process.is_alive():
                    failures.append(
                        _failure(
                            worker["source_name"],
                            worker["company_name"],
                            "source_error",
                            "source failed",
                        )
                    )
                    if health is not None:
                        health.fail(worker['company_scope'], failures[-1]['category'])
                    completed.append(worker)
                if checkpoint_state is not None and len(failures) > failures_before:
                    checkpoint_state.fail(worker['company_scope'], failures[-1])
                    if on_progress is not None:
                        on_progress(checkpoint_state.progress())

            for worker in completed:
                _close_worker(worker)
                workers.remove(worker)

            if workers and not completed:
                time.sleep(0.005)
        completed_normally = True
    finally:
        try:
            _close_workers(workers)
        finally:
            if health is not None:
                health.finish(interrupted=not completed_normally)

    failures.sort(
        key=lambda failure: (
            failure["source"].casefold(),
            failure["company"].casefold(),
            failure["category"],
        )
    )

    unique: dict[tuple[str, str], Job] = {}
    for job in sorted(jobs, key=_job_order):
        unique.setdefault(_job_key(job), job)
    normalized_jobs = json.loads(
        json.dumps([asdict(job) for job in unique.values()], ensure_ascii=False)
    )
    result = {
        "contract_version": 1,
        "observed_on": observed_on,
        "mode": mode,
        "incomplete": bool(failures),
        "jobs": normalized_jobs if mode == "best-effort" or not failures else [],
        "failures": failures,
    }
    if checkpoint_state is not None:
        represented_date = max([observed_on] + [job["last_seen"] for job in result["jobs"]])
        latest_date = checkpoint_state.latest_observed_on
        if latest_date > represented_date:
            result["latest_observed_on"] = latest_date
    if mode == "atomic" and failures:
        raise AtomicSourceScanFailed(result)
    _atomic_write_json(output, result)
    return result
