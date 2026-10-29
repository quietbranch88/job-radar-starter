"""Public ATS fixtures through real spawned scanners and the local CLI workflow."""

import json
from io import StringIO
from pathlib import Path

import pytest

from job_radar.catalog import Catalog, Company, Source
from job_radar.cli import main
from job_radar.source_orchestration import AtomicSourceScanFailed, scan_catalog_resilient


def malformed_official_response(_url):
    return {}


def malformed_ashby_visibility_response(_url):
    return {"jobs": [{"isListed": None}]}


def official_lever_response(url):
    assert url == "https://api.lever.co/v0/postings/example?mode=json"
    return [{"id": "lever-1", "text": "Backend Engineer", "categories": {"location": "Canada"},
             "hostedUrl": "https://jobs.lever.co/example/lever-1", "createdAt": 1783900800000}]


@pytest.mark.parametrize("source,get_json", [
    ("greenhouse", malformed_official_response),
    ("lever", malformed_official_response),
    ("ashby", malformed_official_response),
    ("ashby", malformed_ashby_visibility_response),
])
def test_malformed_payload_cannot_replace_output_or_complete_checkpoint(tmp_path: Path, source, get_json):
    catalog = Catalog(countries={}, sources={source: Source("official_api", "https://example.test/terms", True)},
                      companies={"Example": Company(("CA",), source, "example")})
    output = tmp_path / "latest.json"
    output.write_text('{"previous":true}', encoding="utf-8")
    checkpoint = tmp_path / "walk.json"

    with pytest.raises(AtomicSourceScanFailed) as failure:
        scan_catalog_resilient(catalog, get_json, mode="atomic", per_source_timeout=2,
                               output=output, checkpoint=checkpoint)

    assert failure.value.result["incomplete"] is True
    assert failure.value.result["failures"][0]["category"] == "malformed"
    assert output.read_text(encoding="utf-8") == '{"previous":true}'
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))["companies"][0]
    assert saved["state"] == "failed"
    assert saved["fetched_at"] is None
    assert saved["jobs_hash"] is None


def test_lever_epoch_milliseconds_flow_through_spawned_scan_and_public_run(tmp_path: Path):
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text("""
sources:
  lever: {kind: official_api, terms_url: https://github.com/lever/postings-api, enabled: true}
companies:
  Example: {countries: [CA], source: lever, board: example}
""", encoding="utf-8")
    profile = tmp_path / "profile.yaml"
    profile.write_text("matching: {minimum_score: 0}\n", encoding="utf-8")
    output = tmp_path / "latest.json"
    dashboard = tmp_path / "index.html"
    report = StringIO()

    result = main([
        "--json", "run", "--catalog", str(catalog), "--profile", str(profile),
        "--jobs-output", str(output), "--dashboard-output", str(dashboard),
        "--user-data", str(tmp_path / "user-data"), "--aliases", str(tmp_path / "absent.json"),
        "--checkpoint", str(tmp_path / "walk.json"), "--source-timeout", "2",
    ], get_json=official_lever_response, stdout=report)

    assert result == 0, report.getvalue()
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["incomplete"] is False
    assert payload["jobs"][0]["published_at"] == "2026-07-13T00:00:00+00:00"
    lifecycle = json.loads((tmp_path / "user-data/tracking/lifecycle.json").read_text(encoding="utf-8"))
    assert lifecycle["items"][0]["published_at"] == "2026-07-13T00:00:00+00:00"
    assert "Backend Engineer" in dashboard.read_text(encoding="utf-8")
    assert json.loads(report.getvalue())["stages"]["build"] == "complete"
