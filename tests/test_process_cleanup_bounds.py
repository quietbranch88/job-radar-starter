from __future__ import annotations

import importlib
import queue
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest


class _StubbornPopen:
    pid = 12345

    def __init__(self) -> None:
        self.killed = False
        self.wait_timeouts: list[float | None] = []

    def poll(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> None:
        self.wait_timeouts.append(timeout)
        if timeout is None:
            raise AssertionError("cleanup wait must always be bounded")
        raise subprocess.TimeoutExpired("provider", timeout)

    def kill(self) -> None:
        self.killed = True


class _StubbornWorker:
    pid = 54321

    def __init__(self) -> None:
        self.killed = False
        self.join_timeouts: list[float | None] = []

    def is_alive(self) -> bool:
        return True

    def join(self, timeout: float | None = None) -> None:
        self.join_timeouts.append(timeout)
        if timeout is None:
            raise AssertionError("cleanup join must always be bounded")

    def kill(self) -> None:
        self.killed = True


def _timed_out_taskkill(*args, timeout=None, **kwargs):
    del args, kwargs
    assert timeout is not None
    assert timeout <= 2
    raise subprocess.TimeoutExpired("taskkill", timeout)


class _OverflowThenBlockedStream:
    def __init__(self) -> None:
        self._reads = 0
        self.release = threading.Event()

    def read(self, size: int) -> bytes:
        assert size > 0
        self._reads += 1
        if self._reads == 1:
            return b"within"
        if self._reads == 2:
            return b"overflow"
        if self._reads == 3:
            assert self.release.wait(timeout=2)
            return b"discarded descendant output"
        return b""


class _CloseWouldBlockStream(_OverflowThenBlockedStream):
    def close(self) -> None:
        if not self.release.is_set():
            raise AssertionError("must not close a pipe while its reader is blocked")


class _ImmediatePipe:
    def read(self, size: int) -> bytes:
        del size
        return b""

    def write(self, payload: bytes) -> int:
        return len(payload)

    def close(self) -> None:
        pass


class _OverflowProcess:
    pid = 24680
    returncode = None
    _handle = 1

    def __init__(self, stdout: _CloseWouldBlockStream) -> None:
        self.stdin = _ImmediatePipe()
        self.stdout = stdout
        self.stderr = _ImmediatePipe()

    def poll(self) -> None:
        return None


class _FakeWindowsJob:
    def assign_handle(self, handle: int) -> None:
        assert handle == 1

    def close(self) -> None:
        pass


def test_output_reader_notifies_overflow_then_drains_without_buffering():
    bounded = importlib.import_module("job_radar.bounded_process")
    stream = _OverflowThenBlockedStream()
    events: queue.Queue[tuple[str, str | None]] = queue.Queue()
    output = bytearray()
    reader = threading.Thread(
        target=bounded._read_bounded,
        args=(stream, "stdout", len(b"within"), events, output),
    )

    reader.start()
    assert events.get(timeout=1) == ("overflow", "stdout")
    assert reader.is_alive(), "reader must keep the pipe drained during cleanup"
    assert events.empty(), "reader_done must wait until the pipe reaches EOF"

    stream.release.set()
    reader.join(timeout=1)

    assert not reader.is_alive()
    assert events.get_nowait() == ("reader_done", "stdout")
    assert events.empty()
    assert output == b"within"


def test_overflow_cleanup_never_closes_a_pipe_with_a_blocked_reader(monkeypatch):
    bounded = importlib.import_module("job_radar.bounded_process")
    stdout = _CloseWouldBlockStream()
    process = _OverflowProcess(stdout)
    monkeypatch.setattr(bounded.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(bounded, "WindowsJob", lambda name: _FakeWindowsJob())
    monkeypatch.setattr(bounded, "resume_suspended_process", lambda pid: None)
    monkeypatch.setattr(
        bounded, "kill_process_tree", lambda candidate, windows_job=None: None
    )
    # Five seconds is only a deadlock failsafe. The implementation must return
    # using its 0.25-second bounded join, before this event releases the reader.
    release = threading.Timer(5, stdout.release.set)
    release.start()
    started = time.monotonic()

    try:
        with pytest.raises(bounded.ProcessOutputLimitError):
            bounded.run_bounded_process(
                ["fake-provider"],
                input_text="{}",
                timeout_seconds=5,
                max_stdout_bytes=len(b"within"),
            )
        assert not stdout.release.is_set()
    finally:
        stdout.release.set()
        release.cancel()

    # The event assertion above is deterministic. This 2-second ceiling allows
    # 1.75 seconds of loaded-host scheduler tolerance over the 0.25-second join.
    assert time.monotonic() - started < 2


def test_bounded_process_cleanup_survives_taskkill_and_final_wait_stalls(
    monkeypatch,
):
    bounded = importlib.import_module("job_radar.bounded_process")
    process = _StubbornPopen()
    monkeypatch.setattr(bounded, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(bounded.subprocess, "run", _timed_out_taskkill)

    bounded.kill_process_tree(process)

    assert process.killed is True
    assert process.wait_timeouts
    assert all(timeout is not None for timeout in process.wait_timeouts)
    assert max(process.wait_timeouts) <= 2


def test_review_worker_cleanup_survives_taskkill_and_final_join_stalls(
    monkeypatch,
):
    supervisor = importlib.import_module("job_radar.review_supervisor")
    process = _StubbornWorker()
    monkeypatch.setattr(supervisor, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(supervisor.subprocess, "run", _timed_out_taskkill)

    supervisor._kill_worker_tree(process)

    assert process.killed is True
    assert process.join_timeouts
    assert all(timeout is not None for timeout in process.join_timeouts)
    assert max(process.join_timeouts) <= 2
