from __future__ import annotations

import json
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

from job_radar.cli import main


ROOT = Path(__file__).resolve().parents[1]
PRIVACY_SCANNER = ROOT / "scripts" / "verify-tracked-privacy.ps1"
SYNTHETIC_PRIVACY_SENTINEL = "private-owner-" + "sentinel.invalid"


def _text(relative: str) -> str:
    path = ROOT / relative
    assert path.is_file(), f"missing release documentation: {relative}"
    return path.read_text(encoding="utf-8")


def _json(relative: str) -> dict:
    return json.loads(_text(relative))


def test_readme_is_agent_neutral_and_does_not_present_one_vendor_as_required():
    readme = _text("README.md").casefold()

    for actor in ("claude", "codex", "other agents", "normal scripts"):
        assert actor in readme
    assert "claude-only" not in readme
    assert "requires claude" not in readme
    assert "requires codex" not in readme
    assert "any agent or script" in readme


def test_local_data_retention_export_and_deletion_boundaries_are_explicit():
    readme = _text("README.md").casefold()
    privacy = _text("docs/privacy-and-data.md").casefold()
    combined = readme + privacy

    for boundary in (
        "user-data/",
        "scans/",
        "browser localstorage",
        "review/cache.json",
        "review/company-facts.json",
        "tracking/rejected.json",
        "export_to",
        "delete_tracking_data",
        "exported files are not deleted",
        "local-only by default",
    ):
        assert boundary in combined
    assert "automatic cloud upload" not in combined


def test_docs_name_exact_compact_ai_and_legacy_command_fields():
    external = _text("docs/external-data-contracts.md").casefold()

    compact_fields = {
        "contract_version",
        "stable_id",
        "title",
        "company",
        "country",
        "local_fit",
        "jd_evidence",
    }
    for field in compact_fields:
        assert f"`{field}`" in external
    for forbidden in (
        "full_jd",
        "resume contents",
        "application_history",
        "interview notes",
        "job url",
        "jd_hash",
        "profile_rubric_hash",
        "local summary",
        "local risk",
    ):
        assert f"not sent: `{forbidden}`" in external or forbidden in external

    for section in ("legacy `--ai-command`", "compact `review` pipeline"):
        assert section in external
    for legacy_field in (
        "profile.skills",
        "preferences.countries",
        "preferences.roles",
        "preferences.tracks",
        "preferences.visa_required",
        "companies.preferred",
        "companies.excluded",
        "matching.minimum_score",
        "matching.must_have",
        "matching.exclude_keywords",
        "jobs",
    ):
        assert f"`{legacy_field}`" in external


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16-le", "utf-16-be"])
def test_privacy_scanner_decodes_supported_text_and_blocks_sentinel(
    tmp_path: Path,
    encoding: str,
):
    tracked = tmp_path / f"tracked-{encoding}.txt"
    bom = {
        "utf-8": b"\xef\xbb\xbf",
        "utf-16-le": b"\xff\xfe",
        "utf-16-be": b"\xfe\xff",
    }[encoding]
    tracked.write_bytes(bom + SYNTHETIC_PRIVACY_SENTINEL.encode(encoding))

    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-File",
            str(PRIVACY_SCANNER),
            "-RepoRoot",
            str(tmp_path),
            "-TrackedPaths",
            str(tracked),
            "-BlockedMarkers",
            SYNTHETIC_PRIVACY_SENTINEL,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "privacy scan found blocked metadata" in (
        completed.stdout + completed.stderr
    ).casefold()

def test_committed_privacy_scanner_uses_only_injected_or_synthetic_markers():
    scanner = _text("scripts/verify-tracked-privacy.ps1").casefold()

    for private_default in (
        "$ownername",
        "$ownerhandle",
        "$ownersite",
        "$deploymentname",
        "$localownerpath",
    ):
        assert private_default not in scanner
    assert "blockedmarkers" in scanner
    assert "job_radar_private_markers" in scanner
    assert "private-owner-" in scanner
    assert "sentinel.invalid" in scanner


def test_privacy_scanner_rejects_blocked_marker_in_copyright_shaped_line(
    tmp_path: Path,
):
    marker = "private-license-" + "sentinel.invalid"
    license_path = tmp_path / "LICENSE"
    license_path.write_text(
        f"Copyright (c) 2026 {marker}\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-File",
            str(PRIVACY_SCANNER),
            "-RepoRoot",
            str(tmp_path),
            "-TrackedPaths",
            str(license_path),
            "-BlockedMarkers",
            marker,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "privacy scan found blocked metadata" in (
        completed.stdout + completed.stderr
    ).casefold()

    allowed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-File",
            str(PRIVACY_SCANNER),
            "-RepoRoot",
            str(tmp_path),
            "-TrackedPaths",
            str(license_path),
            "-BlockedMarkers",
            marker,
            "-AllowedPathLines",
            f"LICENSE::Copyright (c) 2026 {marker}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr


def test_release_gate_scans_before_generation_and_compares_temp_dashboard():
    release_gate = _text("scripts/verify-public-release.ps1").casefold()

    privacy_scan = release_gate.index("verify-tracked-privacy.ps1")
    dashboard_build = release_gate.index('"build-dashboard"')
    assert privacy_scan < dashboard_build
    assert "$generateddashboard" in release_gate
    assert '"--output", "dashboard/public/index.html"' not in release_gate
    assert '"diff", "--exit-code", "--", "dashboard/public/index.html"' in release_gate

def test_docs_name_exact_optional_sync_fields_and_third_party_responsibility():
    external = _text("docs/external-data-contracts.md").casefold()
    privacy = _text("docs/privacy-and-data.md").casefold()
    source_policy = _text("SOURCE_POLICY.md").casefold()
    combined = external + privacy + source_policy

    for field in (
        "`contract_version`",
        "`statuses`",
        "`legacy_keys`",
        "stable job id",
        "status value",
    ):
        assert field in external
    for statement in (
        "cloudflare is a third party",
        "provider retention",
        "training",
        "sharing",
        "deletion",
        "operator is responsible",
        "not legal advice",
    ):
        assert statement in combined
    assert "legal approval" not in combined


def test_source_policy_distinguishes_legacy_and_compact_ai_disclosures():
    policy = _text("SOURCE_POLICY.md").casefold()

    assert "legacy `score`/`run --ai-command` protocol" in policy
    assert "structured skills, preferences, and company boundaries" in policy
    assert "compact `review --provider-command` protocol" in policy
    assert "does not send profile-derived preferences" in policy


def test_release_gate_uses_synthetic_dashboard_privacy_marker():
    release_gate = _text("scripts/verify-public-release.ps1").casefold()

    assert "private-company-" in release_gate
    assert "sentinel.invalid" in release_gate
    assert "blocked synthetic privacy marker" in release_gate


def test_source_policy_is_an_exact_builtin_allowlist_and_scraper_denylist():
    policy = _text("SOURCE_POLICY.md").casefold()
    catalog = _text("catalog/sources.yaml").casefold()
    adapters = _text("job_radar/adapters.py").casefold()

    for source in ("greenhouse", "lever", "ashby"):
        assert source in policy
        assert source in catalog
        assert source in adapters
    for excluded in ("linkedin", "indeed", "google jobs", "104", "jobspy"):
        assert excluded in policy
        assert f"no bundled `{excluded}`" in policy or "does not bundle" in policy
    assert "cookie warming" in policy
    assert "session reuse" in policy
    assert "tls verification downgrade" in policy


def test_operator_guide_covers_each_public_workflow_and_semantics():
    guide = _text("docs/operator-guide.md").casefold()

    for topic in (
        "initialize",
        "application",
        "interview",
        "follow-up",
        "funnel",
        "lifecycle",
        "first_seen",
        "last_seen",
        "stale",
        "expired",
        "atomic",
        "best-effort",
        "incomplete",
        "hard exclusion",
        "fast review",
        "strong review",
        "versioned cache",
        "rejected sample",
        "dashboard",
        "cloudflare",
        "export",
        "delete",
    ):
        assert topic in guide
    assert "atomic mode does not replace the last complete output" in guide
    assert "best-effort" in guide and "partial" in guide
    assert "ai output is advisory" in guide


def test_job_tracker_skill_migration_and_archive_order_are_documented():
    migration = _text("docs/migration-from-job-tracker-skill.md").casefold()

    for statement in (
        "job-tracker-skill",
        "job-radar-starter",
        "applications",
        "events",
        "follow-up",
        "funnel",
        "rejection",
        "single developer",
        "archive",
    ):
        assert statement in migration
    assert "merge job-radar-starter before archiving" in migration
    assert "no automatic migration of personal data" in migration


@pytest.mark.parametrize(
    "relative",
    [
        "docs/schemas/dashboard-view-model.schema.json",
        "docs/schemas/tracking-export.schema.json",
        "docs/schemas/ai-review-request.schema.json",
        "docs/schemas/ai-review-response.schema.json",
        "docs/schemas/cloudflare-sync.schema.json",
        "examples/dashboard-view-model.example.json",
        "examples/ai-review-request.example.json",
        "examples/ai-review-response.example.json",
        "examples/cloudflare-sync.example.json",
    ],
)
def test_versioned_schemas_and_examples_exist(relative: str):
    document = _json(relative)
    assert document.get("contract_version") == 1 or document.get(
        "$id"
    ), f"{relative} must be versioned"


def test_published_schema_bounds_match_runtime_contracts():
    request = _json("docs/schemas/ai-review-request.schema.json")["properties"]
    response = _json("docs/schemas/ai-review-response.schema.json")["properties"]
    sync = _json("docs/schemas/cloudflare-sync.schema.json")["properties"]

    assert request["stable_id"]["maxLength"] == 256
    assert request["title"]["maxLength"] == 240
    assert request["company"]["maxLength"] == 240
    assert request["country"]["maxLength"] == 80
    assert request["jd_evidence"]["maxItems"] == 8
    assert request["jd_evidence"]["items"]["maxLength"] == 500
    assert response["stable_id"]["maxLength"] == 256
    assert response["reason_codes"]["maxItems"] == 12
    assert response["reason_codes"]["items"]["maxLength"] == 80
    assert response["summary"]["maxLength"] == 500
    assert sync["statuses"]["maxProperties"] == 500
    assert sync["statuses"]["propertyNames"]["maxLength"] == 128
    assert sync["legacy_keys"]["maxItems"] == 500
    assert sync["legacy_keys"]["items"]["maxLength"] == 512


def test_current_spec_and_all_tracked_release_text_are_generic():
    current = _text(".spec/current.md").casefold()
    synthetic_owner = "private-" + "owner"
    for private_marker in (
        synthetic_owner + "-handle",
        synthetic_owner + "-site.invalid",
        rf"c:\users\{synthetic_owner}",
        "private repository",
        "personal site",
    ):
        assert private_marker not in current

    script = _text("scripts/verify-public-release.ps1").casefold()
    scanner = _text("scripts/verify-tracked-privacy.ps1").casefold()
    assert "verify-tracked-privacy.ps1" in script
    assert "git ls-files" in scanner
    assert "utf8encoding" in scanner
    assert "unicodeencoding" in scanner
    assert "$ownername" not in scanner
    assert "allowedpathlines" in scanner
    assert '$allowed.contains($relative + "::" + $trimmed)' in scanner
    assert "test-trackedtextfile" not in script + scanner
    assert "privacytargets = @(" not in script + scanner
    assert ":(exclude)tests" not in script + scanner
    assert "tests/*" not in script + scanner


def test_all_public_examples_use_only_invented_names_and_public_placeholder_urls():
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "examples").glob("*"))
        if path.is_file()
    ).casefold()

    for real_name in (
        "openai",
        "gitlab",
        "paypay",
        "faire",
        "monzo",
        "airwallex",
        "cohere",
        "private-" + "owner",
    ):
        assert real_name not in text
    assert "example.com" in text or "example.test" in text
    assert "private" not in text


def test_existing_cli_help_preserves_all_public_commands(capsys):
    with pytest.raises(SystemExit) as error:
        main(["--help"])
    assert error.value.code == 0
    output = capsys.readouterr().out
    for command in (
        "doctor",
        "catalog",
        "scan",
        "score",
        "run",
        "schedule",
        "build-dashboard",
        "review",
    ):
        assert command in output


def test_release_gate_script_contains_every_required_local_evidence_command():
    script = _text("scripts/verify-public-release.ps1").casefold()

    for command in (
        "python -m pytest -q",
        "build-dashboard",
        "npm audit",
        "npm test",
        "npm run test:browser",
        "wrangler deploy --dry-run",
        "git diff --check",
        "gitleaks",
        "privacy",
        "generated",
    ):
        assert command in script
    assert "continueonerror" not in script
    assert "|| true" not in script
    assert "deploy --dry-run" in script
    assert "wrangler deploy" not in script.replace("wrangler deploy --dry-run", "")
    assert "test_release_gate_covers_run_export_and_delete" in script


def test_hosted_pr_ci_executes_the_complete_public_release_gate():
    workflow = _text(".github/workflows/public-release.yml").casefold()

    assert "pull_request:" in workflow
    assert "windows-latest" in workflow
    assert "scripts/verify-public-release.ps1" in workflow
    assert "npm ci" in workflow
    assert "playwright install chromium" in workflow
    assert "gitleaks" in workflow
    assert "deploy --dry-run" in _text("scripts/verify-public-release.ps1").casefold()
    assert "continue-on-error" not in workflow
    assert "pull_request_target" not in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "requirements-ci.lock" in workflow
    assert "--require-hashes" in workflow
    assert '-e ".[dev]"' not in workflow


def test_browser_fixture_python_launcher_supports_hosted_and_local_runtimes():
    browser = _text("tests/browser/dashboard.spec.mjs")

    assert "process.env.PYTHON" in browser
    assert "existsSync" in browser
    assert "process.platform" in browser
    assert "'python.exe'" in browser
    assert "PYTHONPATH" in browser
    assert "const python = path.join(root, '.venv', 'Scripts', 'python.exe')" not in browser


def test_python_ci_dependencies_are_exactly_locked_with_hashes():
    lock = _text("requirements-ci.lock")
    requirement_lines = [
        line.strip()
        for line in lock.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert requirement_lines
    assert all("==" in line or line.startswith("--hash=") for line in requirement_lines)
    assert "pyyaml==" in lock.casefold()
    assert "pytest==" in lock.casefold()
    assert lock.count("--hash=sha256:") >= 4


def test_hosted_editable_install_uses_the_hash_locked_build_backend():
    workflow = _text(".github/workflows/public-release.yml").casefold()
    project = tomllib.loads(_text("pyproject.toml"))
    lock = _text("requirements-ci.lock").casefold()

    assert "--no-build-isolation --no-deps -e ." in workflow
    build_requires = project["build-system"]["requires"]
    assert build_requires == ["setuptools==84.1.0"]
    assert project["build-system"]["build-backend"] == "setuptools.build_meta"
    assert re.search(
        r"setuptools==84\.1\.0\s*\\\s*\n\s*--hash=sha256:[0-9a-f]{64}",
        lock,
    )


def test_release_checklist_does_not_claim_a_production_deployment():
    checklist = _text("docs/release-checklist.md").casefold()

    for evidence in (
        "clean checkout",
        "full python suite",
        "dashboard build",
        "worker tests",
        "browser",
        "secret scan",
        "dependency audit",
        "generated artifact",
        "manual review",
    ):
        assert evidence in checklist
    assert "production deployed" not in checklist
    assert "cloudflare production smoke passed" not in checklist
