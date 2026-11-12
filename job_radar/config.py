from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Profile:
    skills: tuple[str, ...]
    resume_path: str


@dataclass(frozen=True)
class Preferences:
    countries: tuple[str, ...]
    roles: tuple[str, ...]
    tracks: tuple[str, ...]
    visa_required: bool


@dataclass(frozen=True)
class Companies:
    preferred: tuple[str, ...]
    excluded: tuple[str, ...]


@dataclass(frozen=True)
class ScoreWeights:
    """Local point values. Penalties are positive numbers that scoring subtracts."""

    base: int = 30
    country: int = 10
    role: int = 15
    track: int = 10
    skill: int = 8
    skill_cap: int = 24
    preferred_company: int = 10
    must_have: int = 5
    missing_must_have: int = 20
    visa_listed: int = 5
    visa_absent: int = 25
    visa_unknown: int = 8


@dataclass(frozen=True)
class Matching:
    minimum_score: int
    must_have: tuple[str, ...]
    exclude_keywords: tuple[str, ...]
    weights: ScoreWeights = field(default_factory=ScoreWeights)


_WEIGHT_LIMITS = {
    "base": (0, 100),
    "country": (0, 100),
    "role": (0, 100),
    "track": (0, 100),
    "skill": (0, 100),
    "skill_cap": (0, 100),
    "preferred_company": (0, 100),
    "must_have": (0, 100),
    "missing_must_have": (0, 100),
    "visa_listed": (0, 100),
    "visa_absent": (0, 100),
    "visa_unknown": (0, 100),
}


@dataclass(frozen=True)
class UserConfig:
    profile: Profile
    preferences: Preferences
    companies: Companies
    matching: Matching


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a mapping")
    return value


def _score_weights(matching: dict[str, Any]) -> ScoreWeights:
    raw = matching.get("weights", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("matching.weights must be a mapping")
    unknown = sorted(set(raw) - set(_WEIGHT_LIMITS))
    if unknown:
        raise ValueError(f"matching.weights has unsupported fields: {', '.join(unknown)}")
    values: dict[str, int] = {}
    for name, (low, high) in _WEIGHT_LIMITS.items():
        if name not in raw:
            continue
        value = raw[name]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(
                f"matching.weights.{name} must be an integer between {low} and {high}"
            )
        values[name] = value
    return ScoreWeights(**values)


def load_user_config(path: Path) -> UserConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("configuration root must be a mapping")

    profile = _section(raw, "profile")
    preferences = _section(raw, "preferences")
    companies = _section(raw, "companies")
    matching = _section(raw, "matching")
    minimum_score = int(matching.get("minimum_score", 0))
    if not 0 <= minimum_score <= 100:
        raise ValueError("matching.minimum_score must be between 0 and 100")
    return UserConfig(
        profile=Profile(
            skills=tuple(profile.get("skills", ())),
            resume_path=str(profile.get("resume_path", "")),
        ),
        preferences=Preferences(
            countries=tuple(preferences.get("countries", ())),
            roles=tuple(preferences.get("roles", ())),
            tracks=tuple(preferences.get("tracks", ())),
            visa_required=bool(preferences.get("visa_required", False)),
        ),
        companies=Companies(
            preferred=tuple(companies.get("preferred", ())),
            excluded=tuple(companies.get("excluded", ())),
        ),
        matching=Matching(
            minimum_score=minimum_score,
            must_have=tuple(matching.get("must_have", ())),
            exclude_keywords=tuple(matching.get("exclude_keywords", ())),
            weights=_score_weights(matching),
        ),
    )
