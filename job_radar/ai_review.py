"""Provider-neutral, compact job review pipeline.

Safe providers implement ``review_with_timeout(mode, request,
timeout_seconds=...)``. This module never imports a provider SDK or opens a
network connection.
"""

from __future__ import annotations

import hashlib
import json
import time
from copy import deepcopy
from datetime import date, timedelta
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence


class ReviewProvider(Protocol):
    def review(self, mode: str, request: dict[str, Any]) -> dict[str, Any]: ...


class ReviewRepository(Protocol):
    def read(self, kind: str) -> dict[str, Any]: ...

    def write(self, kind: str, document: dict[str, Any]) -> None: ...


_RESPONSE_FIELDS = {
    "contract_version",
    "stable_id",
    "decision",
    "score",
    "reason_codes",
    "summary",
}
_DECISIONS = {"recommend", "reject"}
DEFAULT_PER_CALL_TIMEOUT_SECONDS = 30
DEFAULT_TOTAL_DEADLINE_SECONDS = 300
DEFAULT_MAX_FAST_CALLS = 100
PUBLIC_MAX_EVIDENCE_ITEMS = 8
PUBLIC_MAX_EVIDENCE_CHARS = 500
DEFAULT_CACHE_RETENTION_DAYS = 30
DEFAULT_REJECTED_RETENTION_DAYS = 90
DEFAULT_MAX_REJECTED_ITEMS = 10_000
DEFAULT_MAX_REJECTED_BYTES = 8 * 1024 * 1024
CHECKPOINT_BATCH_SIZE = 100


def _canonical_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def review_cache_key(
    *,
    stable_id: str,
    jd_hash: str,
    profile_rubric_hash: str,
    prompt_version: str,
    model: str,
    mode: str,
) -> str:
    """Return a stable key over every input that can change a review."""

    return _canonical_digest(
        {
            "stable_id": stable_id,
            "jd_hash": jd_hash,
            "profile_rubric_hash": profile_rubric_hash,
            "prompt_version": prompt_version,
            "model": model,
            "mode": mode,
        }
    )


def _state_items(repository: ReviewRepository, kind: str) -> list[dict[str, Any]]:
    document = repository.read(kind)
    if not isinstance(document, dict) or document.get("contract_version") != 1:
        raise ValueError(f"{kind} state has unsupported contract version")
    items = document.get("items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise ValueError(f"{kind} state items must be objects")
    return deepcopy(items)


def _bounded_text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()[:maximum]


def _validate_config(config: Mapping[str, Any]) -> None:
    if config.get("contract_version") != 1:
        raise ValueError("review config has unsupported contract version")
    for field in (
        "minimum_fit",
        "near_threshold_margin",
        "strong_fit_threshold",
        "max_escalations",
        "max_request_bytes",
        "max_evidence_items",
        "max_evidence_chars",
        "company_fact_ttl_days",
    ):
        value = config.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{field} must be a positive integer")
    if not 0 <= config["minimum_fit"] <= 100:
        raise ValueError("minimum_fit must be between 0 and 100")
    if not 0 <= config["strong_fit_threshold"] <= 100:
        raise ValueError("strong_fit_threshold must be between 0 and 100")
    for field in (
        "profile_rubric_hash",
        "prompt_version",
        "fast_model",
        "strong_model",
    ):
        _bounded_text(config.get(field), field, 256)
    for field in (
        "per_call_timeout_seconds",
        "total_deadline_seconds",
    ):
        value = config.get(field)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value <= 0
        ):
            raise ValueError(f"{field} must be a positive number")
    for field in (
        "max_fast_calls",
        "cache_retention_days",
        "rejected_retention_days",
    ):
        value = config.get(field)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ):
            raise ValueError(f"{field} must be a positive integer")
    if config["max_evidence_items"] > PUBLIC_MAX_EVIDENCE_ITEMS:
        raise ValueError(
            f"max_evidence_items must not exceed {PUBLIC_MAX_EVIDENCE_ITEMS}"
        )
    if config["max_evidence_chars"] > PUBLIC_MAX_EVIDENCE_CHARS:
        raise ValueError(
            f"max_evidence_chars must not exceed {PUBLIC_MAX_EVIDENCE_CHARS}"
        )


def _minimal_request(
    job: Mapping[str, Any],
    *,
    max_items: int,
    max_chars: int,
    max_bytes: int,
) -> dict[str, Any]:
    evidence = job.get("jd_evidence", [])
    if not isinstance(evidence, list):
        raise ValueError("jd_evidence must be a list")
    compact_evidence = [
        item.strip()[:max_chars]
        for item in evidence[:max_items]
        if isinstance(item, str) and item.strip()
    ]
    request = {
        "contract_version": 1,
        "stable_id": _bounded_text(job.get("stable_id"), "stable_id", 256),
        "title": _bounded_text(job.get("title"), "title", 240),
        "company": _bounded_text(job.get("company"), "company", 240),
        "country": _bounded_text(job.get("country"), "country", 80),
        "local_fit": job.get("local_fit"),
        "jd_evidence": compact_evidence,
    }
    fit = request["local_fit"]
    if isinstance(fit, bool) or not isinstance(fit, int) or not 0 <= fit <= 100:
        raise ValueError("local_fit must be an integer between 0 and 100")

    def size() -> int:
        return len(json.dumps(request, ensure_ascii=False).encode("utf-8"))

    while request["jd_evidence"] and size() > max_bytes:
        longest = max(range(len(request["jd_evidence"])), key=lambda i: len(request["jd_evidence"][i]))
        value = request["jd_evidence"][longest]
        if len(value) <= 16:
            request["jd_evidence"].pop(longest)
        else:
            request["jd_evidence"][longest] = value[: max(16, len(value) // 2)]
    if size() > max_bytes:
        raise ValueError("review request exceeds max_request_bytes")
    return request


def _validate_response(response: Any, stable_id: str) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise ValueError("review response must be an object")
    unsupported = set(response) - _RESPONSE_FIELDS
    missing = _RESPONSE_FIELDS - set(response)
    if unsupported:
        raise ValueError(f"review response contains unsupported fields: {sorted(unsupported)}")
    if missing:
        raise ValueError(f"review response is missing fields: {sorted(missing)}")
    if response["contract_version"] != 1:
        raise ValueError("review response has unsupported contract version")
    if response["stable_id"] != stable_id:
        raise ValueError("review response stable_id does not match request")
    if response["decision"] not in _DECISIONS:
        raise ValueError("review response decision is unsupported")
    score = response["score"]
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
        raise ValueError("review response score must be an integer between 0 and 100")
    reasons = response["reason_codes"]
    if (
        not isinstance(reasons, list)
        or not reasons
        or len(reasons) > 12
        or not all(
            isinstance(reason, str) and 0 < len(reason.strip()) <= 80
            for reason in reasons
        )
    ):
        raise ValueError("review response reason_codes must be a bounded non-empty list")
    summary = response["summary"]
    if not isinstance(summary, str) or len(summary) > 500:
        raise ValueError("review response summary must be a string up to 500 characters")
    return deepcopy(response)


def _fact_overrides(
    items: Sequence[dict[str, Any]],
    *,
    observed_on: str,
    maximum_ttl_days: int,
) -> dict[str, bool]:
    today = date.fromisoformat(observed_on)
    facts: dict[str, bool] = {}
    for item in items:
        source = item.get("evidence_source")
        observed = item.get("observed_on")
        ttl = item.get("ttl_days")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("company fact evidence_source is required")
        if not isinstance(observed, str) or not observed:
            raise ValueError("company fact observed_on is required")
        try:
            observed_date = date.fromisoformat(observed)
        except ValueError as error:
            raise ValueError("company fact observed_on must be an ISO date") from error
        if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0:
            raise ValueError("company fact ttl_days must be positive")
        if (
            item.get("fact") == "visa_support"
            and isinstance(item.get("company"), str)
            and isinstance(item.get("value"), bool)
            and 0 <= (today - observed_date).days <= min(ttl, maximum_ttl_days)
        ):
            facts[item["company"].casefold()] = item["value"]
    return facts


def _reject_record(
    job: Mapping[str, Any],
    *,
    reason_codes: Sequence[str],
    observed_on: str,
    hard_excluded: bool,
    rescued: bool = False,
) -> dict[str, Any]:
    return {
        "stable_id": job["stable_id"],
        "reason_codes": sorted(set(reason_codes)),
        "local_fit": job["local_fit"],
        "country": job["country"],
        "hard_excluded": hard_excluded,
        "observed_on": observed_on,
        "rescued": rescued,
    }


def _rejected_audit_key(item: Mapping[str, Any]) -> str:
    return _canonical_digest(
        {
            "stable_id": item.get("stable_id"),
            "reason_codes": item.get("reason_codes"),
            "observed_on": item.get("observed_on"),
            "hard_excluded": item.get("hard_excluded"),
        }
    )


def _ordered_rejected(
    items: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    return sorted(
        items,
        key=lambda item: (item.get("observed_on", ""), item.get("stable_id", ""), item.get("reason_codes", [])),
    )


_REJECTED_DOCUMENT_PREFIX = b'{\n  "contract_version": 1,\n  "items": [\n'
_REJECTED_DOCUMENT_SUFFIX = b"\n  ]\n}\n"


def _rejected_item_serialized_size(item: Mapping[str, Any]) -> int:
    lines = json.dumps(
        item,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).splitlines()
    return sum(4 + len(line.encode("utf-8")) for line in lines) + max(
        0, len(lines) - 1
    )


def _bounded_rejected_items(
    items: Sequence[dict[str, Any]],
    *,
    maximum_items: int = DEFAULT_MAX_REJECTED_ITEMS,
    maximum_bytes: int = DEFAULT_MAX_REJECTED_BYTES,
) -> list[dict[str, Any]]:
    """Keep the newest deterministic prefix within item and byte caps."""

    newest_first = sorted(
        items,
        key=lambda item: (
            str(item.get("observed_on", "")),
            str(item.get("stable_id", "")),
            tuple(str(value) for value in item.get("reason_codes", [])),
            bool(item.get("hard_excluded")),
        ),
        reverse=True,
    )[:maximum_items]
    fixed_size = len(_REJECTED_DOCUMENT_PREFIX) + len(_REJECTED_DOCUMENT_SUFFIX)
    selected: list[dict[str, Any]] = []
    serialized_size = fixed_size
    for item in newest_first:
        item_size = _rejected_item_serialized_size(item)
        separator_size = 2 if selected else 0
        if serialized_size + separator_size + item_size > maximum_bytes:
            continue
        selected.append(item)
        serialized_size += separator_size + item_size
    return _ordered_rejected(selected)


def _retained_dated_items(
    items: Sequence[dict[str, Any]],
    *,
    date_field: str,
    observed_on: str,
    retention_days: int | None,
) -> list[dict[str, Any]]:
    if retention_days is None:
        return deepcopy(list(items))
    cutoff = date.fromisoformat(observed_on) - timedelta(days=retention_days)
    retained: list[dict[str, Any]] = []
    for item in items:
        raw_date = item.get(date_field)
        if not isinstance(raw_date, str):
            continue
        try:
            item_date = date.fromisoformat(raw_date)
        except ValueError:
            continue
        if cutoff <= item_date <= date.fromisoformat(observed_on):
            retained.append(deepcopy(item))
    return retained


def current_rejected_for_jobs(
    rejected: Sequence[Mapping[str, Any]],
    jobs: Iterable[Mapping[str, Any]],
    *,
    observed_on: str,
) -> list[dict[str, Any]]:
    """Return today's rejected records with one indexed pass over current jobs."""

    date.fromisoformat(observed_on)
    stable_ids = {
        item.get("stable_id")
        for item in jobs
        if isinstance(item.get("stable_id"), str)
    }
    return [
        deepcopy(dict(item))
        for item in rejected
        if item.get("observed_on") == observed_on
        and item.get("stable_id") in stable_ids
    ]


def deterministic_rejected_sample(
    rejected: Sequence[Mapping[str, Any]],
    *,
    sample_date: str,
    size: int,
) -> list[dict[str, Any]]:
    """Return a reproducible round-robin sample across primary reject reasons."""

    date.fromisoformat(sample_date)
    if size <= 0:
        return []
    groups: dict[str, list[dict[str, Any]]] = {}
    for raw in rejected:
        item = deepcopy(dict(raw))
        reasons = item.get("reason_codes")
        if not isinstance(reasons, list) or not reasons:
            raise ValueError("rejected item reason_codes must not be empty")
        groups.setdefault(str(reasons[0]), []).append(item)
    for reason, items in groups.items():
        items.sort(
            key=lambda item: _canonical_digest(
                {
                    "sample_date": sample_date,
                    "reason": reason,
                    "stable_id": item.get("stable_id"),
                }
            )
        )
    reasons = sorted(
        groups,
        key=lambda reason: _canonical_digest(
            {"sample_date": sample_date, "reason": reason}
        ),
    )
    result: list[dict[str, Any]] = []
    while len(result) < min(size, len(rejected)):
        progressed = False
        for reason in reasons:
            if groups[reason]:
                result.append(groups[reason].pop(0))
                progressed = True
                if len(result) == min(size, len(rejected)):
                    break
        if not progressed:
            break
    return result


def run_review_pipeline(
    *,
    jobs: Sequence[Mapping[str, Any]],
    repository: ReviewRepository,
    fast_provider: ReviewProvider | None,
    config: Mapping[str, Any],
    observed_on: str,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Review eligible jobs while preserving hard rules and local audit history."""

    _validate_config(config)
    date.fromisoformat(observed_on)
    cache_items_unpruned = _state_items(repository, "review_cache")
    rejected_unpruned = _state_items(repository, "rejected")
    cache_retention = config.get(
        "cache_retention_days", DEFAULT_CACHE_RETENTION_DAYS
    )
    rejected_retention = config.get(
        "rejected_retention_days", DEFAULT_REJECTED_RETENTION_DAYS
    )
    cache_items = _retained_dated_items(
        cache_items_unpruned,
        date_field="cached_on",
        observed_on=observed_on,
        retention_days=cache_retention if isinstance(cache_retention, int) else None,
    )
    rejected_existing = _retained_dated_items(
        rejected_unpruned,
        date_field="observed_on",
        observed_on=observed_on,
        retention_days=(
            rejected_retention if isinstance(rejected_retention, int) else None
        ),
    )
    rejected_existing = _bounded_rejected_items(rejected_existing)
    company_fact_items = _state_items(repository, "company_facts")
    fact_values = _fact_overrides(
        company_fact_items,
        observed_on=observed_on,
        maximum_ttl_days=config["company_fact_ttl_days"],
    )
    cache = {item.get("key"): item for item in cache_items if isinstance(item.get("key"), str)}
    cache_changed = cache_items != cache_items_unpruned
    cache_dirty_count = 0
    cache_checkpointed_this_run = False
    provider_calls = 0
    cache_hits = 0
    escalations = 0
    company_facts_used = 0
    reviews: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    rejected_by_key = {
        _rejected_audit_key(item): item for item in rejected_existing
    }
    rejected_state = _ordered_rejected(rejected_by_key.values())
    rejected_dirty_count = 0
    rejected_checkpointed_this_run = False
    safe_limits_enabled = any(
        field in config
        for field in (
            "per_call_timeout_seconds",
            "total_deadline_seconds",
            "max_fast_calls",
        )
    )
    per_call_timeout = float(
        config.get("per_call_timeout_seconds", DEFAULT_PER_CALL_TIMEOUT_SECONDS)
    )
    total_deadline_seconds = float(
        config.get("total_deadline_seconds", DEFAULT_TOTAL_DEADLINE_SECONDS)
    )
    max_fast_calls = int(config.get("max_fast_calls", DEFAULT_MAX_FAST_CALLS))
    deadline = monotonic() + total_deadline_seconds
    fast_calls = 0
    fast_call_budget_exhausted = False
    deadline_exhausted = False

    def write_cache() -> None:
        nonlocal cache_changed, cache_dirty_count, cache_checkpointed_this_run
        repository.write(
            "review_cache",
            {
                "contract_version": 1,
                "items": sorted(cache.values(), key=lambda item: item["key"]),
            },
        )
        cache_changed = False
        cache_dirty_count = 0
        cache_checkpointed_this_run = True

    def checkpoint_rejected(record: dict[str, Any]) -> None:
        nonlocal rejected_state, rejected_dirty_count
        key = _rejected_audit_key(record)
        previous = rejected_by_key.get(key)
        if previous is not None and record.get("rescued"):
            previous["rescued"] = True
        elif previous is None:
            rejected_by_key[key] = deepcopy(record)
        rejected_dirty_count += 1
        if (
            not rejected_checkpointed_this_run
            or rejected_dirty_count >= CHECKPOINT_BATCH_SIZE
        ):
            write_rejected()

    def write_rejected() -> None:
        nonlocal rejected_dirty_count, rejected_state, rejected_by_key
        nonlocal rejected_checkpointed_this_run
        rejected_state = _bounded_rejected_items(list(rejected_by_key.values()))
        rejected_by_key = {
            _rejected_audit_key(item): item for item in rejected_state
        }
        repository.write(
            "rejected",
            {"contract_version": 1, "items": rejected_state},
        )
        rejected_dirty_count = 0
        rejected_checkpointed_this_run = True

    if cache_changed:
        write_cache()
        cache_checkpointed_this_run = False
    if rejected_existing != rejected_unpruned:
        repository.write(
            "rejected",
            {"contract_version": 1, "items": rejected_existing},
        )

    ordered_jobs = sorted(jobs, key=lambda item: (-int(item.get("local_fit", -1)), str(item.get("stable_id", ""))))

    def call(
        provider: ReviewProvider,
        mode: str,
        job: Mapping[str, Any],
        request: dict[str, Any],
    ) -> dict[str, Any] | None:
        nonlocal provider_calls, cache_hits, cache_changed, cache_dirty_count
        nonlocal fast_calls, fast_call_budget_exhausted, deadline_exhausted
        model = config["fast_model"] if mode == "fast" else config["strong_model"]
        key = review_cache_key(
            stable_id=job["stable_id"],
            jd_hash=_bounded_text(job.get("jd_hash"), "jd_hash", 256),
            profile_rubric_hash=config["profile_rubric_hash"],
            prompt_version=config["prompt_version"],
            model=model,
            mode=mode,
        )
        cached = cache.get(key)
        if cached is not None:
            cache_hits += 1
            return _validate_response(cached.get("response"), job["stable_id"])
        if safe_limits_enabled and mode == "fast" and fast_calls >= max_fast_calls:
            fast_call_budget_exhausted = True
            failures.append(
                {
                    "stable_id": job["stable_id"],
                    "mode": mode,
                    "category": "fast_call_budget",
                    "message": "review fast-call budget exhausted",
                }
            )
            return None
        remaining = deadline - monotonic()
        if safe_limits_enabled and remaining <= 0:
            deadline_exhausted = True
            failures.append(
                {
                    "stable_id": job["stable_id"],
                    "mode": mode,
                    "category": "total_deadline",
                    "message": "review total deadline exhausted",
                }
            )
            return None
        timeout_seconds = min(per_call_timeout, remaining)
        call_started = monotonic()
        try:
            timed_review = getattr(provider, "review_with_timeout", None)
            if safe_limits_enabled:
                if not callable(timed_review):
                    failures.append(
                        {
                            "stable_id": job["stable_id"],
                            "mode": mode,
                            "category": "provider_contract",
                            "message": "review provider lacks timeout contract",
                        }
                    )
                    return None
                provider_calls += 1
                if mode == "fast":
                    fast_calls += 1
                raw = timed_review(
                    mode,
                    deepcopy(request),
                    timeout_seconds=timeout_seconds,
                )
            else:
                provider_calls += 1
                if mode == "fast":
                    fast_calls += 1
                raw = provider.review(mode, deepcopy(request))
        except TimeoutError:
            exhausted_total = remaining <= per_call_timeout
            failures.append(
                {
                    "stable_id": job["stable_id"],
                    "mode": mode,
                    "category": (
                        "total_deadline" if exhausted_total else "provider_timeout"
                    ),
                    "message": (
                        "review total deadline exhausted"
                        if exhausted_total
                        else "review provider timed out"
                    ),
                }
            )
            if exhausted_total:
                deadline_exhausted = True
            return None
        except Exception:
            failures.append(
                {
                    "stable_id": job["stable_id"],
                    "mode": mode,
                    "category": "provider_error",
                    "message": "review provider failed",
                }
            )
            return None
        if safe_limits_enabled and monotonic() - call_started > timeout_seconds:
            exhausted_total = monotonic() >= deadline
            failures.append(
                {
                    "stable_id": job["stable_id"],
                    "mode": mode,
                    "category": (
                        "total_deadline" if exhausted_total else "provider_timeout"
                    ),
                    "message": (
                        "review total deadline exhausted"
                        if exhausted_total
                        else "review provider timed out"
                    ),
                }
            )
            deadline_exhausted = exhausted_total
            return None
        response = _validate_response(raw, job["stable_id"])
        entry = {
            "key": key,
            "stable_id": job["stable_id"],
            "mode": mode,
            "model": model,
            "response": response,
        }
        cache[key] = entry
        cache_changed = True
        cache_dirty_count += 1
        entry["cached_on"] = observed_on
        if (
            not cache_checkpointed_this_run
            or cache_dirty_count >= CHECKPOINT_BATCH_SIZE
        ):
            write_cache()
        return response

    for job in ordered_jobs:
        if job.get("hard_excluded") is True:
            reasons = job.get("hard_reason_codes")
            if not isinstance(reasons, list) or not reasons:
                raise ValueError("hard excluded jobs require hard_reason_codes")
            checkpoint_rejected(
                _reject_record(
                    job,
                    reason_codes=reasons,
                    observed_on=observed_on,
                    hard_excluded=True,
                )
            )
            continue
        if job["local_fit"] < config["minimum_fit"]:
            checkpoint_rejected(
                _reject_record(
                    job,
                    reason_codes=["below_minimum_fit"],
                    observed_on=observed_on,
                    hard_excluded=False,
                )
            )
            continue
        if fast_provider is None:
            continue
        request = _minimal_request(
            job,
            max_items=config["max_evidence_items"],
            max_chars=config["max_evidence_chars"],
            max_bytes=config["max_request_bytes"],
        )
        fast_response = call(fast_provider, "fast", job, request)
        if fast_response is None:
            if deadline_exhausted:
                break
            continue
        if job.get("visa_supported") is None and fact_values.get(str(job["company"]).casefold()) is not None:
            company_facts_used += 1
        reviews.append(fast_response)
        if fast_response["decision"] == "reject":
            checkpoint_rejected(
                _reject_record(
                    job,
                    reason_codes=fast_response["reason_codes"],
                    observed_on=observed_on,
                    hard_excluded=False,
                )
            )

    if cache_changed:
        write_cache()
    if rejected_dirty_count:
        write_rejected()
    if not rejected_state and rejected_state == rejected_existing:
        repository.write("rejected", {"contract_version": 1, "items": []})
    current_rejected = current_rejected_for_jobs(
        rejected_state,
        jobs,
        observed_on=observed_on,
    )
    reviews.sort(key=lambda item: item["stable_id"])
    failures.sort(key=lambda item: (item["stable_id"], item["mode"]))
    report = {
        "provider_calls": provider_calls,
        "cache_hits": cache_hits,
        "escalations": escalations,
    }
    if safe_limits_enabled:
        report.update(
            {
                "fast_call_budget_exhausted": fast_call_budget_exhausted,
                "deadline_exhausted": deadline_exhausted,
            }
        )
    return {
        "contract_version": 1,
        "reviews": reviews,
        "rejected": current_rejected,
        "rejected_sample": deterministic_rejected_sample(
            current_rejected,
            sample_date=observed_on,
            size=min(10, len(current_rejected)),
        ),
        "failures": failures,
        "company_facts_used": company_facts_used,
        "report": report,
    }
