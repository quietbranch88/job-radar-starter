"""Child processes that fetch one official board and stop when the parent asks."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from multiprocessing.connection import Connection
from typing import Any
from urllib.error import HTTPError, URLError

from job_radar.adapters import (
    Job,
    SourcePayloadTooLarge,
    fetch_ashby,
    fetch_greenhouse,
    fetch_lever,
)
from job_radar.posting_country import posting_country


_FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
}
_WORKER_STARTUP_TIMEOUT_SECONDS = 10
_WORKER_TERMINATE_GRACE_SECONDS = 1
_WORKER_KILL_GRACE_SECONDS = 1


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
        result_connection.send(
            (
                "ok",
                _fetch_scoped_jobs(
                    source_name,
                    board,
                    company_name,
                    country,
                    get_json,
                    max_jobs_per_source,
                ),
            )
        )
    except HTTPError as error:
        result_connection.send(("http_error", error.code))
    except TimeoutError:
        result_connection.send(("network_timeout", None))
    except URLError as error:
        kind = "network_timeout" if isinstance(error.reason, TimeoutError) else "network_error"
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


def _fetch_scoped_jobs(
    source_name: str,
    board: str,
    company_name: str,
    country_scope: str,
    get_json: Callable[[str], Any],
    max_jobs_per_source: int,
) -> list[Job]:
    fetched = _FETCHERS[source_name](
        board,
        company_name,
        get_json,
        max_jobs=max_jobs_per_source,
    )
    return [
        replace(job, country=posting_country(country_scope, job.location))
        for job in fetched
    ]


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
