from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping


MAX_JD_EVIDENCE_ITEMS = 8
MAX_JD_EVIDENCE_CHARS = 500


def validate_jd_evidence(job: Mapping[str, Any]) -> None:
    evidence = job.get("jd_evidence", ())
    if (
        not isinstance(evidence, (list, tuple))
        or len(evidence) > MAX_JD_EVIDENCE_ITEMS
        or not all(isinstance(item, str) and len(item) <= MAX_JD_EVIDENCE_CHARS for item in evidence)
    ):
        raise ValueError("jd_evidence must contain at most 8 strings of at most 500 characters")
    origin = job.get("jd_evidence_source", "")
    digest = job.get("jd_hash", "")
    if not isinstance(origin, str) or len(origin) > 200:
        raise ValueError("jd_evidence_source must be a string up to 200 characters")
    if not isinstance(digest, str) or (digest and not re.fullmatch(r"[0-9a-f]{64}", digest)):
        raise ValueError("jd_hash must be empty or a lowercase SHA256 digest")


@dataclass(frozen=True)
class Job:
    source: str
    external_id: str
    company: str
    title: str
    location: str
    url: str
    published_at: str
    score: int | None = None
    country: str = ""
    category: str = ""
    summary: str = ""
    risk: str = ""
    salary: str = ""
    tracks: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    first_seen: str = ""
    visa_supported: bool | None = None
    jd_evidence: tuple[str, ...] = ()
    jd_evidence_source: str = ""
    jd_hash: str = ""
    last_seen: str = ""

    def __post_init__(self) -> None:
        validate_jd_evidence(vars(self))
        object.__setattr__(self, "jd_evidence", tuple(self.jd_evidence))
