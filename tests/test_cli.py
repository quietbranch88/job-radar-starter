import json
import shlex
from io import StringIO
from pathlib import Path

from job_radar import cli
from job_radar.cli import main


def test_build_dashboard_command_reads_normalized_json(tmp_path: Path):
    jobs = tmp_path / "jobs.json"
    output = tmp_path / "public" / "index.html"
    jobs.write_text(
        json.dumps([
            {
                "source": "greenhouse",
                "external_id": "1",
                "company": "Example",
                "title": "Backend Engineer",
                "location": "Toronto",
                "url": "https://example.com/jobs/1",
                "published_at": "2026-07-13T00:00:00Z"
            }
        ]),
        encoding="utf-8",
    )

    assert main(["build-dashboard", "--jobs", str(jobs), "--output", str(output)]) == 0
    assert "Backend Engineer" in output.read_text(encoding="utf-8")


def test_help_exposes_the_complete_daily_pipeline():
    help_text = cli._parser().format_help()

    for command in ("doctor", "init", "catalog", "scan", "score", "run", "schedule", "build-dashboard"):
        assert command in help_text


def test_schedule_render_is_safe_and_does_not_install_anything(tmp_path: Path):
    cron = cli.render_schedule(
        platform="cron",
        daily_at="08:15",
        project_dir=tmp_path,
        executable=tmp_path / ".venv" / "bin" / "job-radar",
    )
    windows = cli.render_schedule(
        platform="windows",
        daily_at="08:15",
        project_dir=tmp_path,
        executable=tmp_path / ".venv" / "Scripts" / "job-radar.exe",
    )

    assert cron.startswith("15 8 * * *")
    assert "job-radar" in cron
    assert "New-ScheduledTaskAction" in windows
    assert "08:15" in windows
    assert "Register-ScheduledTask" in windows


def _write_ready_catalog(path: Path) -> None:
    path.write_text(
        """
countries:
  CA: {name: Canada, sources: [greenhouse]}
sources:
  greenhouse:
    kind: official_api
    terms_url: https://developer.greenhouse.io/job-board.html
    enabled: true
companies:
  Example:
    countries: [CA]
    source: greenhouse
    board: example
""".strip(),
        encoding="utf-8",
    )


def _write_profile(path: Path) -> None:
    path.write_text(
        """
profile: {skills: [Go]}
preferences:
  countries: [CA]
  roles: [backend]
  tracks: [backend]
  visa_required: false
matching: {minimum_score: 50, must_have: [backend]}
""".strip(),
        encoding="utf-8",
    )


def test_init_copies_local_files_once_and_prints_the_next_command(tmp_path: Path):
    profile_example = tmp_path / "profile.example.yaml"
    catalog_example = tmp_path / "sources.example.yaml"
    profile = tmp_path / "user-data" / "profile.yaml"
    catalog = tmp_path / "user-data" / "sources.yaml"
    _write_profile(profile_example)
    catalog_example.write_text(
        """
countries:
  CA: {name: Canada, sources: [greenhouse]}
sources:
  greenhouse:
    kind: official_api
    terms_url: https://developer.greenhouse.io/job-board.html
    enabled: true
companies:
  Example:
    countries: [CA]
    source: greenhouse
    board: replace-with-your-board-token
""".strip(),
        encoding="utf-8",
    )
    args = [
        "init",
        "--profile",
        str(profile),
        "--catalog",
        str(catalog),
        "--profile-example",
        str(profile_example),
        "--catalog-example",
        str(catalog_example),
    ]

    stdout = StringIO()
    assert main(args, stdout=stdout) == 0
    text = stdout.getvalue()
    assert "Created the local files" in text
    assert "replace-with-your-board" in text
    assert f"job-radar doctor --catalog {shlex.quote(str(catalog))}" in text
    assert profile.read_text(encoding="utf-8") == profile_example.read_text(encoding="utf-8")

    profile.write_text("profile: {skills: [Kept]}\n", encoding="utf-8")
    stdout = StringIO()
    assert main(["--json", *args], stdout=stdout) == 0
    report = json.loads(stdout.getvalue())
    assert report["ok"] is True
    assert report["profile"]["created"] is False
    assert report["catalog"]["created"] is False
    assert report["catalog"]["placeholders"] == ["Example"]
    assert "Kept" in profile.read_text(encoding="utf-8")


def test_doctor_explains_a_missing_profile_and_placeholder_boards(tmp_path: Path):
    catalog = tmp_path / "catalog" / "sources.yaml"
    catalog.parent.mkdir()
    catalog.write_text(
        """
countries:
  CA: {name: Canada, sources: [greenhouse]}
sources:
  greenhouse:
    kind: official_api
    terms_url: https://developer.greenhouse.io/job-board.html
    enabled: true
companies:
  Northstar:
    countries: [CA]
    source: greenhouse
    board: replace-with-your-board-token
  Cedarline:
    countries: [CA]
    source: greenhouse
    board: replace-with-your-board-name
""".strip(),
        encoding="utf-8",
    )
    stdout = StringIO()
    status = main(
        [
            "doctor",
            "--catalog",
            str(catalog),
            "--profile",
            str(tmp_path / "missing-profile.yaml"),
            "--jobs-output",
            str(tmp_path / "scans" / "latest.json"),
            "--dashboard-output",
            str(tmp_path / "dashboard" / "index.html"),
        ],
        stdout=stdout,
    )

    text = stdout.getvalue()
    assert status == 1
    assert "Not ready to scan yet." in text
    assert "2 placeholder boards" in text
    assert "job-radar init" in text
    assert "Preferences file is missing." in text
    assert "boards.greenhouse.io/BOARD" in text

    profile = tmp_path / "profile.yaml"
    _write_profile(profile)
    _write_ready_catalog(catalog)
    stdout = StringIO()
    status = main(
        [
            "--json",
            "doctor",
            "--catalog",
            str(catalog),
            "--profile",
            str(profile),
            "--jobs-output",
            str(tmp_path / "scans" / "latest.json"),
            "--dashboard-output",
            str(tmp_path / "dashboard" / "index.html"),
        ],
        stdout=stdout,
    )
    report = json.loads(stdout.getvalue())
    assert status == 0
    assert report["ok"] is True
    assert report["catalog"]["placeholders"] == []


def test_run_command_emits_machine_readable_counts_and_paths(tmp_path: Path):
    catalog = tmp_path / "catalog.yaml"
    profile = tmp_path / "profile.yaml"
    jobs = tmp_path / "scans" / "latest.json"
    dashboard = tmp_path / "dashboard" / "index.html"
    catalog.write_text(
        """
countries:
  CA: {name: Canada, sources: [greenhouse]}
sources:
  greenhouse:
    kind: official_api
    terms_url: https://developer.greenhouse.io/job-board.html
    enabled: true
companies:
  Example:
    countries: [CA]
    source: greenhouse
    board: example
""".strip(),
        encoding="utf-8",
    )
    profile.write_text(
        """
profile: {skills: [Go]}
preferences:
  countries: [CA]
  roles: [backend]
  tracks: [backend]
  visa_required: false
matching: {minimum_score: 50, must_have: [backend]}
""".strip(),
        encoding="utf-8",
    )

    def fake_get_json(url):
        return {
            "jobs": [
                {
                    "id": 1,
                    "title": "Go Backend Engineer",
                    "absolute_url": "https://example.com/jobs/1",
                    "location": {"name": "Canada"},
                }
            ]
        }

    stdout = StringIO()
    status = main(
        [
            "--json",
            "run",
            "--catalog",
            str(catalog),
            "--profile",
            str(profile),
            "--jobs-output",
            str(jobs),
            "--dashboard-output",
            str(dashboard),
        ],
        get_json=fake_get_json,
        stdout=stdout,
    )

    report = json.loads(stdout.getvalue())
    assert status == 0
    assert report["ok"] is True
    assert report["counts"] == {"scanned": 1, "published": 1}
    assert report["jobs_output"] == str(jobs.resolve())
    assert report["dashboard_output"] == str(dashboard.resolve())


def test_run_timeouts_stay_options_when_they_precede_the_provider_command():
    args = cli._parser().parse_args(
        [
            "run",
            "--per-call-timeout",
            "9",
            "--total-review-deadline",
            "11",
            "--max-fast-calls",
            "3",
            "--provider-command",
            "echo",
            "hi",
        ]
    )
    assert args.per_call_timeout == 9.0
    assert args.total_review_deadline == 11.0
    assert args.max_fast_calls == 3
    assert args.provider_command == ["echo", "hi"]
    run_parser = cli._parser()._subparsers._group_actions[0].choices["run"]
    help_text = run_parser.format_help()
    assert help_text.index("--per-call-timeout") < help_text.index("--provider-command")


def test_doctor_checks_a_provider_command_separately(tmp_path: Path):
    catalog = tmp_path / "catalog.yaml"
    profile = tmp_path / "profile.yaml"
    _write_ready_catalog(catalog)
    _write_profile(profile)
    stdout = StringIO()
    status = main(
        [
            "--json",
            "doctor",
            "--catalog",
            str(catalog),
            "--profile",
            str(profile),
            "--jobs-output",
            str(tmp_path / "scans" / "latest.json"),
            "--dashboard-output",
            str(tmp_path / "dashboard" / "index.html"),
            "--provider-command",
            "job-radar-missing-provider",
        ],
        stdout=stdout,
    )
    report = json.loads(stdout.getvalue())
    assert status == 1
    assert report["ok"] is False
    assert report["provider"]["ready"] is False
    assert report["ai"]["mode"] == "local_rules"


def test_atomic_failure_names_the_board_and_leaves_the_jobs_file(tmp_path: Path):
    from job_radar.source_orchestration import AtomicSourceScanFailed

    catalog = tmp_path / "catalog.yaml"
    _write_ready_catalog(catalog)
    jobs = tmp_path / "scans" / "latest.json"
    jobs.parent.mkdir()
    jobs.write_text('{"keep": true}\n', encoding="utf-8")

    def scan_runner(_catalog, _transport, **_kwargs):
        raise AtomicSourceScanFailed(
            {
                "contract_version": 1,
                "mode": "atomic",
                "incomplete": True,
                "jobs": [],
                "failures": [
                    {
                        "source": "ashby",
                        "company": "Example",
                        "category": "http_403",
                        "message": "forbidden",
                    }
                ],
            }
        )

    stderr = StringIO()
    status = main(
        ["scan", "--catalog", str(catalog), "--output", str(jobs), "--mode", "atomic"],
        scan_runner=scan_runner,
        stdout=StringIO(),
        stderr=stderr,
    )
    text = stderr.getvalue()
    assert status == 2
    assert "Example / ashby / http_403" in text
    assert f"Jobs file left unchanged: {jobs}" in text
    assert jobs.read_text(encoding="utf-8") == '{"keep": true}\n'

    stdout = StringIO()
    status = main(
        ["--json", "scan", "--catalog", str(catalog), "--output", str(jobs)],
        scan_runner=scan_runner,
        stdout=stdout,
        stderr=StringIO(),
    )
    report = json.loads(stdout.getvalue())
    assert status == 2
    assert report["failures"][0]["category"] == "http_403"
