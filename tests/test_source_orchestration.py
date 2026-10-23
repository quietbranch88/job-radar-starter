from __future__ import annotations

import importlib
import json
import multiprocessing
import os
import signal
import time
from datetime import date
from io import StringIO
from pathlib import Path
from types import ModuleType

import pytest

from job_radar.catalog import Catalog, Company, Source
from job_radar.cli import main


def fake_mixed_get_json(url: str):
    if url.endswith("/healthy/jobs?content=true"):
        return {
            "jobs": [
                {
                    "id": 7,
                    "title": "Backend Engineer",
                    "absolute_url": "https://example.test/jobs/7",
                    "location": {"name": "Canada"},
                }
            ]
        }
    if url.endswith("/duplicate/jobs?content=true"):
        return {
            "jobs": [
                {
                    "id": 7,
                    "title": "Backend Engineer duplicate",
                    "absolute_url": "https://example.test/jobs/7",
                    "location": {"name": "Canada"},
                }
            ]
        }
    if url.endswith("/broken/jobs?content=true"):
        raise RuntimeError(
            "upstream rejected token=PRIVATE_TOKEN at C:\\Users\\Private\\source.py"
        )
    raise AssertionError(url)


def fake_hanging_get_json(url: str):
    if url.endswith("/healthy/jobs?content=true"):
        return {
            "jobs": [
                {
                    "id": 1,
                    "title": "Platform Engineer",
                    "absolute_url": "https://example.test/jobs/1",
                    "location": {"name": "Remote"},
                }
            ]
        }
    if url.endswith("/hanging/jobs?content=true"):
        time.sleep(30)
        return {"jobs": []}
    raise AssertionError(url)


def fake_sigterm_ignoring_get_json(url: str):
    if os.name != "nt":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(30)
    return {"jobs": []}


def fake_malformed_get_json(url: str):
    return {"jobs": "not-a-list"}


def fake_oversized_get_json(url: str):
    return {
        "jobs": [
            {
                "id": index,
                "title": f"Engineer {index}",
                "absolute_url": f"https://example.test/jobs/{index}",
                "location": {"name": "Remote"},
            }
            for index in range(4)
        ]
    }


def fake_all_healthy_get_json(url: str):
    board = url.rsplit("/", 2)[-2]
    return {
        "jobs": [
            {
                "id": board,
                "title": f"{board.title()} Engineer",
                "absolute_url": f"https://example.test/jobs/{board}",
                "location": {"name": "Remote"},
            }
        ]
    }


def fake_concurrency_probe_get_json(url: str):
    probe_dir = Path(os.environ["JOB_RADAR_CONCURRENCY_PROBE"])
    board = url.rsplit("/", 2)[-2]
    marker = probe_dir / f"{board}.active"
    marker.write_text(board, encoding="utf-8")
    active = len(list(probe_dir.glob("*.active")))
    (probe_dir / f"{board}.{active}.observed").write_text("", encoding="utf-8")
    time.sleep(0.4)
    marker.unlink()
    return {
        "jobs": [
            {
                "id": board,
                "title": f"{board.title()} Engineer",
                "absolute_url": f"https://example.test/jobs/{board}",
                "location": {"name": "Remote"},
            }
        ]
    }


def fake_near_limit_get_json(url: str):
    return {
        "jobs": [
            {
                "id": index,
                "title": f"Backend Engineer {index}",
                "absolute_url": f"https://example.test/jobs/{index}",
                "location": {"name": "Remote"},
            }
            for index in range(9_500)
        ]
    }


class _ExplodingOversizedList(list):
    def __iter__(self):
        raise AssertionError("oversized response must be rejected before iteration")


def fake_producer_oversized_get_json(url: str):
    return {
        "jobs": _ExplodingOversizedList(
            [
                {
                    "id": index,
                    "title": f"Backend {index}",
                    "absolute_url": f"https://example.test/jobs/{index}",
                    "location": {"name": "Toronto"},
                }
                for index in range(3)
            ]
        )
    }


def fake_rolling_mixed_get_json(url: str):
    board = url.rsplit("/", 2)[-2]
    if board.startswith("healthy"):
        return {
            "jobs": [
                {
                    "id": board,
                    "title": f"{board.title()} Engineer",
                    "absolute_url": f"https://example.test/jobs/{board}",
                    "location": {"name": "Remote"},
                }
            ]
        }
    if board.startswith("hanging"):
        time.sleep(30)
        return {"jobs": []}
    if board.startswith("broken"):
        raise RuntimeError("private upstream detail must not escape")
    raise AssertionError(url)


@pytest.fixture()
def orchestration() -> ModuleType:
    try:
        return importlib.import_module("job_radar.source_orchestration")
    except ModuleNotFoundError as error:
        if error.name != "job_radar.source_orchestration":
            raise
        pytest.fail(
            "production API missing: implement job_radar.source_orchestration",
            pytrace=False,
        )


def _catalog(*companies: tuple[str, str]) -> Catalog:
    return Catalog(
        countries={},
        sources={
            "greenhouse": Source(
                "official_api",
                "https://developer.greenhouse.io/job-board.html",
                True,
            )
        },
        companies={
            company: Company(("CA",), "greenhouse", board)
            for company, board in companies
        },
    )


def test_best_effort_writes_healthy_partial_output_with_structured_safe_failure(
    orchestration: ModuleType,
    tmp_path: Path,
):
    output = tmp_path / "latest.json"

    result = orchestration.scan_catalog_resilient(
        _catalog(("Broken Board", "broken"), ("Healthy Board", "healthy")),
        fake_mixed_get_json,
        mode="best-effort",
        per_source_timeout=2,
        output=output,
    )

    assert result["contract_version"] == 1
    assert result["mode"] == "best-effort"
    assert result["incomplete"] is True
    assert [(job["company"], job["external_id"]) for job in result["jobs"]] == [
        ("Healthy Board", "7")
    ]
    assert result["failures"] == [
        {
            "source": "greenhouse",
            "company": "Broken Board",
            "category": "source_error",
            "message": "source failed",
        }
    ]
    serialized = output.read_text(encoding="utf-8")
    assert json.loads(serialized) == result
    assert "PRIVATE_TOKEN" not in serialized
    assert r"C:\Users" not in serialized


def test_hanging_source_is_terminated_promptly_without_killing_test_process(
    orchestration: ModuleType,
    tmp_path: Path,
):
    output = tmp_path / "latest.json"
    started = time.monotonic()

    result = orchestration.scan_catalog_resilient(
        _catalog(("Hanging Board", "hanging"), ("Healthy Board", "healthy")),
        fake_hanging_get_json,
        mode="best-effort",
        per_source_timeout=0.2,
        output=output,
    )

    elapsed = time.monotonic() - started
    assert elapsed < 5
    assert [job["external_id"] for job in result["jobs"]] == ["1"]
    assert result["incomplete"] is True
    assert result["failures"] == [
        {
            "source": "greenhouse",
            "company": "Hanging Board",
            "category": "timeout",
            "message": "source exceeded 0.2 seconds",
        }
    ]


class _TerminateIgnoringProcess:
    def __init__(self):
        self.alive = True
        self.terminated = False
        self.killed = False
        self.join_timeouts: list[float | None] = []
        self.closed = False

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.terminated = True

    def join(self, timeout=None):
        self.join_timeouts.append(timeout)
        if self.killed:
            self.alive = False

    def kill(self):
        self.killed = True

    def close(self):
        self.closed = True


class _ConnectionProbe:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_worker_cleanup_escalates_from_bounded_terminate_to_kill(
    orchestration: ModuleType,
):
    process = _TerminateIgnoringProcess()
    connection = _ConnectionProbe()

    orchestration._close_worker(
        {"process": process, "connection": connection}
    )

    assert process.terminated is True
    assert process.killed is True
    assert process.join_timeouts
    assert all(timeout is not None for timeout in process.join_timeouts)
    assert max(process.join_timeouts) <= 5
    assert process.closed is True
    assert connection.closed is True


@pytest.mark.skipif(os.name == "nt", reason="SIGTERM semantics require POSIX")
def test_sigterm_ignoring_source_is_force_killed_after_timeout(
    orchestration: ModuleType,
    tmp_path: Path,
):
    before = {child.pid for child in multiprocessing.active_children()}
    started = time.monotonic()

    result = orchestration.scan_catalog_resilient(
        _catalog(("Ignoring Board", "ignoring")),
        fake_sigterm_ignoring_get_json,
        mode="best-effort",
        per_source_timeout=0.2,
        max_concurrency=1,
        output=tmp_path / "latest.json",
    )

    assert time.monotonic() - started < 5
    assert result["failures"][0]["category"] == "timeout"
    assert {child.pid for child in multiprocessing.active_children()} == before


@pytest.mark.parametrize(
    ("getter", "category", "message", "maximum"),
    [
        (fake_malformed_get_json, "malformed", "source returned malformed data", 10),
        (fake_oversized_get_json, "oversized", "source exceeded 2 jobs", 2),
    ],
)
def test_malformed_and_oversized_sources_are_isolated_as_failures(
    orchestration: ModuleType,
    tmp_path: Path,
    getter,
    category: str,
    message: str,
    maximum: int,
):
    result = orchestration.scan_catalog_resilient(
        _catalog(("Unsafe Board", "unsafe")),
        getter,
        mode="best-effort",
        per_source_timeout=2,
        max_jobs_per_source=maximum,
        output=tmp_path / "latest.json",
    )

    assert result["jobs"] == []
    assert result["incomplete"] is True
    assert result["failures"] == [
        {
            "source": "greenhouse",
            "company": "Unsafe Board",
            "category": category,
            "message": message,
        }
    ]


def test_complete_scan_has_versioned_schema_and_no_failures(
    orchestration: ModuleType,
    tmp_path: Path,
):
    result = orchestration.scan_catalog_resilient(
        _catalog(("Beta Board", "beta"), ("Alpha Board", "alpha")),
        fake_all_healthy_get_json,
        mode="atomic",
        per_source_timeout=2,
        output=tmp_path / "latest.json",
    )

    assert set(result) == {
        "contract_version",
        "observed_on",
        "mode",
        "incomplete",
        "jobs",
        "failures",
    }
    assert result["contract_version"] == 1
    assert result["observed_on"] == date.today().isoformat()
    assert result["mode"] == "atomic"
    assert result["incomplete"] is False
    assert result["failures"] == []


def test_reordered_catalog_produces_identical_order_and_stable_deduplication(
    orchestration: ModuleType,
    tmp_path: Path,
):
    first = orchestration.scan_catalog_resilient(
        _catalog(("Zulu Board", "duplicate"), ("Alpha Board", "healthy")),
        fake_mixed_get_json,
        mode="best-effort",
        per_source_timeout=2,
        output=tmp_path / "first.json",
    )
    second = orchestration.scan_catalog_resilient(
        _catalog(("Alpha Board", "healthy"), ("Zulu Board", "duplicate")),
        fake_mixed_get_json,
        mode="best-effort",
        per_source_timeout=2,
        output=tmp_path / "second.json",
    )

    assert first == second
    assert len(first["jobs"]) == 1
    assert first["jobs"][0]["company"] == "Alpha Board"


def test_atomic_failure_raises_and_does_not_replace_last_complete_output(
    orchestration: ModuleType,
    tmp_path: Path,
):
    output = tmp_path / "latest.json"
    previous = {
        "contract_version": 1,
        "mode": "atomic",
        "incomplete": False,
        "jobs": [{"external_id": "previous"}],
        "failures": [],
    }
    output.write_text(json.dumps(previous), encoding="utf-8")

    with pytest.raises(orchestration.AtomicSourceScanFailed) as error:
        orchestration.scan_catalog_resilient(
            _catalog(("Broken Board", "broken"), ("Healthy Board", "healthy")),
            fake_mixed_get_json,
            mode="atomic",
            per_source_timeout=2,
            output=output,
        )

    assert error.value.result["incomplete"] is True
    assert error.value.result["jobs"] == []
    assert error.value.exit_code != 0
    assert json.loads(output.read_text(encoding="utf-8")) == previous
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("mode", ["", "partial", "ATOMIC", None])
def test_mode_must_be_explicit_and_allowlisted(
    orchestration: ModuleType,
    tmp_path: Path,
    mode,
):
    with pytest.raises(ValueError, match="mode"):
        orchestration.scan_catalog_resilient(
            _catalog(("Healthy Board", "healthy")),
            fake_mixed_get_json,
            mode=mode,
            per_source_timeout=2,
            output=tmp_path / "latest.json",
        )


def _write_cli_catalog(path: Path) -> None:
    path.write_text(
        """
countries:
  CA: {name: Canada, sources: [greenhouse]}
sources:
  greenhouse:
    kind: official_api
    terms_url: https://developer.greenhouse.io/job-board.html
    enabled: true
companies:
  Broken Board:
    countries: [CA]
    source: greenhouse
    board: broken
  Healthy Board:
    countries: [CA]
    source: greenhouse
    board: healthy
""".strip(),
        encoding="utf-8",
    )


def test_cli_scan_best_effort_returns_zero_and_reports_incomplete_partial_output(
    orchestration: ModuleType,
    tmp_path: Path,
):
    catalog = tmp_path / "catalog.yaml"
    output = tmp_path / "latest.json"
    _write_cli_catalog(catalog)
    stdout = StringIO()

    status = main(
        [
            "--json",
            "scan",
            "--catalog",
            str(catalog),
            "--output",
            str(output),
            "--mode",
            "best-effort",
            "--source-timeout",
            "2",
        ],
        get_json=fake_mixed_get_json,
        stdout=stdout,
    )

    report = json.loads(stdout.getvalue())
    assert status == 0
    assert report["ok"] is True
    assert report["incomplete"] is True
    assert report["counts"] == {"failures": 1, "scanned": 1}
    assert json.loads(output.read_text(encoding="utf-8"))["incomplete"] is True


def test_cli_scan_atomic_returns_nonzero_and_preserves_previous_output(
    orchestration: ModuleType,
    tmp_path: Path,
):
    catalog = tmp_path / "catalog.yaml"
    output = tmp_path / "latest.json"
    _write_cli_catalog(catalog)
    output.write_text('{"previous":true}', encoding="utf-8")
    stdout = StringIO()

    status = main(
        [
            "--json",
            "scan",
            "--catalog",
            str(catalog),
            "--output",
            str(output),
            "--mode",
            "atomic",
            "--source-timeout",
            "2",
        ],
        get_json=fake_mixed_get_json,
        stdout=stdout,
    )

    report = json.loads(stdout.getvalue())
    assert status != 0
    assert report["ok"] is False
    assert report["incomplete"] is True
    assert report["failures"][0]["category"] == "source_error"
    assert output.read_text(encoding="utf-8") == '{"previous":true}'


def test_cli_scan_exposes_the_source_concurrency_ceiling(tmp_path: Path):
    catalog = tmp_path / "catalog.yaml"
    output = tmp_path / "latest.json"
    _write_cli_catalog(catalog)
    captured: dict[str, object] = {}

    def fake_scan_runner(*args, **kwargs):
        captured.update(kwargs)
        return {
            "contract_version": 1,
            "mode": "atomic",
            "incomplete": False,
            "jobs": [],
            "failures": [],
        }

    status = main(
        [
            "--json",
            "scan",
            "--catalog",
            str(catalog),
            "--output",
            str(output),
            "--max-source-concurrency",
            "3",
        ],
        scan_runner=fake_scan_runner,
        stdout=StringIO(),
    )

    assert status == 0
    assert captured["max_concurrency"] == 3


def test_source_orchestration_does_not_use_process_wide_exit(
    orchestration: ModuleType,
):
    source = Path(orchestration.__file__).read_text(encoding="utf-8")

    assert "os._exit" not in source


@pytest.mark.parametrize("maximum", [None, True, 0, -1, 1.5, "3"])
def test_max_concurrency_must_be_a_positive_integer(
    orchestration: ModuleType,
    tmp_path: Path,
    maximum,
):
    with pytest.raises(ValueError, match="max_concurrency"):
        orchestration.scan_catalog_resilient(
            _catalog(("Healthy Board", "healthy")),
            fake_all_healthy_get_json,
            mode="best-effort",
            per_source_timeout=2,
            max_concurrency=maximum,
            output=tmp_path / "latest.json",
        )


def test_bounded_scheduler_enforces_ceiling_and_rolls_through_full_catalog(
    orchestration: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    monkeypatch.setenv("JOB_RADAR_CONCURRENCY_PROBE", str(probe_dir))

    result = orchestration.scan_catalog_resilient(
        _catalog(*((f"Board {index}", f"board-{index}") for index in range(8))),
        fake_concurrency_probe_get_json,
        mode="best-effort",
        per_source_timeout=2,
        max_concurrency=3,
        output=tmp_path / "latest.json",
    )

    observed = [
        int(path.name.split(".")[1])
        for path in probe_dir.glob("*.observed")
    ]
    assert len(result["jobs"]) == 8
    assert result["failures"] == []
    assert max(observed) <= 3
    assert max(observed) >= 2
    assert not list(probe_dir.glob("*.active"))


def test_near_limit_successful_payload_is_drained_without_false_timeout_or_deadlock(
    orchestration: ModuleType,
    tmp_path: Path,
):
    started = time.monotonic()

    result = orchestration.scan_catalog_resilient(
        _catalog(("Large Board", "large")),
        fake_near_limit_get_json,
        mode="best-effort",
        per_source_timeout=2,
        max_jobs_per_source=10_000,
        max_concurrency=1,
        output=tmp_path / "latest.json",
    )

    assert time.monotonic() - started < 15
    assert len(result["jobs"]) == 9_500
    assert result["incomplete"] is False
    assert result["failures"] == []


def test_oversized_response_is_rejected_in_producer_before_job_materialization(
    orchestration: ModuleType,
    tmp_path: Path,
):
    result = orchestration.scan_catalog_resilient(
        _catalog(("Oversized Board", "oversized")),
        fake_producer_oversized_get_json,
        mode="best-effort",
        per_source_timeout=2,
        max_jobs_per_source=2,
        max_concurrency=1,
        output=tmp_path / "latest.json",
    )

    assert result["jobs"] == []
    assert result["failures"] == [
        {
            "source": "greenhouse",
            "company": "Oversized Board",
            "category": "oversized",
            "message": "source exceeded 2 jobs",
        }
    ]


def test_rolling_workers_keep_mixed_output_deterministic_and_leave_no_children(
    orchestration: ModuleType,
    tmp_path: Path,
):
    catalog = _catalog(
        ("Healthy A", "healthy-a"),
        ("Hanging A", "hanging-a"),
        ("Broken B", "broken-b"),
    )
    before = {child.pid for child in multiprocessing.active_children()}
    started = time.monotonic()

    result = orchestration.scan_catalog_resilient(
        catalog,
        fake_rolling_mixed_get_json,
        mode="best-effort",
        per_source_timeout=0.2,
        max_concurrency=2,
        output=tmp_path / "first.json",
    )

    assert time.monotonic() - started < 10
    assert [job["company"] for job in result["jobs"]] == ["Healthy A"]
    assert result["failures"] == [
        {
            "source": "greenhouse",
            "company": "Broken B",
            "category": "source_error",
            "message": "source failed",
        },
        {
            "source": "greenhouse",
            "company": "Hanging A",
            "category": "timeout",
            "message": "source exceeded 0.2 seconds",
        },
    ]
    assert {
        child.pid for child in multiprocessing.active_children()
    } == before
