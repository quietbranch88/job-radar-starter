# Job Radar Starter

> A local-first job radar for one developer.

Find better roles from official ATS boards, score them transparently on your machine, and review them in a private dashboard.

[Quick start](#quick-start) · [Privacy](#privacy) · [Docs](#docs)

## Why this exists

Job search gets noisy when listings, preferences, and decisions live in separate places. Job Radar turns them into one local workflow:

public job boards → normalized jobs → local scoring → review dashboard → decisions

## Highlights

- Official Greenhouse, Lever, and Ashby adapters
- Local rules by default; AI scoring is optional
- Ranked cards, filters, swipe review, saved matches, and keyboard controls
- Local tracking for interested, applied, hidden, and expired jobs
- Atomic scans, bounded source concurrency, and JSON contracts for agents and scripts
- No automatic applications, telemetry, or vendor lock-in

## Agents and scripts

Use the same CLI and JSON contracts with Claude, Codex, other agents, or normal scripts. Any agent or script can run readiness checks, scan official boards, score jobs, and build the dashboard; no agent subscription is required.

The optional [agent workflow adapter](.codex/skills/job-radar/SKILL.md) documents the CLI workflow. AI review is opt-in through a provider command you configure. See the [operator guide](docs/operator-guide.md) for command examples and the [external data contracts](docs/external-data-contracts.md) for the fields sent to that command.

## Quick start

Requires Python 3.11+.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
New-Item -ItemType Directory -Force user-data | Out-Null
Copy-Item config.example\profile.yaml user-data\profile.yaml
job-radar --json doctor
job-radar --json run
python -m http.server 8000 --directory dashboard\public
```

Open http://localhost:8000.

Preview with example data:

```powershell
job-radar build-dashboard --jobs examples\jobs.example.json --output dashboard\public\index.html
```

## Configure

Copy config.example/profile.yaml to user-data/profile.yaml, then set your countries, roles, skills, preferred companies, exclusions, and minimum score.

The local profile may point to a resume, but built-in components do not open or upload it.

## Privacy

Your resume, preferences, application history, scan output, credentials, and browser status stay local by default.

Applied is a browser marker, not proof of submission. The [local confirmation workflow](docs/operator-guide.md#confirm-a-submission-locally) records a verified application and rebuilds its metrics offline; no application is submitted by Job Radar.

Do not commit real resumes, interview notes, salary records, API keys, or scan results.

Optional AI scoring receives structured preferences and public job fields. It does not receive resume contents, resume paths, prior notes, risks, or application state. If your command calls a hosted model, review that provider's retention and deletion policy.

## Repository map

- job_radar/ — Python CLI, adapters, scoring, tracking, and scheduling
- catalog/ — public source and company-board catalog
- dashboard/public/ — generated dependency-free dashboard
- config.example/ — safe configuration templates
- examples/ — demo data and fixtures
- docs/ — operator and contract documentation
- optional-sync/cloudflare/ — optional self-owned sync template
- .codex/skills/job-radar/ — optional agent workflow adapter

## Docs

- [Operator guide](docs/operator-guide.md): scanning, scoring, AI review, tracking, dashboard, and runtime boundaries
- [Example configuration](config.example/profile.yaml): local profile and source settings
- [Privacy and local data](docs/privacy-and-data.md): retention, export, deletion, and external processing
- [Source policy](SOURCE_POLICY.md): official ATS allowlist and excluded automation
- [External data contracts](docs/external-data-contracts.md): compact AI review, legacy scoring, and sync payloads
- [Agent workflow and scheduling examples](.codex/skills/job-radar/SKILL.md)
- [Optional Cloudflare status sync](optional-sync/cloudflare/README.md)
- [Migration from job-tracker-skill](docs/migration-from-job-tracker-skill.md)
- [Release checklist](docs/release-checklist.md)

## Boundaries

Use official public job-board endpoints. Respect current terms, rate limits, authentication boundaries, privacy obligations, and applicable law. Verify job availability, visa support, compensation, and AI explanations on the official posting.

This project is independent and is not affiliated with any job board, ATS provider, employer, or government agency.

## Development

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## Recovery and local evidence

The local workflow preserves all scored observations while filtering the
Dashboard/review projection. Opt-in company-level `--checkpoint` / `--resume`,
offline `build-dashboard --profile`, and per-attempt stage receipts are described
in the [operator guide](docs/operator-guide.md#6-preserve-observations-and-resume-a-company-walk).
These are local capabilities, not automatic application submission or deployment.

Optional source-health receipts distinguish cached observations from actual
fetches and safe failure categories. Separate `review-receipt` commands record
explicit job/JD/resume-hash attestations; provider completion is never approval.
Opt-in `sheet-sync` uses a durable, readback-verified outbox on a dedicated owned
Google Sheets tab, not an existing master-sheet migration. Both are disabled
unless explicitly invoked; see the [operator guide](docs/operator-guide.md#8-diagnose-configured-sources)
and [privacy rules](docs/privacy-and-data.md).

## License

MIT
