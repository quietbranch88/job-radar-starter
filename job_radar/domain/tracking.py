from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence


_ID_MAX_LENGTH = 128
_COMPANY_MAX_LENGTH = 200
_TITLE_MAX_LENGTH = 300
_NOTES_MAX_LENGTH = 10_000
_CONTRACT_VERSION = 1

_APPLICATION_STATUSES = {
    "interested",
    "applied",
    "interview",
    "offer",
    "accepted",
    "rejected",
    "withdrawn",
}
_LEGACY_STATUSES = _APPLICATION_STATUSES | {"skip", "dead"}
_DASHBOARD_MARKERS = {"interested", "applied", "skip", "dead"}


def dashboard_marker(status: str) -> str:
    """Fold application states onto the four browser markers."""
    normalized = status.casefold()
    if normalized == "hidden":
        return "skip"
    if normalized in _DASHBOARD_MARKERS:
        return normalized
    if normalized in {"interview", "offer", "accepted"}:
        return "applied"
    if normalized in {"rejected", "withdrawn"}:
        return "skip"
    raise ValueError(f"unsupported status: {status}")


_ALLOWED_TRANSITIONS = {
    "interested": {"interested", "applied", "rejected", "withdrawn"},
    "applied": {"applied", "interview", "rejected", "withdrawn"},
    "interview": {"interview", "offer", "rejected", "withdrawn"},
    "offer": {"offer", "accepted", "rejected", "withdrawn"},
    "accepted": {"accepted"},
    "rejected": {"rejected"},
    "withdrawn": {"withdrawn"},
}


def _display_text(value: str) -> str:
    return " ".join(value.strip().split())


def _normalized_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.split())


def _normalized_company(value: str) -> str:
    words = _normalized_text(value).split()
    while words and words[-1] in {
        "co",
        "company",
        "corp",
        "corporation",
        "inc",
        "incorporated",
        "ltd",
        "limited",
        "llc",
    }:
        words.pop()
    return " ".join(words)


def _digest(prefix: str, *parts: str) -> str:
    canonical = "\x1f".join(parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(canonical).hexdigest()[:32]}"


def canonicalize_alias(value: str, aliases: Mapping[str, str] | None = None) -> str:
    """Resolve user-owned aliases using normalized, case-insensitive keys."""

    display = _display_text(value)
    if not aliases:
        return display

    normalized_aliases: dict[str, str] = {}
    for alias, target in aliases.items():
        if not isinstance(alias, str) or not isinstance(target, str):
            raise TypeError("alias keys and values must be strings")
        normalized_aliases[_normalized_text(alias)] = _display_text(target)

    visited: set[str] = set()
    current = display
    while (key := _normalized_text(current)) in normalized_aliases:
        if key in visited:
            raise ValueError("alias cycle detected")
        visited.add(key)
        current = normalized_aliases[key]
    return current


def stable_job_id(
    *,
    source: str,
    external_id: str | None,
    company: str,
    title: str,
    aliases: Mapping[str, str] | None = None,
) -> str:
    """Return an identity stable across harmless display changes."""

    normalized_source = _normalized_text(source)
    if not normalized_source:
        raise ValueError("source must not be empty")

    normalized_external_id = _normalized_text(external_id or "")
    if normalized_external_id:
        return _digest("job", normalized_source, normalized_external_id)

    canonical_company = canonicalize_alias(company, aliases)
    canonical_title = canonicalize_alias(title, aliases)
    company_key = _normalized_company(canonical_company)
    title_key = _normalized_text(canonical_title)
    if not company_key:
        raise ValueError("company must not be empty")
    if not title_key:
        raise ValueError("title must not be empty")
    return _digest("job", normalized_source, company_key, title_key)


def stable_application_id(job_id: str) -> str:
    normalized_job_id = _normalized_text(job_id)
    if not normalized_job_id:
        raise ValueError("job_id must not be empty")
    return _digest("application", normalized_job_id)


def transition_application(current: str, target: str) -> str:
    current_status = _normalized_text(current)
    target_status = _normalized_text(target)
    if current_status not in _APPLICATION_STATUSES:
        raise ValueError(f"unsupported application status: {current}")
    if target_status not in _APPLICATION_STATUSES:
        raise ValueError(f"unsupported application status: {target}")
    if target_status not in _ALLOWED_TRANSITIONS[current_status]:
        raise ValueError(
            f"invalid application transition: {current_status} -> {target_status}"
        )
    return target_status


def stable_event_id(
    *,
    application_id: str,
    event_type: str,
    occurred_at: str,
    details: Mapping[str, Any],
) -> str:
    application_key = _normalized_text(application_id)
    event_key = _normalized_text(event_type)
    if not application_key:
        raise ValueError("application_id must not be empty")
    if not event_key:
        raise ValueError("event_type must not be empty")
    occurred = parse_iso_date_or_datetime(occurred_at)
    if not isinstance(occurred, datetime):
        raise ValueError("event occurred_at must be an ISO datetime")
    canonical_details = json.dumps(
        details,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _digest(
        "event",
        application_key,
        event_key,
        occurred.isoformat().replace("+00:00", "Z"),
        canonical_details,
    )


def parse_iso_date_or_datetime(value: object) -> date | datetime:
    """Parse a strict ISO date or aware datetime, normalizing datetimes to UTC."""

    if not isinstance(value, str):
        raise TypeError("date or datetime must be an ISO string")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("invalid ISO date") from error
    if " " in value or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
        value,
    ):
        raise ValueError("invalid ISO datetime")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("invalid ISO datetime") from error
    if parsed.tzinfo is None:
        raise ValueError("ISO datetime must include a timezone")
    return parsed.astimezone(timezone.utc)


def _as_date(value: str | date) -> date:
    if isinstance(value, datetime):
        raise TypeError("expected a date, not a datetime")
    if isinstance(value, date):
        return value
    parsed = parse_iso_date_or_datetime(value)
    if isinstance(parsed, datetime):
        return parsed.date()
    return parsed


@dataclass(frozen=True)
class JobLifecycle:
    job_id: str
    first_seen: date
    last_seen: date

    def __post_init__(self) -> None:
        if not _normalized_text(self.job_id):
            raise ValueError("job_id must not be empty")
        if self.first_seen > self.last_seen:
            raise ValueError("first_seen must not be after last_seen")

    def observe(self, observed_on: date) -> JobLifecycle:
        observed = _as_date(observed_on)
        return JobLifecycle(
            job_id=self.job_id,
            first_seen=min(self.first_seen, observed),
            last_seen=max(self.last_seen, observed),
        )


def freshness_state(
    *,
    last_seen: str | date,
    as_of: str | date,
    stale_after_days: int = 14,
    expired_after_days: int = 30,
) -> str:
    if (
        isinstance(stale_after_days, bool)
        or isinstance(expired_after_days, bool)
        or stale_after_days < 0
        or expired_after_days <= stale_after_days
    ):
        raise ValueError("freshness threshold values are invalid")
    last_seen_date = _as_date(last_seen)
    as_of_date = _as_date(as_of)
    age = (as_of_date - last_seen_date).days
    if age < 0:
        raise ValueError("last_seen cannot be in the future")
    if age >= expired_after_days:
        return "expired"
    if age >= stale_after_days:
        return "stale"
    return "active"


def migrate_legacy_statuses(
    statuses: Mapping[str, str],
    jobs: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    """Map old provider/URL keys to stable IDs, preferring an existing stable key."""

    for key, status in statuses.items():
        if not isinstance(key, str) or not isinstance(status, str):
            raise TypeError("status keys and values must be strings")
        if _normalized_text(status) not in _LEGACY_STATUSES:
            raise ValueError(f"unsupported status value: {status}")

    migrated: dict[str, str] = {}
    for job in sorted(jobs, key=lambda item: str(item.get("stable_id", ""))):
        unknown = set(job) - {"stable_id", "legacy_status_keys"}
        if unknown:
            raise ValueError(f"unsupported lifecycle fields: {sorted(unknown)}")
        stable_id = job.get("stable_id")
        legacy_keys = job.get("legacy_status_keys", ())
        if not isinstance(stable_id, str) or not stable_id:
            raise ValueError("stable_id must be a non-empty string")
        if not isinstance(legacy_keys, (list, tuple)) or not all(
            isinstance(key, str) for key in legacy_keys
        ):
            raise ValueError("legacy_status_keys must be a list of strings")

        if stable_id in statuses:
            migrated[stable_id] = _normalized_text(statuses[stable_id])
            continue
        candidates = sorted(key for key in legacy_keys if key in statuses)
        if candidates:
            migrated[stable_id] = _normalized_text(statuses[candidates[0]])
    return migrated


def _bounded_string(
    payload: Mapping[str, Any],
    field: str,
    maximum: int,
    *,
    allow_empty: bool = False,
) -> str:
    value = payload.get(field)
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if not allow_empty and not value:
        raise ValueError(f"{field} must not be empty")
    if len(value) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return value


@dataclass(frozen=True)
class ApplicationRecord:
    contract_version: int
    application_id: str
    job_id: str
    company: str
    title: str
    status: str
    created_at: str
    updated_at: str
    notes: str = ""

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ApplicationRecord:
        if not isinstance(payload, Mapping):
            raise ValueError("application record must be an object")
        fields = {
            "contract_version",
            "application_id",
            "job_id",
            "company",
            "title",
            "status",
            "created_at",
            "updated_at",
            "notes",
        }
        unknown = set(payload) - fields
        if unknown:
            raise ValueError(f"unsupported application fields: {sorted(unknown)}")
        missing = fields - set(payload)
        if missing:
            raise ValueError(f"missing application fields: {sorted(missing)}")

        version = payload["contract_version"]
        if isinstance(version, bool) or version != _CONTRACT_VERSION:
            raise ValueError(f"unsupported contract version: {version}")
        application_id = _bounded_string(payload, "application_id", _ID_MAX_LENGTH)
        job_id = _bounded_string(payload, "job_id", _ID_MAX_LENGTH)
        company = _bounded_string(payload, "company", _COMPANY_MAX_LENGTH)
        title = _bounded_string(payload, "title", _TITLE_MAX_LENGTH)
        status = _bounded_string(payload, "status", 32)
        if status not in _APPLICATION_STATUSES:
            raise ValueError(f"unsupported application status: {status}")
        created_at = _bounded_string(payload, "created_at", 40)
        updated_at = _bounded_string(payload, "updated_at", 40)
        created = parse_iso_date_or_datetime(created_at)
        updated = parse_iso_date_or_datetime(updated_at)
        if not isinstance(created, datetime) or not isinstance(updated, datetime):
            raise ValueError("created_at and updated_at must be ISO datetimes")
        if updated < created:
            raise ValueError("updated_at must not be before created_at")
        notes = _bounded_string(
            payload,
            "notes",
            _NOTES_MAX_LENGTH,
            allow_empty=True,
        )
        return cls(
            contract_version=version,
            application_id=application_id,
            job_id=job_id,
            company=company,
            title=title,
            status=status,
            created_at=created_at,
            updated_at=updated_at,
            notes=notes,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "application_id": self.application_id,
            "job_id": self.job_id,
            "company": self.company,
            "title": self.title,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "notes": self.notes,
        }
