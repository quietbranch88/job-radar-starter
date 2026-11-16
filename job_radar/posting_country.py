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
    "tw": ("taiwan",),
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


def location_names_other_country(location: str, country_code: str) -> bool:
    """True when the location names a recognized country other than this code."""

    stated = str(country_code or "").strip().casefold()
    for code in _HINTS:
        if code in {stated, "remote"}:
            continue
        if location_matches_country(location, code):
            return True
    return False


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


_STATED_CODES = {
    "united states of america": "US",
}
for _code, _hints in _HINTS.items():
    if _code == "remote":
        continue
    _STATED_CODES[_code] = _code.upper()
    for _hint in _hints:
        _STATED_CODES[_hint] = _code.upper()


def stated_country_code(value: object) -> str:
    """Read a board's country field. A city line is not a country field."""

    if not isinstance(value, str):
        return ""
    text = " ".join(value.casefold().split())
    if text in _STATED_CODES:
        return _STATED_CODES[text]
    if text.endswith(".") and text[:-1] in _STATED_CODES:
        return _STATED_CODES[text[:-1]]
    return ""


def agreed_country(locations: Sequence[str]) -> str:
    """One country when every office that names a country names the same one.

    A city line such as San Francisco names no country and is skipped. An
    office that names two countries, or two offices that name different
    countries, leaves the posting unlabeled.
    """

    found: list[str] = []
    for location in locations:
        if not isinstance(location, str) or not location.strip():
            continue
        codes = [
            code.upper()
            for code in _HINTS
            if code != "remote" and location_matches_country(location, code)
        ]
        if len(codes) > 1:
            return ""
        if len(codes) == 1:
            found.append(codes[0])
    if found and len(set(found)) == 1:
        return found[0]
    return ""


def resolve_posting_country(countries: Sequence[str] | str, location: str, stated: str = "") -> str:
    """Prefer a structured country that is one of the company's markets."""

    listed = _listed(countries)
    if len(listed) == 1:
        return listed[0]
    stated_code = stated_country_code(stated)
    if stated_code:
        for code in listed:
            if code.casefold() == stated_code.casefold():
                return code
    return posting_country(listed, location)
