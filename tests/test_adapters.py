import pytest

from job_radar.adapters import (
    SourcePayloadTooLarge,
    fetch_ashby,
    fetch_greenhouse,
    fetch_lever,
    parse_ashby,
    parse_greenhouse,
    parse_lever,
)


def test_greenhouse_normalizes_published_jobs():
    jobs = parse_greenhouse(
        {"jobs": [{"id": 7, "title": "Backend Engineer", "absolute_url": "https://example/jobs/7", "location": {"name": "Toronto"}, "updated_at": "2026-07-13T00:00:00Z"}]},
        company="Example",
    )
    assert jobs[0].source == "greenhouse"
    assert jobs[0].external_id == "7"
    assert jobs[0].company == "Example"
    assert jobs[0].location == "Toronto"


def test_lever_normalizes_published_jobs():
    jobs = parse_lever(
        [{"id": "abc", "text": "Platform Engineer", "hostedUrl": "https://jobs.lever.co/example/abc", "categories": {"location": "Remote - Canada"}, "createdAt": 1783900800000}],
        company="Example",
    )
    assert jobs[0].source == "lever"
    assert jobs[0].title == "Platform Engineer"
    assert jobs[0].location == "Remote - Canada"
    assert jobs[0].published_at == "2026-07-13T00:00:00+00:00"


def test_ashby_normalizes_published_jobs_and_skips_unlisted():
    jobs = parse_ashby(
        {"jobs": [
            {"id": "open", "title": "AI Engineer", "jobUrl": "https://jobs.ashbyhq.com/example/open", "location": "Singapore", "publishedAt": "2026-07-13T00:00:00Z", "isListed": True},
            {"id": "hidden", "title": "Hidden", "jobUrl": "https://jobs.ashbyhq.com/example/hidden", "location": "Singapore", "isListed": False},
        ]},
        company="Example",
    )
    assert [job.external_id for job in jobs] == ["open"]
    assert jobs[0].source == "ashby"


@pytest.mark.parametrize("visibility", [None, 0, 1, "", "false", [], {}])
def test_ashby_rejects_non_boolean_visibility_before_filtering(visibility):
    with pytest.raises(ValueError, match="isListed"):
        parse_ashby({"jobs": [{"isListed": visibility}]}, "Example")


def test_ashby_explicitly_hidden_posting_does_not_require_listing_fields():
    assert parse_ashby({"jobs": [{"isListed": False}]}, "Example") == []


def test_ashby_missing_visibility_keeps_the_listing():
    assert len(parse_ashby({"jobs": [{"id": "open", "title": "Backend"}]}, "Example")) == 1


@pytest.mark.parametrize(
    ("parser", "payload"),
    [
        (
            parse_greenhouse,
            {
                "jobs": [
                    {
                        "id": index,
                        "title": "Backend",
                        "absolute_url": f"https://example.test/{index}",
                    }
                    for index in range(3)
                ]
            },
        ),
        (
            parse_lever,
            [
                {
                    "id": str(index),
                    "text": "Backend",
                    "hostedUrl": f"https://example.test/{index}",
                }
                for index in range(3)
            ],
        ),
        (
            parse_ashby,
            {
                "jobs": [
                    {
                        "id": str(index),
                        "title": "Backend",
                        "jobUrl": f"https://example.test/{index}",
                    }
                    for index in range(3)
                ]
            },
        ),
    ],
)
def test_all_official_adapters_reject_job_count_before_normalization(parser, payload):
    with pytest.raises(SourcePayloadTooLarge):
        parser(payload, "Example", max_jobs=2)


def test_official_adapters_use_documented_public_endpoints():
    calls = []

    def fake_get_json(url):
        calls.append(url)
        if "greenhouse" in url:
            return {"jobs": []}
        if "lever" in url:
            return []
        return {"jobs": []}

    assert fetch_greenhouse("example", "Example", fake_get_json) == []
    assert fetch_lever("example", "Example", fake_get_json) == []
    assert fetch_ashby("example", "Example", fake_get_json) == []
    assert calls == [
        "https://boards-api.greenhouse.io/v1/boards/example/jobs?content=true",
        "https://api.lever.co/v0/postings/example?mode=json",
        "https://api.ashbyhq.com/posting-api/job-board/example",
    ]


@pytest.mark.parametrize("parser", [parse_greenhouse, parse_lever, parse_ashby])
@pytest.mark.parametrize("payload", [None, {}, "", 0, False])
def test_official_adapters_reject_malformed_roots(parser, payload):
    with pytest.raises(ValueError):
        parser(payload, "Example")


@pytest.mark.parametrize("parser", [parse_greenhouse, parse_ashby])
@pytest.mark.parametrize("jobs", [None, {}, "", ()])
def test_object_envelopes_require_a_jobs_array(parser, jobs):
    with pytest.raises(ValueError):
        parser({"jobs": jobs}, "Example")


@pytest.mark.parametrize("parser", [parse_greenhouse, parse_lever, parse_ashby])
@pytest.mark.parametrize("entry", [None, [], "bad entry", 7, {}])
def test_official_adapters_reject_malformed_entries(parser, entry):
    payload = [entry] if parser is parse_lever else {"jobs": [entry]}
    with pytest.raises(ValueError):
        parser(payload, "Example")


def _posting(parser):
    if parser is parse_greenhouse:
        return {"id": 7, "title": "Backend", "absolute_url": "https://example.test/7"}
    if parser is parse_lever:
        return {"id": "7", "text": "Backend", "hostedUrl": "https://example.test/7"}
    return {"id": "7", "title": "Backend", "jobUrl": "https://example.test/7"}


@pytest.mark.parametrize("parser", [parse_greenhouse, parse_lever, parse_ashby])
@pytest.mark.parametrize("value", [None, True, [], ""])
def test_job_identity_must_not_be_fabricated_from_malformed_values(parser, value):
    posting = {**_posting(parser), "id": value}
    payload = [posting] if parser is parse_lever else {"jobs": [posting]}
    with pytest.raises(ValueError):
        parser(payload, "Example")


@pytest.mark.parametrize("parser", [parse_greenhouse, parse_lever, parse_ashby])
@pytest.mark.parametrize("value", [None, False, [], ""])
def test_required_title_must_be_nonempty_text(parser, value):
    title_field = "text" if parser is parse_lever else "title"
    posting = {**_posting(parser), title_field: value}
    payload = [posting] if parser is parse_lever else {"jobs": [posting]}
    with pytest.raises(ValueError):
        parser(payload, "Example")


@pytest.mark.parametrize("parser,payload", [
    (parse_greenhouse, {"jobs": []}),
    (parse_lever, []),
    (parse_ashby, {"jobs": []}),
])
def test_official_empty_job_results_remain_valid(parser, payload):
    assert parser(payload, "Example") == []


class _NoIterationList(list):
    def __iter__(self):
        raise AssertionError("oversized data must not be inspected or normalized")


@pytest.mark.parametrize("parser", [parse_greenhouse, parse_lever, parse_ashby])
def test_size_limit_precedes_entry_validation_and_job_construction(parser):
    entries = _NoIterationList([None, None])
    payload = entries if parser is parse_lever else {"jobs": entries}
    with pytest.raises(SourcePayloadTooLarge):
        parser(payload, "Example", max_jobs=1)


@pytest.mark.parametrize("value", [None, True, False, [], {}, "yesterday", "1783900800000",
                                  "2026-07-13T00:00:00", float("nan"), float("inf"), 10**100])
def test_lever_rejects_malformed_created_at(value):
    with pytest.raises(ValueError):
        parse_lever([{**_posting(parse_lever), "createdAt": value}], "Example")


@pytest.mark.parametrize("value,expected", [
    (1783900800123, "2026-07-13T00:00:00.123000+00:00"),
    ("2026-07-13T00:00:00Z", "2026-07-13T00:00:00+00:00"),
    ("2026-07-13T08:00:00+08:00", "2026-07-13T00:00:00+00:00"),
    ("", ""),
])
def test_lever_dates_normalize_without_local_timezone_assumptions(value, expected):
    job = parse_lever([{**_posting(parse_lever), "createdAt": value}], "Example")[0]
    assert job.published_at == expected


def test_lever_missing_created_at_remains_unknown():
    assert parse_lever([_posting(parse_lever)], "Example")[0].published_at == ""
