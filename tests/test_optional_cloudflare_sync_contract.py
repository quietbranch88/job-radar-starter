from __future__ import annotations

import ast
import json
import re
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "optional-sync" / "cloudflare"


def _required(path: Path) -> str:
    assert path.is_file(), f"missing optional Cloudflare template file: {path}"
    return path.read_text(encoding="utf-8")


def _jsonc(path: Path) -> dict:
    raw = _required(path)
    without_comments = re.sub(r"/\*.*?\*/|//[^\n]*", "", raw, flags=re.S)
    return json.loads(without_comments)


def test_optional_template_is_isolated_and_has_all_operator_files():
    for relative in (
        "src/index.ts",
        "wrangler.jsonc",
        "package.json",
        "vitest.config.ts",
        "README.md",
        "SECURITY.md",
    ):
        _required(TEMPLATE / relative)

    root_package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    dependencies = {
        **root_package.get("dependencies", {}),
        **root_package.get("devDependencies", {}),
    }
    assert not any(
        name in dependencies
        for name in (
            "wrangler",
            "@cloudflare/vitest-plugin",
            "@cloudflare/vitest-pool-workers",
            "@cloudflare/workers-types",
        )
    )


def test_wrangler_config_uses_one_sqlite_durable_object_without_production_ids():
    config = _jsonc(TEMPLATE / "wrangler.jsonc")

    assert config["name"] == "job-radar-status-sync"
    assert config["main"] == "src/index.ts"
    assert re.fullmatch(r"20\d\d-\d\d-\d\d", config["compatibility_date"])
    assert config["durable_objects"] == {
        "bindings": [
            {
                "name": "STATUS_COORDINATOR",
                "class_name": "StatusCoordinator",
            }
        ]
    }
    assert config["migrations"] == [
        {
            "tag": "v1",
            "new_sqlite_classes": ["StatusCoordinator"],
        }
    ]
    assert "kv_namespaces" not in config
    serialized = json.dumps(config).casefold()
    synthetic_owner = "private-" + "owner"
    for forbidden in (
        "account_id",
        "preview_id",
        '"id"',
        "route",
        "custom_domain",
        "workers_dev",
        "remote",
        synthetic_owner,
        "private-deployment-" + "sentinel",
    ):
        assert forbidden not in serialized


def test_optional_package_pins_verified_worker_test_dependencies():
    package = json.loads(_required(TEMPLATE / "package.json"))
    assert package["private"] is True
    assert package["scripts"]["test"] == "vitest run"
    assert package["scripts"]["dev"] == "wrangler dev --local"
    assert package["scripts"]["deploy"] == "wrangler deploy"
    assert package["engines"] == {"node": "^22.12.0 || >=24.0.0"}
    assert package["devDependencies"] == {
        "@cloudflare/vitest-plugin": "1.4.7",
        "@cloudflare/workers-types": "5.20261019.1",
        "typescript": "7.0.2",
        "vitest": "4.1.12",
        "wrangler": "4.152.0",
    }
    # Explicit 2026-10-24 decision permits only these two exact advisory
    # patches. Coupled Worker parents above remain pinned and independently tested.
    assert package['overrides'] == {
        'undici': '7.30.1',
        'zod': '4.6.6',
    }
    config = _required(TEMPLATE / "vitest.config.ts")
    assert 'from "@cloudflare/vitest-plugin"' in config
    assert "@cloudflare/vitest-pool-workers" not in config


def test_worker_source_contains_bounded_single_owner_security_contract():
    source = _required(TEMPLATE / "src" / "index.ts")

    for required in (
        "owner:statuses",
        "Bearer ",
        "SYNC_TOKEN",
        "STATUS_COORDINATOR",
        "StatusCoordinator",
        "DurableObject",
        "transactionSync",
        "stored_state_limit_exceeded",
        "private, no-store",
        "application/json",
        "65536",
        "500",
        "interested",
        "applied",
        "skip",
        "dead",
        "legacy_keys",
        "timingSafeEqual",
    ):
        assert required in source
    for forbidden in (
        "user_id",
        "account_id",
        "Access-Control-Allow-Origin",
        "console.log",
        "error.stack",
        "error.message",
    ):
        assert forbidden not in source


def test_documentation_states_retention_deletion_abuse_and_no_saas_claim():
    readme = _required(TEMPLATE / "README.md").casefold()
    security = _required(TEMPLATE / "SECURITY.md").casefold()
    combined = readme + security

    for statement in (
        "optional",
        "single developer",
        "local-only",
        "retention",
        "delete",
        "third party",
        "bearer",
        "64 kib",
        "500",
        "rate",
        "abuse",
        "durable object",
        "sqlite",
        "concurrent",
    ):
        assert statement in combined
    assert "production-ready saas" not in combined
    assert "legal approval" not in combined


def test_main_python_runtime_has_no_cloudflare_dependency():
    # Vendor disclosure in an HTML string is not a Python SDK dependency.
    # Check executable imports and declared runtime dependencies instead of prose.
    forbidden = {"cloudflare", "wrangler", "miniflare", "pycloudflare", "cloudflare_api"}
    for path in (ROOT / "job_radar").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            assert not {
                module.split(".")[0].casefold() for module in modules
            } & forbidden, path
            if path.name == "dashboard_sync.py":
                # The installer embeds browser code; it has no Python network path.
                assert all(module == "__future__" for module in modules), path

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = project["project"].get("dependencies", [])
    for dependency in dependencies:
        name = re.split(r"[\s\[<>=!~;]", dependency, maxsplit=1)[0]
        assert name.casefold().replace("-", "_") not in forbidden
