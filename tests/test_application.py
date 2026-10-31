from pathlib import Path

from job_radar.catalog import Catalog, Company, Source
from job_radar.source_orchestration import scan_catalog_resilient


def _catalog() -> Catalog:
    return Catalog(
        countries={},
        sources={
            "greenhouse": Source("official_api", "https://example/greenhouse", True),
            "lever": Source("official_api", "https://example/lever", True),
            "ashby": Source("official_api", "https://example/ashby", True),
        },
        companies={
            "GH Co": Company(("CA",), "greenhouse", "gh"),
            "Lever Co": Company(("GB",), "lever", "lever"),
            "Ashby Co": Company(("SG",), "ashby", "ashby"),
        },
    )


def _scan(catalog: Catalog, get_json, output: Path):
    return scan_catalog_resilient(
        catalog,
        get_json,
        mode="best-effort",
        per_source_timeout=5,
        output=output,
        inline=True,
        observed_on="2026-07-15",
    )


def test_scan_catalog_routes_each_company_to_its_official_adapter(tmp_path: Path):
    def fake_get_json(url):
        if "greenhouse" in url:
            return {"jobs": [{"id": 1, "title": "Backend", "absolute_url": "https://example/1", "location": {"name": "Canada"}}]}
        if "lever" in url:
            return [{"id": "2", "text": "Platform", "hostedUrl": "https://example/2", "categories": {"location": "UK"}}]
        return {"jobs": [{"id": "3", "title": "AI", "jobUrl": "https://example/3", "location": "Singapore", "isListed": True}]}

    result = _scan(_catalog(), fake_get_json, tmp_path / "jobs.json")

    assert result["failures"] == []
    assert [(job["company"], job["source"]) for job in result["jobs"]] == [
        ("Ashby Co", "ashby"),
        ("GH Co", "greenhouse"),
        ("Lever Co", "lever"),
    ]


def test_failed_source_is_recorded_without_aborting_the_catalog(tmp_path: Path):
    catalog = Catalog(
        countries={},
        sources={
            "greenhouse": Source("official_api", "https://example/greenhouse", True),
            "lever": Source("official_api", "https://example/lever", True),
        },
        companies={
            "Broken Board": Company(("CA",), "greenhouse", "broken"),
            "Healthy Co": Company(("GB",), "lever", "healthy"),
        },
    )

    def fake_get_json(url):
        if "greenhouse" in url:
            raise ValueError("response too large")
        return [{"id": "2", "text": "Platform", "hostedUrl": "https://example/2", "categories": {"location": "UK"}}]

    result = _scan(catalog, fake_get_json, tmp_path / "jobs.json")

    assert result["incomplete"] is True
    assert [(job["company"], job["source"]) for job in result["jobs"]] == [("Healthy Co", "lever")]
    assert result["failures"] == [
        {
            "source": "greenhouse",
            "company": "Broken Board",
            "category": "malformed",
            "message": "source returned malformed data",
        }
    ]
