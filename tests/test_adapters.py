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
