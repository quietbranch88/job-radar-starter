"""Assign a posting country from its location, not from a company's market list."""

from __future__ import annotations

import re
from collections.abc import Sequence


# Short names that appear in public Greenhouse, Lever, and Ashby locations.
_HINTS = {
    "us": ("united states", "u.s.a", "u.s.", "usa"),
    "ca": ("canada",),
    "gb": ("united kingdom", "u.k.", "uk", "england", "scotland", "wales"),
    "sg": ("singapore",),
    "jp": ("japan",),
    "au": ("australia",),
    "de": ("germany",),
    "fr": ("france",),
    "nl": ("netherlands",),
    "ie": ("ireland",),
    "in": ("india",),
    "remote": ("remote",),
}


def scope_country(countries: Sequence[str]) -> str:
    """Stable checkpoint label. One market stays that code; several are sorted."""
    cleaned = [code.strip() for code in countries if isinstance(code, str) and code.strip()]
    if len(cleaned) <= 1:
        return cleaned[0] if cleaned else ""
    return ",".join(sorted(set(cleaned), key=str.casefold))


def _listed(countries: Sequence[str] | str) -> tuple[str, ...]:
    if isinstance(countries, str):
        if not countries:
            return ()
        if "," in countries:
            return tuple(part for part in countries.split(",") if part)
        return (countries,)
    return tuple(code.strip() for code in countries if isinstance(code, str) and code.strip())


# These codes are also ordinary location tokens: CA is California, IN is
# Indiana, and DE is the German article inside Île-de-France. Match them by
# the country name only.
_AMBIGUOUS_CODES = frozenset({"ca", "in", "de"})
_REGION_HINTS = frozenset({"england", "scotland", "wales"})
_FALSE_FRIEND = re.compile(
    r"(?<![a-z0-9])new england(?![a-z0-9])|(?<![a-z0-9])new south wales(?![a-z0-9])"
)


def _contains_term(text: str, term: str) -> bool:
    if term in _REGION_HINTS and _FALSE_FRIEND.search(text):
        return False
    return re.search(rf"(?<![a-z0-9-]){re.escape(term)}(?![a-z0-9-])", text) is not None


def location_matches_country(location: str, code: str) -> bool:
    if not isinstance(location, str) or not isinstance(code, str):
        return False
    text = location.casefold()
    folded = code.strip().casefold()
    if not text or not folded:
        return False
    hints = _HINTS.get(folded, ())
    terms = hints if folded in _AMBIGUOUS_CODES else (folded, *hints)
    return any(_contains_term(text, term) for term in terms)


def posting_country(countries: Sequence[str] | str, location: str) -> str:
    """One listed market stays on the posting. Several markets resolve from location text."""
    listed = _listed(countries)
    if len(listed) == 1:
        return listed[0]
    matches: list[str] = []
    seen: set[str] = set()
    for code in listed:
        if not location_matches_country(location, code):
            continue
        key = code.casefold()
        if key in seen:
            continue
        seen.add(key)
        matches.append(code)
    if len(matches) == 1:
        return matches[0]
    return ""
