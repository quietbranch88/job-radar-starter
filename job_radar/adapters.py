import hashlib
from html import unescape
from html.parser import HTMLParser
from typing import Any, Callable

from job_radar.domain.jobs import Job, MAX_JD_EVIDENCE_CHARS, MAX_JD_EVIDENCE_ITEMS


JsonGetter = Callable[[str], Any]


class SourcePayloadTooLarge(ValueError):
    pass


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
    items = payload.get("jobs", ())
    if max_jobs is not None and isinstance(items, list) and len(items) > max_jobs:
        raise SourcePayloadTooLarge
    return [
        Job(
            source="greenhouse",
            external_id=str(item["id"]),
            company=company,
            title=str(item["title"]),
            location=str((item.get("location") or {}).get("name", "")),
            url=str(item["absolute_url"]),
            published_at=str(item.get("updated_at", "")),
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
    if max_jobs is not None and len(payload) > max_jobs:
        raise SourcePayloadTooLarge
    return [
        Job(
            source="lever",
            external_id=str(item["id"]),
            company=company,
            title=str(item["text"]),
            location=str((item.get("categories") or {}).get("location", "")),
            url=str(item["hostedUrl"]),
            published_at=str(item.get("createdAt", "")),
            **_lever_jd(item),
        )
        for item in payload
    ]


def parse_ashby(
    payload: dict[str, Any],
    company: str,
    *,
    max_jobs: int | None = None,
) -> list[Job]:
    items = payload.get("jobs", ())
    if max_jobs is not None and isinstance(items, list) and len(items) > max_jobs:
        raise SourcePayloadTooLarge
    return [
        Job(
            source="ashby",
            external_id=str(item["id"]),
            company=company,
            title=str(item["title"]),
            location=str(item.get("location", "")),
            url=str(item.get("jobUrl") or item.get("applyUrl") or ""),
            published_at=str(item.get("publishedAt", "")),
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
