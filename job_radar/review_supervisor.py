"""Run one review callable in a child process and stop it at the deadline."""

from __future__ import annotations

import multiprocessing
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any, Protocol

from job_radar.windows_job import WindowsJob


_TASKKILL_TIMEOUT_SECONDS = 1
_WORKER_JOIN_TIMEOUT_SECONDS = 1


class ReviewRepository(Protocol):
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
    repository: ReviewRepository,
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
