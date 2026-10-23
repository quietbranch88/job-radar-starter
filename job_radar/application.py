from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from job_radar.config import UserConfig
from job_radar.domain.jobs import Job


_AI_FIELDS = {
    "source",
    "external_id",
    "score",
    "summary",
    "risk",
    "tracks",
    "skills",
    "visa_supported",
}


def _terms(job: Job) -> str:
    return " ".join(
        (
            job.company,
            job.title,
            job.location,
            job.country,
            job.category,
            *job.tracks,
            *job.skills,
            *job.jd_evidence,
        )
    ).casefold()


def _contains(text: str, term: str) -> bool:
    return bool(term.strip()) and term.casefold() in text


def score_jobs(jobs: Sequence[Job], config: UserConfig) -> list[Job]:
    """Score jobs with transparent, generic local rules.

    The rules only use structured configuration. The resume path is not opened.
    """

    preferred = {company.casefold() for company in config.companies.preferred}
    excluded = {company.casefold() for company in config.companies.excluded}
    scored: list[Job] = []

    for job in jobs:
        text = _terms(job)
        company = job.company.casefold()
        blocked_keyword = next(
            (term for term in config.matching.exclude_keywords if _contains(text, term)),
            None,
        )
        if company in excluded:
            scored.append(
                replace(
                    job,
                    score=0,
                    summary="Excluded by local preferences.",
                    risk="excluded company",
                )
            )
            continue
        if blocked_keyword:
            scored.append(
                replace(
                    job,
                    score=0,
                    summary="Excluded by local preferences.",
                    risk=f"excluded keyword: {blocked_keyword}",
                )
            )
            continue

        score = 30
        matches: list[str] = []
        risks: list[str] = []

        if job.country and job.country.casefold() in {
            country.casefold() for country in config.preferences.countries
        }:
            score += 10
            matches.append("country")

        role_matches = [role for role in config.preferences.roles if _contains(text, role)]
        if role_matches:
            score += 15
            matches.append("role")

        track_matches = [track for track in config.preferences.tracks if _contains(text, track)]
        if track_matches:
            score += 10
            matches.append("track")

        skill_matches = [skill for skill in config.profile.skills if _contains(text, skill)]
        if skill_matches:
            score += min(24, 8 * len(skill_matches))
            matches.append("skills")

        if company in preferred:
            score += 10
            matches.append("preferred company")

        missing_must_have = [
            term for term in config.matching.must_have if not _contains(text, term)
        ]
        if missing_must_have:
            score -= 20
            risks.append("missing must-have: " + ", ".join(missing_must_have))
        elif config.matching.must_have:
            score += 5
            matches.append("must-have")

        if config.preferences.visa_required:
            if job.visa_supported is True:
                score += 5
                matches.append("visa")
            elif job.visa_supported is False:
                score -= 25
                risks.append("visa support not indicated")
            else:
                score -= 8
                risks.append("verify visa support")

        normalized_score = max(0, min(100, score))
        summary = (
            "Matched local preferences: " + ", ".join(matches)
            if matches
            else "No strong local preferences matched."
        )
        scored.append(
            replace(
                job,
                score=normalized_score,
                summary=summary,
                risk="; ".join(risks),
                tracks=job.tracks or tuple(track_matches),
                skills=job.skills or tuple(skill_matches),
            )
        )

    return scored


def build_legacy_ai_request(
    jobs: Sequence[Job],
    config: UserConfig,
) -> dict[str, Any]:
    return {
        "contract_version": 1,
        "profile": {"skills": list(config.profile.skills)},
        "preferences": {
            "countries": list(config.preferences.countries),
            "roles": list(config.preferences.roles),
            "tracks": list(config.preferences.tracks),
            "visa_required": config.preferences.visa_required,
        },
        "companies": {
            "preferred": list(config.companies.preferred),
            "excluded": list(config.companies.excluded),
        },
        "matching": {
            "minimum_score": config.matching.minimum_score,
            "must_have": list(config.matching.must_have),
            "exclude_keywords": list(config.matching.exclude_keywords),
        },
        "jobs": [
            {
                "source": job.source,
                "external_id": job.external_id,
                "company": job.company,
                "title": job.title,
                "location": job.location,
                "url": job.url,
                "published_at": job.published_at,
                "country": job.country,
                "category": job.category,
                "salary": job.salary,
                "tracks": list(job.tracks),
                "skills": list(job.skills),
                "first_seen": job.first_seen,
                "visa_supported": job.visa_supported,
            }
            for job in jobs
        ],
    }


def _string_list(
    item: Mapping[str, Any],
    field: str,
    fallback: tuple[str, ...],
) -> tuple[str, ...]:
    if field not in item:
        return fallback
    value = item[field]
    if not isinstance(value, list) or not all(isinstance(entry, str) for entry in value):
        raise ValueError(f"AI response {field} must be a list of strings")
    return tuple(value)


def score_jobs_with_provider(
    jobs: Sequence[Job],
    config: UserConfig,
    provider: Callable[[dict[str, Any]], Mapping[str, Any]],
) -> list[Job]:
    baseline = score_jobs(jobs, config)
    payload = provider(build_legacy_ai_request(jobs, config))
    if not isinstance(payload.get("scores"), list):
        raise ValueError("AI response must contain a scores list")

    by_key = {(job.source, job.external_id): job for job in baseline}
    seen: set[tuple[str, str]] = set()
    for item in payload["scores"]:
        if not isinstance(item, Mapping):
            raise ValueError("AI score entries must be objects")
        unexpected = set(item) - _AI_FIELDS
        if unexpected:
            raise ValueError(
                f"AI response contains unsupported fields: {sorted(unexpected)}"
            )
        source = item.get("source")
        external_id = item.get("external_id")
        if not isinstance(source, str) or not isinstance(external_id, str):
            raise ValueError("AI response source and external_id must be strings")
        key = (source, external_id)
        if key not in by_key:
            raise ValueError(f"AI response references unknown job: {source}/{external_id}")
        if key in seen:
            raise ValueError(f"AI response contains duplicate job: {source}/{external_id}")
        seen.add(key)

        score = item.get("score")
        if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
            raise ValueError("AI response score must be an integer between 0 and 100")
        current = by_key[key]
        summary = item.get("summary", current.summary)
        risk = item.get("risk", current.risk)
        if not isinstance(summary, str) or not isinstance(risk, str):
            raise ValueError("AI response summary and risk must be strings")
        visa_supported = item.get("visa_supported", current.visa_supported)
        if visa_supported is not None and not isinstance(visa_supported, bool):
            raise ValueError("AI response visa_supported must be boolean or null")
        tracks = _string_list(item, "tracks", current.tracks)
        skills = _string_list(item, "skills", current.skills)
        if current.score == 0 and current.summary == "Excluded by local preferences.":
            continue
        by_key[key] = replace(
            current,
            score=score,
            summary=summary,
            risk=risk,
            tracks=tracks,
            skills=skills,
            visa_supported=visa_supported,
        )
    return [by_key[(job.source, job.external_id)] for job in baseline]
