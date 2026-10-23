from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from job_radar.domain.tracking import (
    parse_iso_date_or_datetime,
    stable_application_id,
    stable_event_id,
    transition_application,
)


class TrackingRepository(Protocol):
    def read(self, kind: str) -> dict[str, Any]: ...

    def write(self, kind: str, document: dict[str, Any]) -> Any: ...

    def commit_application_event(
        self,
        applications: dict[str, Any],
        events: dict[str, Any],
    ) -> Any: ...


def _document(repository: TrackingRepository, kind: str) -> dict[str, Any]:
    document = repository.read(kind)
    if not isinstance(document, dict):
        raise ValueError(f"{kind} state must be an object")
    if document.get("contract_version") != 1:
        raise ValueError(f"{kind} state has unsupported contract version")
    if set(document) != {"contract_version", "items"}:
        raise ValueError(f"{kind} state has unsupported fields")
    items = document["items"]
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise ValueError(f"{kind} state items must be objects")
    return deepcopy(document)


def _aware_datetime(value: object, field: str) -> datetime:
    parsed = parse_iso_date_or_datetime(value)
    if not isinstance(parsed, datetime):
        raise ValueError(f"{field} must be an ISO datetime")
    return parsed


def _optional_string(
    payload: Mapping[str, Any],
    field: str,
    maximum: int,
    *,
    default: str = "",
) -> str:
    value = payload.get(field, default)
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"{field} must be a string up to {maximum} characters")
    return value


def _required_string(
    payload: Mapping[str, Any],
    field: str,
    maximum: int,
) -> str:
    value = _optional_string(payload, field, maximum)
    if not value:
        raise ValueError(f"{field} must not be empty")
    return value


def _normalize_application(payload: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "application_id",
        "job_id",
        "company",
        "title",
        "status",
        "applied_at",
        "updated_at",
        "resume_version",
        "channel",
        "country",
        "rejection_stage",
        "promised_response_date",
        "offer_deadline",
        "confirmation_evidence",
        "confirmed_at",
    }
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError(f"unsupported application fields: {sorted(unknown)}")
    job_id = _required_string(payload, "job_id", 128)
    company = _required_string(payload, "company", 200)
    title = _required_string(payload, "title", 300)
    status = _required_string(payload, "status", 32).casefold()
    applied_at = _required_string(payload, "applied_at", 40)
    updated_at = _required_string(payload, "updated_at", 40)
    applied = None if applied_at == "unknown" else _aware_datetime(applied_at, "applied_at")
    updated = _aware_datetime(updated_at, "updated_at")
    if applied is not None and updated < applied:
        raise ValueError("updated_at must not be before applied_at")
    application_id = payload.get("application_id")
    if application_id is None:
        application_id = stable_application_id(job_id)
    if not isinstance(application_id, str) or not application_id:
        raise ValueError("application_id must be a non-empty string")
    if len(application_id) > 128:
        raise ValueError("application_id exceeds 128 characters")
    transition_application(status, status)
    normalized = {
        "job_id": job_id,
        "company": company,
        "title": title,
        "status": status,
        "applied_at": applied_at,
        "updated_at": updated_at,
        "resume_version": _optional_string(payload, "resume_version", 128),
        "channel": _optional_string(payload, "channel", 128),
        "country": _optional_string(payload, "country", 32),
        "application_id": application_id,
    }
    if "confirmation_evidence" in payload:
        normalized["confirmation_evidence"] = _required_string(payload, "confirmation_evidence", 1000)
    if "confirmed_at" in payload:
        confirmed_at = _required_string(payload, "confirmed_at", 40)
        confirmed = _aware_datetime(confirmed_at, "confirmed_at")
        if confirmed > updated or (applied is not None and confirmed < applied):
            raise ValueError("confirmed_at must be between submission and updated_at")
        normalized["confirmed_at"] = confirmed_at
    for field in ("rejection_stage", "promised_response_date", "offer_deadline"):
        if field not in payload:
            continue
        value = _optional_string(payload, field, 128)
        if field.endswith("_date") or field == "offer_deadline":
            parsed = parse_iso_date_or_datetime(value)
            if isinstance(parsed, datetime):
                raise ValueError(f"{field} must be an ISO date")
        normalized[field] = value
    return normalized


def upsert_application(
    repository: TrackingRepository,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    candidate = _normalize_application(payload)
    document = _document(repository, "applications")
    matches = [
        (index, item)
        for index, item in enumerate(document["items"])
        if item.get("application_id") == candidate["application_id"]
        or item.get("job_id") == candidate["job_id"]
    ]
    if len(matches) > 1:
        raise ValueError("application state contains duplicate identity")
    if matches:
        index, existing = matches[0]
        transition_application(str(existing.get("status", "")), candidate["status"])
        candidate["application_id"] = existing["application_id"]
        candidate["job_id"] = existing["job_id"]
        if existing.get("status") != "interested":
            candidate["applied_at"] = existing["applied_at"]
        if candidate == existing:
            return deepcopy(existing)
        document["items"][index] = candidate
    else:
        document["items"].append(candidate)
    document["items"].sort(key=lambda item: str(item["application_id"]))
    repository.write("applications", document)
    return deepcopy(candidate)


def record_confirmed_application(
    repository: TrackingRepository,
    *,
    dashboard_state: Mapping[str, Any],
    job_id: str,
    applied_at: str,
    resume_version: str,
    channel: str,
    confirmation_evidence: str,
    confirmed_at: str,
    confirm_submitted: bool,
) -> dict[str, Any]:
    """Confirm one submission locally; a browser marker is only a handoff candidate."""
    if confirm_submitted is not True:
        raise ValueError("--confirm-submitted is required; a browser marker is not proof of submission")
    if not isinstance(dashboard_state, Mapping):
        raise ValueError("dashboard export must be an object")
    version = dashboard_state.get("contract_version", 1)
    if isinstance(version, bool) or version != 1:
        raise ValueError("dashboard export has unsupported contract version")
    statuses = dashboard_state.get("statuses")
    if not isinstance(statuses, Mapping) or statuses.get(job_id) != "applied":
        raise ValueError("the selected job must have an Applied marker in the local export")
    for name, value in (("resume_version", resume_version), ("channel", channel), ("confirmation_evidence", confirmation_evidence)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must not be empty")
    jobs = _document(repository, "lifecycle")["items"]
    matches = [job for job in jobs if job.get("stable_id") == job_id]
    if len(matches) != 1:
        raise ValueError("selected job must have exactly one persisted lifecycle identity; run the local workflow first")
    job = matches[0]
    candidate = _normalize_application({
        "job_id": job_id, "company": job["company"], "title": job["title"],
        "country": job.get("country", ""), "status": "applied", "applied_at": applied_at,
        "updated_at": confirmed_at, "confirmed_at": confirmed_at,
        "resume_version": resume_version, "channel": channel,
        "confirmation_evidence": confirmation_evidence,
    })
    existing = [item for item in _document(repository, "applications")["items"] if item.get("job_id") == job_id]
    if len(existing) > 1:
        raise ValueError("application state contains duplicate identity")
    if existing and existing[0].get("confirmation_evidence"):
        for field in ("applied_at", "resume_version", "channel", "confirmation_evidence"):
            if candidate[field] != existing[0].get(field):
                raise ValueError("conflicting confirmed application replay; preserve the existing record for explicit correction")
        return deepcopy(existing[0])
    if existing and existing[0].get("status") != "interested":
        raise ValueError("an existing application must be reconciled explicitly; a marker cannot overwrite its history")
    return upsert_application(repository, candidate)


def record_application_event(
    repository: TrackingRepository,
    *,
    application_id: str,
    event_type: str,
    occurred_at: str,
    details: Mapping[str, Any],
    status: str | None = None,
) -> dict[str, Any]:
    applications = _document(repository, "applications")
    events = _document(repository, "events")
    matches = [
        (index, item)
        for index, item in enumerate(applications["items"])
        if item.get("application_id") == application_id
    ]
    if len(matches) != 1:
        raise ValueError(f"unknown application_id: {application_id}")
    if not isinstance(event_type, str) or not event_type.strip():
        raise ValueError("event_type must not be empty")
    if not isinstance(details, Mapping):
        raise ValueError("event details must be an object")
    occurred = _aware_datetime(occurred_at, "occurred_at")
    normalized_event_type = event_type.strip().casefold()
    normalized_status: str | None = None
    if status is not None:
        if not isinstance(status, str):
            raise ValueError("status must be a string")
        normalized_status = status.casefold()
    event_id = stable_event_id(
        application_id=application_id,
        event_type=normalized_event_type,
        occurred_at=occurred_at,
        details=details,
    )

    application_index, application = matches[0]
    updated_application = deepcopy(application)
    if normalized_status is not None:
        transition_application(str(application.get("status", "")), normalized_status)
        updated_application["status"] = normalized_status
    if normalized_event_type == "rejected":
        stage = details.get("stage")
        if not isinstance(stage, str) or not stage:
            raise ValueError("rejection event details must include stage")
        updated_application["rejection_stage"] = stage

    event = {
        "event_id": event_id,
        "application_id": application_id,
        "event_type": normalized_event_type,
        "occurred_at": occurred_at,
        "details": deepcopy(dict(details)),
    }
    if normalized_status is not None:
        event["requested_status"] = normalized_status

    same_identity = [
        item
        for item in events["items"]
        if item.get("application_id") == application_id
        and str(item.get("event_type", "")).casefold() == normalized_event_type
        and _aware_datetime(item.get("occurred_at"), "occurred_at") == occurred
    ]
    if same_identity:
        existing_event = next(
            (item for item in same_identity if item.get("event_id") == event_id),
            None,
        )
        if (
            existing_event is not None
            and existing_event.get("requested_status") == normalized_status
        ):
            return deepcopy(existing_event)
        raise ValueError("conflicting event replay")

    if updated_application != application:
        applications["items"][application_index] = updated_application
    events["items"].append(event)
    events["items"].sort(
        key=lambda item: (
            _aware_datetime(item["occurred_at"], "occurred_at"),
            str(item["event_id"]),
        )
    )
    repository.commit_application_event(applications, events)
    return deepcopy(event)


def _as_of(value: object) -> datetime:
    return _aware_datetime(value, "as_of")


def _add_business_days(value: date, days: int) -> date:
    result = value
    remaining = days
    while remaining:
        result += timedelta(days=1)
        if result.weekday() < 5:
            remaining -= 1
    return result


def due_actions(
    repository: TrackingRepository,
    *,
    as_of: str,
) -> list[dict[str, Any]]:
    now = _as_of(as_of)
    applications = _document(repository, "applications")["items"]
    events = _document(repository, "events")["items"]
    events_by_application: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        events_by_application.setdefault(str(event.get("application_id", "")), []).append(
            event
        )

    actions: list[dict[str, Any]] = []
    for raw_application in applications:
        application = (
            _normalize_application(raw_application)
            if "application_id" not in raw_application
            else raw_application
        )
        application_id = str(application["application_id"])
        status = str(application["status"])
        related = events_by_application.get(application_id, [])
        event_types = {str(event.get("event_type", "")).casefold() for event in related}
        applied_at = (
            None if application["applied_at"] == "unknown"
            else _aware_datetime(application["applied_at"], "applied_at")
        )
        age = now - applied_at if applied_at is not None else None
        if status == "applied" and applied_at is not None and age is not None and "follow_up_sent" not in event_types:
            if timedelta(days=7) <= age <= timedelta(days=10):
                actions.append(
                    {
                        "action": "application_follow_up",
                        "application_id": application_id,
                        "due_at": (applied_at + timedelta(days=7)).isoformat().replace(
                            "+00:00", "Z"
                        ),
                        "priority": "normal",
                    }
                )
            if age >= timedelta(days=14):
                actions.append(
                    {
                        "action": "no_response_review",
                        "application_id": application_id,
                        "due_at": (applied_at + timedelta(days=14)).isoformat().replace(
                            "+00:00", "Z"
                        ),
                        "priority": "normal",
                    }
                )

        if "thank_you_sent" not in event_types:
            interview_times = [
                _aware_datetime(event["occurred_at"], "occurred_at")
                for event in related
                if str(event.get("event_type", "")).casefold()
                == "interview_completed"
            ]
            if interview_times:
                latest = max(interview_times)
                elapsed = now - latest
                if timedelta(0) <= elapsed <= timedelta(hours=24):
                    actions.append(
                        {
                            "action": "interview_thank_you",
                            "application_id": application_id,
                            "due_at": (latest + timedelta(hours=24))
                            .isoformat()
                            .replace("+00:00", "Z"),
                            "priority": "normal",
                        }
                    )

        promised = application.get("promised_response_date")
        if isinstance(promised, str) and promised:
            parsed = parse_iso_date_or_datetime(promised)
            if isinstance(parsed, datetime):
                raise ValueError("promised_response_date must be an ISO date")
            due = _add_business_days(parsed, 2)
            if now.date() >= due:
                actions.append(
                    {
                        "action": "promised_response_follow_up",
                        "application_id": application_id,
                        "due_at": due.isoformat(),
                        "priority": "normal",
                    }
                )

        offer_deadline = application.get("offer_deadline")
        if status == "offer" and isinstance(offer_deadline, str) and offer_deadline:
            deadline = parse_iso_date_or_datetime(offer_deadline)
            if isinstance(deadline, datetime):
                raise ValueError("offer_deadline must be an ISO date")
            actions.append(
                {
                    "action": "offer_deadline",
                    "application_id": application_id,
                    "due_at": deadline.isoformat(),
                    "priority": "high",
                }
            )
    return sorted(
        actions,
        key=lambda item: (
            str(item["due_at"]),
            str(item["application_id"]),
            str(item["action"]),
        ),
    )


def build_metrics(repository: TrackingRepository) -> dict[str, Any]:
    applications = _document(repository, "applications")["items"]
    ordered = sorted(
        applications,
        key=lambda item: str(item.get("application_id", "")),
    )
    funnel = Counter(str(item.get("status", "")) for item in ordered)
    rejection_stages = Counter(
        str(item["rejection_stage"])
        for item in ordered
        if item.get("status") == "rejected" and item.get("rejection_stage")
    )
    slices: dict[str, dict[str, dict[str, Any]]] = {}
    for field in ("resume_version", "channel", "country"):
        counts = Counter(str(item.get(field, "")) for item in ordered if item.get(field))
        slices[field] = {
            value: {"count": count, "insufficient": count < 10}
            for value, count in sorted(counts.items())
        }
    return {
        "contract_version": 1,
        "total": len(ordered),
        "funnel": dict(sorted(funnel.items())),
        "rejection_stages": dict(sorted(rejection_stages.items())),
        "slices": slices,
    }


def metrics_json(repository: TrackingRepository) -> str:
    return json.dumps(
        build_metrics(repository),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
