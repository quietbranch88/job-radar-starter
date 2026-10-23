from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from job_radar.ai_review import run_review_pipeline
from job_radar.bounded_process import run_bounded_process
from job_radar.domain.tracking import stable_job_id


_MAX_PROVIDER_RESPONSE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ExternalCommandReviewProvider:
    command: tuple[str, ...]

    def __init__(self, command: Sequence[str]):
        normalized = tuple(str(part) for part in command)
        if not normalized or not normalized[0].strip():
            raise ValueError("provider command must not be empty")
        object.__setattr__(self, "command", normalized)

    def review(self, mode: str, request: dict[str, Any]) -> dict[str, Any]:
        return self.review_with_timeout(mode, request, timeout_seconds=30)

    def review_with_timeout(
        self,
        mode: str,
        request: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        del mode
        try:
            completed = run_bounded_process(
                self.command,
                input_text=json.dumps(request, ensure_ascii=False),
                timeout_seconds=timeout_seconds,
                max_stdout_bytes=_MAX_PROVIDER_RESPONSE_BYTES,
                max_stderr_bytes=_MAX_PROVIDER_RESPONSE_BYTES,
            )
        except TimeoutError as error:
            raise TimeoutError("review provider timed out") from error
        if completed.returncode != 0:
            raise RuntimeError("review provider failed")
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise ValueError("review provider returned invalid JSON") from error
        if not isinstance(result, dict):
            raise ValueError("review provider response must be an object")
        return result


def _digest_job(job: Mapping[str, Any]) -> str:
    public_jd = {
        field: job.get(field)
        for field in (
            "source",
            "external_id",
            "company",
            "title",
            "location",
            "published_at",
            "country",
            "category",
            "summary",
            "risk",
            "tracks",
            "skills",
            "visa_supported",
            "jd_evidence",
            "jd_evidence_source",
            "jd_hash",
        )
    }
    encoded = json.dumps(
        public_jd,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compact_review_jobs(jobs: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for raw in jobs:
        job = deepcopy(dict(raw))
        score = job.get("score")
        if isinstance(score, bool) or not isinstance(score, int):
            continue
        stable_id = job.get("stable_id")
        if not isinstance(stable_id, str) or not stable_id:
            stable_id = stable_job_id(
                source=str(job.get("source", "")),
                external_id=str(job.get("external_id", "")),
                company=str(job.get("company", "")),
                title=str(job.get("title", "")),
            )
        explicit_evidence = job.get("jd_evidence", [])
        if not isinstance(explicit_evidence, (list, tuple)) or not all(
            isinstance(value, str) for value in explicit_evidence
        ):
            raise ValueError("jd_evidence must be a list of strings")
        public_skills = job.get("public_skills", [])
        if not isinstance(public_skills, (list, tuple)) or not all(
            isinstance(value, str) for value in public_skills
        ):
            raise ValueError("public_skills must be a list of strings")
        evidence = [
            value
            for value in (
                *explicit_evidence,
                job.get("location"),
                " ".join(public_skills),
            )
            if isinstance(value, str) and value.strip()
        ]
        compact.append(
            {
                "stable_id": stable_id,
                "title": str(job.get("title", "")),
                "company": str(job.get("company", "")),
                "country": str(job.get("country", "")) or "unknown",
                "local_fit": score,
                "jd_evidence": evidence,
                "jd_hash": _digest_job(job),
                "hard_excluded": score == 0,
                "hard_reason_codes": (
                    ["local_hard_exclusion"] if score == 0 else []
                ),
                "visa_supported": job.get("visa_supported"),
            }
        )
    return compact


@dataclass(frozen=True)
class ConfiguredExternalReviewRunner:
    command: tuple[str, ...]
    profile_rubric_hash: str = "public-local-profile-v1"
    prompt_version: str = "compact-review-v1"
    fast_model: str = "external-command-fast"
    strong_model: str = "external-command-strong"

    def __init__(self, command: Sequence[str]):
        normalized = tuple(str(part) for part in command)
        if not normalized:
            raise ValueError("provider command must not be empty")
        object.__setattr__(self, "command", normalized)

    def __call__(
        self,
        jobs: list[dict[str, Any]],
        *,
        observed_on: str,
        repository: Any,
        per_call_timeout_seconds: float,
        total_deadline_seconds: float,
        max_fast_calls: int,
    ) -> dict[str, Any]:
        provider = ExternalCommandReviewProvider(self.command)
        return run_review_pipeline(
            jobs=compact_review_jobs(jobs),
            repository=repository,
            fast_provider=provider,
            strong_provider=None,
            config={
                "contract_version": 1,
                "minimum_fit": 1,
                "near_threshold_margin": 5,
                "strong_fit_threshold": 90,
                "max_escalations": 10,
                "max_request_bytes": 4096,
                "max_evidence_items": 8,
                "max_evidence_chars": 500,
                "company_fact_ttl_days": 30,
                "profile_rubric_hash": self.profile_rubric_hash,
                "prompt_version": self.prompt_version,
                "fast_model": self.fast_model,
                "strong_model": self.strong_model,
                "per_call_timeout_seconds": per_call_timeout_seconds,
                "total_deadline_seconds": total_deadline_seconds,
                "max_fast_calls": max_fast_calls,
            },
            observed_on=observed_on,
        )
