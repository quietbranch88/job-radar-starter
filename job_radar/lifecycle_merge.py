from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
from typing import Any, Mapping

from job_radar.domain.tracking import (
    freshness_state,
    parse_iso_date_or_datetime,
    stable_job_id,
)
from job_radar.domain.jobs import validate_jd_evidence


_SCAN_FIELDS = {
    "source",
    "external_id",
    "company",
    "title",
    "location",
    "url",
    "published_at",
    "score",
    "country",
    "category",
    "summary",
    "risk",
    "salary",
    "tracks",
    "skills",
    "first_seen",
    "visa_supported",
}
_CURATED_FIELDS = {
    "score",
    "summary",
    "risk",
    "tracks",
    "skills",
    "visa_supported",
}
_LIFECYCLE_FIELDS = {
    "stable_id",
    "first_seen",
    "last_seen",
    "freshness",
    "legacy_status_keys",
}
_JD_FIELDS = {"jd_evidence", "jd_evidence_source", "jd_hash"}


def _iso_date(value: object, field: str) -> date:
    parsed = parse_iso_date_or_datetime(value)
    if isinstance(parsed, datetime):
        raise ValueError(f"{field} must be an ISO date")
    return parsed


def _mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return deepcopy(dict(value))


def observation_as_of(observed_on: str, jobs: list[Mapping[str, Any]]) -> str:
    """Keep the walk date separate from actual per-job UTC fetch dates."""
    observed = _iso_date(observed_on, "observed_on")
    today = datetime.now(timezone.utc).date()
    for job in jobs:
        if job.get("last_seen"):
            seen = _iso_date(job["last_seen"], "last_seen")
            if seen > today:
                raise ValueError("job observation date cannot be in the future")
            observed = max(observed, seen)
    return observed.isoformat()


def _validate_aliases(aliases: object) -> tuple[dict[str, str], dict[str, str]]:
    payload = _mapping(aliases, "aliases")
    if payload.get("contract_version") != 1:
        raise ValueError("aliases has unsupported contract version")
    if set(payload) != {"contract_version", "company_aliases", "title_aliases"}:
        raise ValueError("aliases has unsupported fields")
    company_aliases = payload["company_aliases"]
    title_aliases = payload["title_aliases"]
    for field, values in (
        ("company_aliases", company_aliases),
        ("title_aliases", title_aliases),
    ):
        if not isinstance(values, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in values.items()
        ):
            raise ValueError(f"{field} must map strings to strings")
    return dict(company_aliases), dict(title_aliases)


def _validate_scan(scan_result: object) -> dict[str, Any]:
    scan = _mapping(scan_result, "scan_result")
    if scan.get("contract_version") != 1:
        raise ValueError("scan_result has unsupported contract version")
    if set(scan) - {"observed_on"} != {
        "contract_version",
        "mode",
        "incomplete",
        "jobs",
        "failures",
    }:
        raise ValueError("scan_result has unsupported fields")
    if scan["mode"] not in {"atomic", "best-effort"}:
        raise ValueError("scan_result has unsupported mode")
    if not isinstance(scan["incomplete"], bool):
        raise ValueError("scan_result incomplete must be boolean")
    if not isinstance(scan["jobs"], list) or not all(
        isinstance(job, dict) for job in scan["jobs"]
    ):
        raise ValueError("scan_result jobs must be objects")
    if not isinstance(scan["failures"], list) or not all(
        isinstance(failure, dict) for failure in scan["failures"]
    ):
        raise ValueError("scan_result failures must be objects")
    return scan


def _validate_prior(prior_state: object) -> dict[str, Any]:
    prior = _mapping(prior_state, "prior_state")
    if prior.get("contract_version") != 1:
        raise ValueError("prior_state has unsupported contract version")
    if set(prior) != {"contract_version", "jobs", "statuses"}:
        raise ValueError("prior_state has unsupported fields")
    if not isinstance(prior["jobs"], list) or not all(
        isinstance(job, dict) for job in prior["jobs"]
    ):
        raise ValueError("prior_state jobs must be objects")
    if not isinstance(prior["statuses"], dict):
        raise ValueError("prior_state statuses must be an object")
    return prior


def _validate_job(job: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(job) - (_SCAN_FIELDS | _LIFECYCLE_FIELDS | _JD_FIELDS)
    if unknown:
        raise ValueError(f"job has unsupported fields: {sorted(unknown)}")
    missing = _SCAN_FIELDS - set(job)
    if missing:
        raise ValueError(f"job has missing fields: {sorted(missing)}")
    normalized = deepcopy(dict(job))
    if "last_seen" in normalized and not isinstance(normalized["last_seen"], str):
        raise ValueError("job last_seen must be a string")
    validate_jd_evidence(normalized)
    if "jd_evidence" in normalized:
        normalized["jd_evidence"] = list(normalized["jd_evidence"])
    for field in (
        "source",
        "external_id",
        "company",
        "title",
        "location",
        "url",
        "published_at",
        "country",
        "category",
        "summary",
        "risk",
        "salary",
        "first_seen",
    ):
        if not isinstance(normalized[field], str):
            raise ValueError(f"job {field} must be a string")
    if not normalized["source"] or not normalized["company"] or not normalized["title"]:
        raise ValueError("job source, company, and title must not be empty")
    for field in ("tracks", "skills"):
        if not isinstance(normalized[field], list) or not all(
            isinstance(item, str) for item in normalized[field]
        ):
            raise ValueError(f"job {field} must be a list of strings")
    score = normalized["score"]
    if score is not None and (
        isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100
    ):
        raise ValueError("job score must be null or an integer from 0 to 100")
    if normalized["visa_supported"] is not None and not isinstance(
        normalized["visa_supported"], bool
    ):
        raise ValueError("job visa_supported must be boolean or null")
    return normalized


def _stable_id(
    job: Mapping[str, Any],
    company_aliases: Mapping[str, str],
    title_aliases: Mapping[str, str],
) -> str:
    aliases = {**company_aliases, **title_aliases}
    return stable_job_id(
        source=str(job["source"]),
        external_id=str(job["external_id"]),
        company=str(job["company"]),
        title=str(job["title"]),
        aliases=aliases,
    )


def _legacy_keys(job: Mapping[str, Any]) -> list[str]:
    keys: list[str] = []
    source = str(job.get("source", ""))
    external_id = str(job.get("external_id", ""))
    url = str(job.get("url", ""))
    if source and external_id:
        keys.append(f"{source.casefold()}:{external_id}")
    if url:
        keys.append(url)
    return sorted(set(keys))


def _provider_order(job: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(job["source"]).casefold(),
        str(job["external_id"]).casefold(),
        str(job["company"]).casefold(),
        str(job["title"]).casefold(),
        str(job["url"]).casefold(),
    )


def _failure_scope(scan: Mapping[str, Any]) -> set[tuple[str, str]]:
    failed: set[tuple[str, str]] = set()
    for failure in scan["failures"]:
        source = failure.get("source")
        company = failure.get("company")
        if isinstance(source, str) and isinstance(company, str):
            failed.add((source.casefold(), company.casefold()))
    return failed


def _freshness(
    job: Mapping[str, Any],
    observed: date,
    *,
    stale_after_days: int,
    expired_after_days: int,
) -> str:
    return freshness_state(
        last_seen=str(job["last_seen"]),
        as_of=observed,
        stale_after_days=stale_after_days,
        expired_after_days=expired_after_days,
    )


def _migrate_statuses(
    statuses: Mapping[str, str],
    jobs: list[dict[str, Any]],
) -> dict[str, str]:
    allowed = {"interested", "applied", "skip", "dead"}
    for key, value in statuses.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("statuses must map strings to strings")
        if value.casefold() not in allowed:
            raise ValueError(f"unsupported status: {value}")
    migrated: dict[str, str] = {}
    for job in jobs:
        stable_id = job["stable_id"]
        if stable_id in statuses:
            migrated[stable_id] = statuses[stable_id].casefold()
            continue
        matches = sorted(
            key for key in job["legacy_status_keys"] if key in statuses
        )
        if matches:
            migrated[stable_id] = statuses[matches[0]].casefold()
    return dict(sorted(migrated.items()))


def merge_scan_state(
    *,
    prior_state: Mapping[str, Any],
    scan_result: Mapping[str, Any],
    observed_on: str,
    aliases: Mapping[str, Any],
    statuses: Mapping[str, str],
    stale_after_days: int = 14,
    expired_after_days: int = 30,
    untracked_expired_retention_days: int = 90,
) -> dict[str, Any]:
    prior = _validate_prior(prior_state)
    scan = _validate_scan(scan_result)
    company_aliases, title_aliases = _validate_aliases(aliases)
    observed = _iso_date(observed_on, "observed_on")
    if (
        isinstance(untracked_expired_retention_days, bool)
        or not isinstance(untracked_expired_retention_days, int)
        or untracked_expired_retention_days < 0
    ):
        raise ValueError("untracked_expired_retention_days must be a non-negative integer")

    existing: dict[str, dict[str, Any]] = {}
    for raw_job in prior["jobs"]:
        job = _validate_job(raw_job)
        stable_id = job.get("stable_id")
        if not isinstance(stable_id, str) or not stable_id:
            stable_id = _stable_id(job, company_aliases, title_aliases)
        if stable_id in existing:
            raise ValueError("prior_state contains duplicate stable_id")
        first_seen = _iso_date(job.get("first_seen"), "first_seen")
        last_seen = _iso_date(job.get("last_seen"), "last_seen")
        if first_seen > last_seen or last_seen > observed:
            raise ValueError("lifecycle dates cannot be in the future")
        job["stable_id"] = stable_id
        keys = job.get("legacy_status_keys", [])
        if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
            raise ValueError("legacy_status_keys must be a list of strings")
        job["legacy_status_keys"] = sorted(set(keys) | set(_legacy_keys(job)))
        existing[stable_id] = job

    incoming_by_id: dict[str, dict[str, Any]] = {}
    for raw_job in sorted(scan["jobs"], key=_provider_order):
        incoming = _validate_job(raw_job)
        seen = _iso_date(incoming.get("last_seen") or observed_on, "last_seen")
        if seen > observed:
            raise ValueError("job observation date cannot be in the future")
        published = incoming["published_at"]
        if published:
            parsed_published = parse_iso_date_or_datetime(published)
            published_date = (
                parsed_published.date()
                if isinstance(parsed_published, datetime)
                else parsed_published
            )
            if published_date > seen:
                raise ValueError("published date cannot be in the future")
        stable_id = _stable_id(incoming, company_aliases, title_aliases)
        incoming["last_seen"] = seen.isoformat()
        candidate = incoming_by_id.get(stable_id)
        if candidate is None or candidate["last_seen"] < incoming["last_seen"]:
            incoming_by_id[stable_id] = incoming

    failed_scope = _failure_scope(scan)
    merged: dict[str, dict[str, Any]] = {}
    for stable_id, incoming in incoming_by_id.items():
        previous = existing.get(stable_id)
        if previous is not None and previous["last_seen"] > incoming["last_seen"]:
            continue
        job = deepcopy(incoming)
        job["stable_id"] = stable_id
        if previous is None:
            job["first_seen"] = incoming["last_seen"]
            job["legacy_status_keys"] = _legacy_keys(job)
        else:
            job["first_seen"] = previous["first_seen"]
            for field in _CURATED_FIELDS:
                job[field] = deepcopy(previous[field])
            job["legacy_status_keys"] = sorted(
                set(previous["legacy_status_keys"]) | set(_legacy_keys(job))
            )
        job["last_seen"] = incoming["last_seen"]
        job["freshness"] = _freshness(
            job, observed, stale_after_days=stale_after_days,
            expired_after_days=expired_after_days,
        )
        merged[stable_id] = job

    for stable_id, previous in existing.items():
        if stable_id in merged:
            continue
        job = deepcopy(previous)
        source_key = str(job["source"]).casefold()
        company_key = str(job["company"]).casefold()
        source_failed = any(
            failed_source == source_key
            for failed_source, _failed_company in failed_scope
        )
        company_failed = (source_key, company_key) in failed_scope
        if scan["incomplete"] and (source_failed or company_failed):
            job["freshness"] = "active"
        else:
            job["freshness"] = _freshness(
                job,
                observed,
                stale_after_days=stale_after_days,
                expired_after_days=expired_after_days,
            )
        merged[stable_id] = job

    jobs = [merged[key] for key in sorted(merged)]
    source_statuses = dict(prior["statuses"])
    source_statuses.update(deepcopy(dict(statuses)))
    migrated_statuses = _migrate_statuses(source_statuses, jobs)
    maximum_untracked_age = expired_after_days + untracked_expired_retention_days
    jobs = [
        job
        for job in jobs
        if job["stable_id"] in migrated_statuses
        or job["freshness"] != "expired"
        or (observed - _iso_date(job["last_seen"], "last_seen")).days
        <= maximum_untracked_age
    ]
    return {
        "contract_version": 1,
        "jobs": jobs,
        "statuses": _migrate_statuses(migrated_statuses, jobs),
    }
