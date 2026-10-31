import ast
import re
from pathlib import Path


def test_gitleaks_runs_for_pushes_and_pull_requests():
    workflow = Path(".github/workflows/gitleaks.yml")

    assert workflow.exists()
    text = workflow.read_text(encoding="utf-8")
    assert "push:" in text
    assert "pull_request:" in text
    assert "gitleaks/gitleaks-action" in text
    assert re.search(r"actions/checkout@[0-9a-f]{40}", text)
    assert re.search(r"gitleaks/gitleaks-action@[0-9a-f]{40}", text)


def test_hosted_release_installs_and_smokes_the_local_console_script():
    workflow = Path(".github/workflows/public-release.yml").read_text(
        encoding="utf-8"
    )

    assert "pip install --no-build-isolation --no-deps -e ." in workflow
    assert "job-radar --help" in workflow


def test_application_layer_does_not_import_or_render_dashboard():
    source = Path("job_radar/application.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert "job_radar.dashboard" not in imported_modules
    assert "render_dashboard(" not in source


def test_application_workflows_do_not_import_concrete_adapter_job_model():
    for relative in (
        "job_radar/application.py",
        "job_radar/public_workflow.py",
    ):
        tree = ast.parse(Path(relative).read_text(encoding="utf-8"))
        concrete_job_imports = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module == "job_radar.adapters"
            and any(alias.name == "Job" for alias in node.names)
        ]
        assert concrete_job_imports == [], relative


def test_application_layer_has_no_concrete_io_or_process_dependencies():
    tree = ast.parse(Path("job_radar/application.py").read_text(encoding="utf-8"))
    forbidden = {
        "job_radar.adapters",
        "job_radar.bounded_process",
        "pathlib",
        "subprocess",
    }
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert imported.isdisjoint(forbidden), sorted(imported & forbidden)


def test_application_layer_does_not_write_files_or_select_concrete_fetchers():
    source = Path("job_radar/application.py").read_text(encoding="utf-8")

    for forbidden in (
        "_FETCHERS",
        "write_text(",
        ".replace(output)",
        "run_bounded_process(",
        "subprocess.run",
    ):
        assert forbidden not in source


def test_public_workflow_depends_on_injected_ports_not_concrete_adapters():
    tree = ast.parse(Path("job_radar/public_workflow.py").read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert imported.isdisjoint(
        {
            "job_radar.adapters",
            "job_radar.data.tracking_store",
            "job_radar.dashboard",
            "job_radar.job_output",
            "job_radar.legacy_command_adapter",
            "job_radar.legacy_scoring",
        }
    )


def test_concrete_adapter_modules_do_not_import_application_workflows_upward():
    for relative in (
        "job_radar/adapters.py",
        "job_radar/bounded_process.py",
        "job_radar/job_output.py",
        "job_radar/legacy_command_adapter.py",
    ):
        tree = ast.parse(Path(relative).read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert "job_radar.application" not in imported, relative
