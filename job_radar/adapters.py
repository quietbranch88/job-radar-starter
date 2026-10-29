import hashlib
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from typing import Any, Callable

from job_radar.domain.jobs import Job, MAX_JD_EVIDENCE_CHARS, MAX_JD_EVIDENCE_ITEMS


JsonGetter = Callable[[str], Any]


class SourcePayloadTooLarge(ValueError):
    pass


def _posting_items(payload: Any, *, envelope: bool, max_jobs: int | None) -> list[dict[str, Any]]:
    if envelope:
        if not isinstance(payload, dict) or "jobs" not in payload:
            raise ValueError("source response must contain a jobs array")
        items = payload["jobs"]
    else:
        items = payload
    if not isinstance(items, list):
        raise ValueError("source jobs must be an array")
    # Keep the producer limit ahead of entry inspection and Job construction.
    if max_jobs is not None and len(items) > max_jobs:
        raise SourcePayloadTooLarge
    if any(not isinstance(item, dict) for item in items):
        raise ValueError("source job entries must be objects")
    return items


def _posting_id(item: dict[str, Any]) -> str:
    value = item.get("id")
    if type(value) not in (str, int) or not str(value).strip():
        raise ValueError("source job id must be a nonempty string or integer")
    return str(value)


def _required_text(item: dict[str, Any], field: str) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"source job {field} must be nonempty text")
    return value


def _optional_text(item: dict[str, Any], field: str) -> str:
    value = item.get(field)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"source job {field} must be text")
    return value


def _nested_text(item: dict[str, Any], field: str, nested_field: str) -> str:
    value = item.get(field)
    if value is None:
        return ""
    if not isinstance(value, dict):
        raise ValueError(f"source job {field} must be an object")
    return _optional_text(value, nested_field)


def _lever_created_at(item: dict[str, Any]) -> str:
    if "createdAt" not in item or item["createdAt"] == "":
        return ""
    value = item["createdAt"]
    try:
        if type(value) in (int, float):
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.utcoffset() is not None:
                return parsed.astimezone(timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        pass
    raise ValueError("Lever createdAt must be epoch milliseconds or an aware ISO datetime")


class _PostingText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        elif not self.hidden:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _public_jd(source: str, **fields: Any) -> dict[str, Any]:
    texts: list[str] = []
    origins: list[str] = []
    for field, value in fields.items():
        if not isinstance(value, str) or not value.strip():
            continue
        parser = _PostingText()
        parser.feed(unescape(value))
        text = " ".join("".join(parser.parts).split())
        if text:
            texts.append(text)
            origins.append(field)
    content = " ".join(texts)
    if not content:
        return {}
    return {
        "jd_evidence": tuple(
            content[offset:offset + MAX_JD_EVIDENCE_CHARS]
            for offset in range(0, min(len(content), MAX_JD_EVIDENCE_ITEMS * MAX_JD_EVIDENCE_CHARS), MAX_JD_EVIDENCE_CHARS)
        ),
        "jd_evidence_source": source + ":" + "+".join(origins),
        "jd_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def _lever_jd(item: dict[str, Any]) -> dict[str, Any]:
    fields = {"descriptionPlain": item.get("descriptionPlain")} if item.get("descriptionPlain") else {"description": item.get("description")}
    sections = item.get("lists", [])
    if isinstance(sections, list):
        fields["lists"] = " ".join(
            str(section.get("text", "")) + " " + str(section.get("content", ""))
            for section in sections if isinstance(section, dict)
        )
    fields["additionalPlain" if item.get("additionalPlain") else "additional"] = item.get("additionalPlain") or item.get("additional")
    return _public_jd("lever", **fields)


def parse_greenhouse(
    payload: dict[str, Any],
    company: str,
    *,
    max_jobs: int | None = None,
) -> list[Job]:
    items = _posting_items(payload, envelope=True, max_jobs=max_jobs)
    return [
        Job(
            source="greenhouse",
            external_id=_posting_id(item),
            company=company,
            title=_required_text(item, "title"),
            location=_nested_text(item, "location", "name"),
            url=_required_text(item, "absolute_url"),
            published_at=_optional_text(item, "updated_at"),
            **_public_jd("greenhouse", content=item.get("content")),
        )
        for item in items
    ]


def parse_lever(
    payload: list[dict[str, Any]],
    company: str,
    *,
    max_jobs: int | None = None,
) -> list[Job]:
    items = _posting_items(payload, envelope=False, max_jobs=max_jobs)
    return [
        Job(
            source="lever",
            external_id=_posting_id(item),
            company=company,
            title=_required_text(item, "text"),
            location=_nested_text(item, "categories", "location"),
            url=_required_text(item, "hostedUrl"),
            published_at=_lever_created_at(item),
            **_lever_jd(item),
        )
        for item in items
    ]


def parse_ashby(
    payload: dict[str, Any],
    company: str,
    *,
    max_jobs: int | None = None,
) -> list[Job]:
    items = _posting_items(payload, envelope=True, max_jobs=max_jobs)
    for item in items:
        if "isListed" in item and type(item["isListed"]) is not bool:
            raise ValueError("source job isListed must be boolean")
    return [
        Job(
            source="ashby",
            external_id=_posting_id(item),
            company=company,
            title=_required_text(item, "title"),
            location=_optional_text(item, "location"),
            url=_optional_text(item, "jobUrl") or _optional_text(item, "applyUrl"),
            published_at=_optional_text(item, "publishedAt"),
            **_public_jd("ashby", **(
                {"descriptionPlain": item.get("descriptionPlain")}
                if item.get("descriptionPlain") else {"descriptionHtml": item.get("descriptionHtml")}
            )),
        )
        for item in items
        if item.get("isListed", True)
    ]


def fetch_greenhouse(
    board_token: str,
    company: str,
    get_json: JsonGetter,
    *,
    max_jobs: int | None = None,
) -> list[Job]:
    payload = get_json(f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true")
    return parse_greenhouse(payload, company, max_jobs=max_jobs)


def fetch_lever(
    site: str,
    company: str,
    get_json: JsonGetter,
    *,
    max_jobs: int | None = None,
) -> list[Job]:
    payload = get_json(f"https://api.lever.co/v0/postings/{site}?mode=json")
    return parse_lever(payload, company, max_jobs=max_jobs)


def fetch_ashby(
    board_name: str,
    company: str,
    get_json: JsonGetter,
    *,
    max_jobs: int | None = None,
) -> list[Job]:
    payload = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{board_name}")
    return parse_ashby(payload, company, max_jobs=max_jobs)
