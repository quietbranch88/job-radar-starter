from __future__ import annotations

import importlib
import json
import time
from copy import deepcopy
from io import StringIO
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from job_radar.cli import main


class FakeRepository:
    def __init__(self, state: dict[str, dict[str, Any]] | None = None):
        self.state = deepcopy(state or {})
        self.writes: list[tuple[str, dict[str, Any]]] = []

    def read(self, kind: str) -> dict[str, Any]:
        return deepcopy(
            self.state.get(kind, {"contract_version": 1, "items": []})
        )

    def write(self, kind: str, document: dict[str, Any]) -> None:
        copied = deepcopy(document)
        self.state[kind] = copied
        self.writes.append((kind, copied))


class FakeProvider:
    def __init__(
        self,
        responses: dict[tuple[str, str], dict[str, Any]] | None = None,
        *,
        error: Exception | None = None,
    ):
        self.responses = responses or {}
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def review(self, mode: str, request: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((mode, deepcopy(request)))
        if self.error is not None:
            raise self.error
        return deepcopy(self.responses[(mode, request["stable_id"])])


class TimedFakeProvider(FakeProvider):
    def __init__(
        self,
        responses: dict[tuple[str, str], dict[str, Any]] | None = None,
        *,
        timed_out_ids: set[str] | None = None,
        interrupt_after: int | None = None,
        clock: "ManualClock | None" = None,
        work_seconds: float = 0,
    ):
        super().__init__(responses)
        self.timed_out_ids = timed_out_ids or set()
        self.interrupt_after = interrupt_after
        self.clock = clock
        self.work_seconds = work_seconds
        self.timeouts: list[float] = []

    def review_with_timeout(
        self,
        mode: str,
        request: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        self.calls.append((mode, deepcopy(request)))
        self.timeouts.append(timeout_seconds)
        if self.interrupt_after is not None and len(self.calls) > self.interrupt_after:
            raise KeyboardInterrupt("simulated process interruption")
        if self.clock is not None:
            self.clock.advance(self.work_seconds)
        if (
            request["stable_id"] in self.timed_out_ids
            or self.work_seconds > timeout_seconds
        ):
            raise TimeoutError("provider honored the bounded timeout")
        return deepcopy(self.responses[(mode, request["stable_id"])])


class ManualClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def review() -> ModuleType:
    try:
        return importlib.import_module("job_radar.ai_review")
    except ModuleNotFoundError as error:
        if error.name != "job_radar.ai_review":
            raise
        pytest.fail(
            "production API missing: implement job_radar.ai_review",
            pytrace=False,
        )


def _job(
    stable_id: str,
    *,
    fit: int = 75,
    title: str = "Backend Engineer",
    company: str = "Example Systems",
    country: str = "CA",
    jd_evidence: list[str] | None = None,
    hard_excluded: bool = False,
    hard_reason_codes: list[str] | None = None,
    visa_supported: bool | None = True,
) -> dict[str, Any]:
    return {
        "stable_id": stable_id,
        "title": title,
        "company": company,
        "country": country,
        "local_fit": fit,
        "jd_hash": f"jd-hash-{stable_id}",
        "jd_evidence": jd_evidence
        or [
            "Build reliable backend services.",
            "Operate distributed systems in production.",
        ],
        "hard_excluded": hard_excluded,
        "hard_reason_codes": hard_reason_codes or [],
        "visa_supported": visa_supported,
        "url": f"https://private.example/{stable_id}",
        "full_jd": "PRIVATE FULL JD " * 500,
        "application_history": ["private interview note"],
        "resume": "PRIVATE RESUME CONTENT",
    }


def _response(
    stable_id: str,
    *,
    decision: str = "recommend",
    score: int = 82,
    reason_codes: list[str] | None = None,
    summary: str = "Relevant backend scope.",
) -> dict[str, Any]:
    return {
        "contract_version": 1,
        "stable_id": stable_id,
        "decision": decision,
        "score": score,
        "reason_codes": reason_codes or ["backend_match"],
        "summary": summary,
    }


def _config(**overrides: Any) -> dict[str, Any]:
    config = {
        "contract_version": 1,
        "minimum_fit": 60,
        "max_request_bytes": 1200,
        "max_evidence_items": 3,
        "max_evidence_chars": 240,
        "profile_rubric_hash": "rubric-v1",
        "prompt_version": "prompt-v1",
        "fast_model": "fast-v1",
        "company_fact_ttl_days": 30,
    }
    config.update(overrides)
    return config


def _run(
    review: ModuleType,
    jobs: list[dict[str, Any]],
    fast: FakeProvider | None,
    *,
    repository: FakeRepository | None = None,
    config: dict[str, Any] | None = None,
    observed_on: str = "2026-07-17",
):
    repository = repository or FakeRepository()
    result = review.run_review_pipeline(
        jobs=jobs,
        repository=repository,
        fast_provider=fast,
        config=config or _config(),
        observed_on=observed_on,
    )
    return result, repository


def test_fast_review_sends_only_bounded_minimal_fields(review: ModuleType):
    job = _job("job_01", jd_evidence=["x" * 500, "second sentence"])
    fast = FakeProvider({("fast", "job_01"): _response("job_01")})

    result, _ = _run(review, [job], fast)

    assert result["reviews"][0]["decision"] == "recommend"
    assert len(fast.calls) == 1
    mode, request = fast.calls[0]
    assert mode == "fast"
    assert set(request) == {
        "contract_version",
        "stable_id",
        "title",
        "company",
        "country",
        "local_fit",
        "jd_evidence",
    }
    assert request["stable_id"] == "job_01"
    assert len(request["jd_evidence"]) <= 3
    assert all(len(item) <= 240 for item in request["jd_evidence"])
    serialized = json.dumps(request, ensure_ascii=False)
    assert len(serialized.encode("utf-8")) <= 1200
    for forbidden in (
        "PRIVATE FULL JD",
        "PRIVATE RESUME",
        "private interview",
        "https://private.example",
        "jd-hash-job_01",
        "rubric-v1",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"max_evidence_items": 9}, "max_evidence_items"),
        ({"max_evidence_chars": 501}, "max_evidence_chars"),
    ],
)
def test_public_request_schema_caps_are_runtime_caps(
    review: ModuleType,
    override: dict[str, int],
    message: str,
):
    with pytest.raises(ValueError, match=message):
        _run(
            review,
            [_job("job_01")],
            FakeProvider({("fast", "job_01"): _response("job_01")}),
            config=_config(**override),
        )


@pytest.mark.parametrize(
    ("bad_response", "message"),
    [
        ({**_response("job_01"), "unexpected": True}, "unsupported"),
        ({**_response("job_01"), "score": 101}, "score"),
        ({**_response("job_01"), "decision": "maybe"}, "decision"),
        ({**_response("job_01"), "summary": "x" * 501}, "summary"),
        ({**_response("job_01"), "reason_codes": []}, "reason"),
        ({**_response("other_job")}, "stable_id"),
    ],
)
def test_provider_response_schema_is_strict_and_bounded(
    review: ModuleType,
    bad_response: dict[str, Any],
    message: str,
):
    fast = FakeProvider({("fast", "job_01"): bad_response})

    with pytest.raises(ValueError, match=message):
        _run(review, [_job("job_01")], fast)


def test_provider_failure_is_safe_and_does_not_write_partial_cache(
    review: ModuleType,
):
    repository = FakeRepository()
    fast = FakeProvider(error=RuntimeError("token=PRIVATE at C:\\Users\\Owner"))

    result, repository = _run(
        review,
        [_job("job_01")],
        fast,
        repository=repository,
    )

    assert result["reviews"] == []
    assert result["failures"] == [
        {
            "stable_id": "job_01",
            "mode": "fast",
            "category": "provider_error",
            "message": "review provider failed",
        }
    ]
    assert repository.read("review_cache")["items"] == []
    assert "PRIVATE" not in json.dumps(result)
    assert r"C:\Users" not in json.dumps(result)


def test_hard_exclusion_skips_provider_and_always_wins(review: ModuleType):
    job = _job(
        "job_blocked",
        fit=95,
        hard_excluded=True,
        hard_reason_codes=["excluded_company", "commission_only"],
    )
    fast = FakeProvider()

    result, repository = _run(review, [job], fast)

    assert fast.calls == []
    assert result["reviews"] == []
    assert result["rejected"][0]["reason_codes"] == [
        "commission_only",
        "excluded_company",
    ]
    assert result["rejected"][0]["hard_excluded"] is True
    assert repository.read("rejected")["items"] == result["rejected"]


@pytest.mark.parametrize(
    ("job", "fast_response"),
    [
        (_job("high", fit=80), _response("high", score=84)),
        (
            _job("visa", fit=72, visa_supported=None),
            _response("visa", score=76),
        ),
        (_job("near", fit=62), _response("near", score=63)),
        (
            _job("conflict", fit=76),
            _response("conflict", decision="reject", score=40),
        ),
        (_job("normal", fit=72), _response("normal", score=74)),
    ],
)
def test_single_provider_reviews_every_eligible_case(
    review: ModuleType,
    job: dict[str, Any],
    fast_response: dict[str, Any],
):
    stable_id = job["stable_id"]
    fast = FakeProvider({("fast", stable_id): fast_response})

    result, _ = _run(review, [job], fast)

    assert len(fast.calls) == 1
    assert result["reviews"][0]["decision"] == fast_response["decision"]
    assert result["reviews"][0].get("rescued") is not True
    assert "escalations" not in result["report"]


def test_single_provider_reviews_every_eligible_job_in_fit_order(review: ModuleType):
    jobs = [_job(f"job_{index}", fit=90 - index) for index in range(5)]
    fast = FakeProvider(
        {
            ("fast", job["stable_id"]): _response(job["stable_id"])
            for job in jobs
        }
    )

    result, _ = _run(review, list(reversed(jobs)), fast)

    assert "escalations" not in result["report"]
    assert [request["stable_id"] for _, request in fast.calls] == [
        "job_0",
        "job_1",
        "job_2",
        "job_3",
        "job_4",
    ]


def test_cache_hit_and_versioned_input_invalidation_matrix(review: ModuleType):
    base = {
        "stable_id": "job_01",
        "jd_hash": "jd-v1",
        "profile_rubric_hash": "rubric-v1",
        "prompt_version": "prompt-v1",
        "model": "fast-v1",
        "mode": "fast",
    }
    key = review.review_cache_key(**base)
    assert key == review.review_cache_key(**dict(reversed(list(base.items()))))

    for field, changed in (
        ("stable_id", "job_02"),
        ("jd_hash", "jd-v2"),
        ("profile_rubric_hash", "rubric-v2"),
        ("prompt_version", "prompt-v2"),
        ("model", "fast-v2"),
        ("mode", "strong"),
    ):
        candidate = {**base, field: changed}
        assert review.review_cache_key(**candidate) != key

    repository = FakeRepository()
    fast = FakeProvider({("fast", "job_01"): _response("job_01")})
    first, repository = _run(
        review,
        [_job("job_01")],
        fast,
        repository=repository,
    )
    second, _ = _run(
        review,
        [_job("job_01")],
        fast,
        repository=repository,
    )

    assert len(fast.calls) == 1
    assert first["report"]["provider_calls"] == 1
    assert second["report"]["provider_calls"] == 0
    assert second["report"]["cache_hits"] == 1


def test_changing_each_pipeline_cache_input_causes_a_new_provider_call(
    review: ModuleType,
):
    repository = FakeRepository()
    fast = FakeProvider({("fast", "job_01"): _response("job_01")})
    _run(review, [_job("job_01")], fast, repository=repository)

    variants = [
        (_job("job_01") | {"jd_hash": "changed-jd"}, _config()),
        (_job("job_01"), _config(profile_rubric_hash="rubric-v2")),
        (_job("job_01"), _config(prompt_version="prompt-v2")),
        (_job("job_01"), _config(fast_model="fast-v2")),
    ]
    for job, config in variants:
        _run(
            review,
            [job],
            fast,
            repository=repository,
            config=config,
        )

    assert len(fast.calls) == 1 + len(variants)


def test_company_fact_requires_evidence_observation_date_and_fresh_ttl(
    review: ModuleType,
):
    facts = {
        "contract_version": 1,
        "items": [
            {
                "company": "Example Systems",
                "fact": "visa_support",
                "value": True,
                "evidence_source": "https://example.test/careers",
                "observed_on": "2026-06-20",
                "ttl_days": 30,
            }
        ],
    }
    repository = FakeRepository({"company_facts": facts})
    fast = FakeProvider({("fast", "job_01"): _response("job_01")})

    fresh, _ = _run(
        review,
        [_job("job_01", visa_supported=None)],
        fast,
        repository=repository,
        observed_on="2026-07-17",
    )
    stale, _ = _run(
        review,
        [_job("job_02", visa_supported=None)],
        FakeProvider({("fast", "job_02"): _response("job_02")}),
        repository=repository,
        observed_on="2026-07-21",
    )

    assert fresh["company_facts_used"] == 1
    assert stale["company_facts_used"] == 0

    for invalid in (
        {**facts["items"][0], "evidence_source": ""},
        {**facts["items"][0], "observed_on": ""},
        {**facts["items"][0], "ttl_days": 0},
    ):
        bad_repository = FakeRepository(
            {"company_facts": {"contract_version": 1, "items": [invalid]}}
        )
        with pytest.raises(ValueError, match="evidence|observed|ttl"):
            _run(
                review,
                [_job("job_bad")],
                FakeProvider({("fast", "job_bad"): _response("job_bad")}),
                repository=bad_repository,
            )


def test_rejected_queue_has_complete_reason_codes_and_deterministic_daily_sample(
    review: ModuleType,
):
    rejected = [
        {
            "stable_id": f"job_{index}",
            "reason_codes": [reason],
            "local_fit": 40 + index,
            "country": "CA" if index % 2 else "GB",
            "hard_excluded": index % 3 == 0,
        }
        for index, reason in enumerate(
            ["low_fit", "visa_unknown", "skill_gap", "low_fit", "location"] * 3
        )
    ]

    first = review.deterministic_rejected_sample(
        rejected,
        sample_date="2026-07-17",
        size=6,
    )
    second = review.deterministic_rejected_sample(
        list(reversed(rejected)),
        sample_date="2026-07-17",
        size=6,
    )

    assert first == second
    assert len(first) == 6
    assert all(item["reason_codes"] for item in first)
    assert len({item["reason_codes"][0] for item in first}) >= 3
    assert review.deterministic_rejected_sample(
        rejected,
        sample_date="2026-07-18",
        size=6,
    ) != first


def test_single_provider_rejection_keeps_the_audit_without_rescue(
    review: ModuleType,
):
    job = _job("job_rescue", fit=76)
    fast = FakeProvider(
        {
            ("fast", "job_rescue"): _response(
                "job_rescue",
                decision="reject",
                score=45,
                reason_codes=["skill_gap"],
            )
        }
    )

    result, repository = _run(review, [job], fast)

    assert result["reviews"][0]["decision"] == "reject"
    assert result["reviews"][0].get("rescued") is not True
    audit = repository.read("rejected")["items"]
    assert len(audit) == 1
    assert audit[0]["stable_id"] == "job_rescue"
    assert audit[0]["reason_codes"] == ["skill_gap"]
    assert audit[0]["rescued"] is False


def test_report_counts_calls_and_cache_hits(review: ModuleType):
    repository = FakeRepository()
    jobs = [_job("high", fit=85), _job("normal", fit=72)]
    fast = FakeProvider(
        {
            ("fast", "high"): _response("high"),
            ("fast", "normal"): _response("normal"),
        }
    )

    first, repository = _run(
        review,
        jobs,
        fast,
        repository=repository,
    )
    second, _ = _run(
        review,
        jobs,
        fast,
        repository=repository,
    )

    assert first["report"] == {
        "provider_calls": 2,
        "cache_hits": 0,
    }
    assert second["report"] == {
        "provider_calls": 0,
        "cache_hits": 2,
    }


def test_cli_review_without_provider_is_no_network_and_reports_skipped(
    review: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    jobs = tmp_path / "jobs.json"
    output = tmp_path / "review.json"
    jobs.write_text(json.dumps([_job("job_01")]), encoding="utf-8")
    stdout = StringIO()

    def forbidden_network(*args, **kwargs):
        raise AssertionError("network must not be used")

    monkeypatch.setattr("urllib.request.urlopen", forbidden_network)
    status = main(
        [
            "--json",
            "review",
            "--jobs",
            str(jobs),
            "--output",
            str(output),
        ],
        stdout=stdout,
    )

    report = json.loads(stdout.getvalue())
    assert status == 0
    assert report["ok"] is True
    assert report["operation"] == "review"
    assert report["mode"] == "disabled"
    assert report["counts"] == {
        "cache_hits": 0,
        "provider_calls": 0,
        "reviewed": 0,
    }
    assert json.loads(output.read_text(encoding="utf-8"))["reviews"] == []


def test_timed_provider_contract_enforces_timeout_without_unbounded_thread(
    review: ModuleType,
):
    provider = TimedFakeProvider(
        {("fast", "job_hang"): _response("job_hang")},
        timed_out_ids={"job_hang"},
    )
    started = time.monotonic()

    result, _ = _run(
        review,
        [_job("job_hang")],
        provider,
        config=_config(
            per_call_timeout_seconds=2,
            total_deadline_seconds=10,
            max_fast_calls=10,
        ),
    )

    assert time.monotonic() - started < 0.5
    assert provider.timeouts == [2]
    assert result["reviews"] == []
    assert result["failures"] == [
        {
            "stable_id": "job_hang",
            "mode": "fast",
            "category": "provider_timeout",
            "message": "review provider timed out",
        }
    ]


def test_total_deadline_and_fast_call_budget_stop_deterministically(
    review: ModuleType,
):
    clock = ManualClock()
    jobs = [_job(f"job_{index}", fit=90 - index) for index in range(4)]
    provider = TimedFakeProvider(
        {
            ("fast", job["stable_id"]): _response(job["stable_id"])
            for job in jobs
        },
        clock=clock,
        work_seconds=2,
    )

    result = review.run_review_pipeline(
        jobs=jobs,
        repository=FakeRepository(),
        fast_provider=provider,
        config=_config(
            per_call_timeout_seconds=10,
            total_deadline_seconds=3,
            max_fast_calls=3,
        ),
        observed_on="2026-07-17",
        monotonic=clock,
    )

    assert [request["stable_id"] for _, request in provider.calls] == [
        "job_0",
        "job_1",
    ]
    assert provider.timeouts == [3, 1]
    assert result["report"]["provider_calls"] == 2
    assert result["report"]["fast_call_budget_exhausted"] is False
    assert result["report"]["deadline_exhausted"] is True
    assert result["failures"][-1] == {
        "stable_id": "job_1",
        "mode": "fast",
        "category": "total_deadline",
        "message": "review total deadline exhausted",
    }

    budget_provider = TimedFakeProvider(
        {
            ("fast", job["stable_id"]): _response(job["stable_id"])
            for job in jobs
        }
    )
    budget_result, _ = _run(
        review,
        jobs,
        budget_provider,
        config=_config(
            per_call_timeout_seconds=10,
            total_deadline_seconds=60,
            max_fast_calls=2,
        ),
    )
    assert [request["stable_id"] for _, request in budget_provider.calls] == [
        "job_0",
        "job_1",
    ]
    assert budget_result["report"]["fast_call_budget_exhausted"] is True


def test_completed_cache_and_rejected_progress_survive_interrupted_replay(
    review: ModuleType,
):
    jobs = [_job("job_first", fit=80), _job("job_second", fit=70)]
    repository = FakeRepository()
    interrupted = TimedFakeProvider(
        {
            ("fast", "job_first"): _response(
                "job_first",
                decision="reject",
                reason_codes=["skill_gap"],
            ),
            ("fast", "job_second"): _response("job_second"),
        },
        interrupt_after=1,
    )

    with pytest.raises(KeyboardInterrupt, match="simulated process interruption"):
        _run(
            review,
            jobs,
            interrupted,
            repository=repository,
            config=_config(
                per_call_timeout_seconds=10,
                total_deadline_seconds=60,
                max_fast_calls=10,
            ),
        )

    assert len(repository.read("review_cache")["items"]) == 1
    rejected = repository.read("rejected")["items"]
    assert [(item["stable_id"], item["rescued"]) for item in rejected] == [
        ("job_first", False)
    ]

    replay = TimedFakeProvider(
        {
            ("fast", "job_first"): _response("job_first"),
            ("fast", "job_second"): _response("job_second"),
        }
    )
    result, _ = _run(
        review,
        jobs,
        replay,
        repository=repository,
        config=_config(
            per_call_timeout_seconds=10,
            total_deadline_seconds=60,
            max_fast_calls=10,
        ),
    )

    assert [request["stable_id"] for _, request in replay.calls] == ["job_second"]
    assert result["report"]["cache_hits"] == 1


def test_first_new_cache_entry_is_durable_when_prior_cache_exists(
    review: ModuleType,
):
    repository = FakeRepository()
    seeded = TimedFakeProvider(
        {("fast", "job_seed"): _response("job_seed")}
    )
    _run(
        review,
        [_job("job_seed", fit=80)],
        seeded,
        repository=repository,
        config=_config(
            per_call_timeout_seconds=10,
            total_deadline_seconds=60,
            max_fast_calls=10,
        ),
    )
    interrupted = TimedFakeProvider(
        {
            ("fast", "job_first"): _response("job_first"),
            ("fast", "job_second"): _response("job_second"),
        },
        interrupt_after=1,
    )

    with pytest.raises(KeyboardInterrupt, match="simulated process interruption"):
        _run(
            review,
            [_job("job_first", fit=80), _job("job_second", fit=70)],
            interrupted,
            repository=repository,
            config=_config(
                per_call_timeout_seconds=10,
                total_deadline_seconds=60,
                max_fast_calls=10,
            ),
        )

    assert {
        item["stable_id"]
        for item in repository.read("review_cache")["items"]
    } == {"job_seed", "job_first"}


def test_two_thousand_hard_rejects_use_amortized_durable_checkpoints(
    review: ModuleType,
):
    jobs = [
        _job(
            f"job_{index:04d}",
            fit=10,
            hard_excluded=True,
            hard_reason_codes=["blocked"],
        )
        for index in range(2_000)
    ]
    repository = FakeRepository()
    started = time.monotonic()

    result, _ = _run(
        review,
        jobs,
        None,
        repository=repository,
        config=_config(
            per_call_timeout_seconds=10,
            total_deadline_seconds=60,
            max_fast_calls=10,
        ),
    )

    rejected_writes = [item for kind, item in repository.writes if kind == "rejected"]
    assert len(result["rejected"]) == 2_000
    assert len(repository.read("rejected")["items"]) == 2_000
    assert len(rejected_writes) <= 25
    # The write-count assertion is the deterministic complexity guard. Keep a
    # generous wall-clock ceiling that still catches the former ~87s quadratic
    # implementation without failing under loaded shared CI hosts.
    assert time.monotonic() - started < 10


def test_cache_and_rejected_retention_are_bounded_and_preserve_recent_rescue(
    review: ModuleType,
):
    repository = FakeRepository(
        {
            "review_cache": {
                "contract_version": 1,
                "items": [
                    {"key": "old", "cached_on": "2026-05-01"},
                    {"key": "current", "cached_on": "2026-07-01"},
                ],
            },
            "rejected": {
                "contract_version": 1,
                "items": [
                    {
                        "stable_id": "old",
                        "reason_codes": ["skill_gap"],
                        "observed_on": "2026-03-01",
                        "rescued": True,
                    },
                    {
                        "stable_id": "recent-rescue",
                        "reason_codes": ["skill_gap"],
                        "observed_on": "2026-06-01",
                        "rescued": True,
                    },
                    {
                        "stable_id": "recent-reject",
                        "reason_codes": ["location"],
                        "observed_on": "2026-06-02",
                        "rescued": False,
                    },
                ],
            },
        }
    )

    _run(
        review,
        [],
        None,
        repository=repository,
        config=_config(),
    )

    assert [item["key"] for item in repository.read("review_cache")["items"]] == [
        "current"
    ]
    assert [
        (item["stable_id"], item["rescued"])
        for item in repository.read("rejected")["items"]
    ] == [
        ("recent-rescue", True),
        ("recent-reject", False),
    ]


def test_history_heavy_rejected_audit_is_capped_after_date_pruning(
    review: ModuleType,
):
    history = [
        {
            "stable_id": f"job_{index:05d}",
            "reason_codes": ["skill_gap"],
            "local_fit": 40,
            "country": "CA",
            "hard_excluded": False,
            "observed_on": f"2026-07-{1 + index % 16:02d}",
            "rescued": index % 17 == 0,
        }
        for index in range(review.DEFAULT_MAX_REJECTED_ITEMS + 2_000)
    ]
    history.extend(
        {
            "stable_id": f"old_{index:05d}",
            "reason_codes": ["old"],
            "local_fit": 10,
            "country": "CA",
            "hard_excluded": True,
            "observed_on": "2026-03-01",
            "rescued": False,
        }
        for index in range(2_000)
    )
    repository = FakeRepository(
        {"rejected": {"contract_version": 1, "items": history}}
    )
    started = time.monotonic()

    _run(review, [], None, repository=repository, config=_config())

    document = repository.read("rejected")
    serialized = json.dumps(
        document,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8") + b"\n"
    assert len(document["items"]) == review.DEFAULT_MAX_REJECTED_ITEMS
    assert len(serialized) <= review.DEFAULT_MAX_REJECTED_BYTES
    assert not any(item["stable_id"].startswith("old_") for item in document["items"])
    assert {item["observed_on"] for item in document["items"]} >= {
        "2026-07-15",
        "2026-07-16",
    }
    assert time.monotonic() - started < 5


def test_rejected_audit_serialized_byte_cap_keeps_newest_records_deterministically(
    review: ModuleType,
):
    large_reason = "x" * 200_000
    items = [
        {
            "stable_id": f"job_{index:03d}",
            "reason_codes": [large_reason, f"reason_{index:03d}"],
            "local_fit": 40,
            "country": "CA",
            "hard_excluded": False,
            "observed_on": f"2026-07-{1 + index % 16:02d}",
            "rescued": False,
        }
        for index in range(60)
    ]
    repository = FakeRepository(
        {"rejected": {"contract_version": 1, "items": items}}
    )

    _run(review, [], None, repository=repository, config=_config())

    document = repository.read("rejected")
    serialized = json.dumps(
        document,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8") + b"\n"
    assert len(serialized) <= review.DEFAULT_MAX_REJECTED_BYTES
    assert len(document["items"]) < len(items)
    assert max(item["observed_on"] for item in document["items"]) == "2026-07-16"
    assert document == repository.read("rejected")


def test_current_rejected_lookup_consumes_jobs_once_at_scale(review: ModuleType):
    class CountingJobs:
        def __init__(self, size: int):
            self.size = size
            self.iterations = 0

        def __iter__(self):
            self.iterations += 1
            for index in range(self.size):
                yield {"stable_id": f"job_{index}"}

    jobs = CountingJobs(5_000)
    history = [
        {
            "stable_id": f"job_{index}",
            "observed_on": "2026-07-17",
        }
        for index in range(10_000)
    ]

    current = review.current_rejected_for_jobs(
        history,
        jobs,
        observed_on="2026-07-17",
    )

    assert jobs.iterations == 1
    assert len(current) == 5_000
