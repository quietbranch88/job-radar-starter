# Job Radar Starter

> A local-first job radar for one developer.

Find better roles from official ATS boards, score them transparently on your machine, and review them in a private dashboard.

[First time here](#first-time-here) · [Privacy](#privacy) · [Docs](#docs)

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

## First time here

You do not need an account. Job Radar runs on your computer, reads the public job boards you name, scores them with your rules, and opens a private page in your browser. Requires Python 3.11+.

### 1. See the page before you configure anything

macOS and Linux:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
job-radar build-dashboard --jobs examples/jobs.example.json --output dashboard/public/index.html
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
job-radar build-dashboard --jobs examples\jobs.example.json --output dashboard\public\index.html
```

Open `dashboard/public/index.html`. The roles on that page are examples. The "New here?" note on the page explains the buttons. Interested, Applied, and Hide stay in the browser. Applied does not submit an application.

### 2. Create your local files

```bash
job-radar init
```

This copies two files and leaves them alone if you run it again:

- `user-data/profile.yaml` — countries, roles, and skills
- `user-data/sources.yaml` — which company job boards to read

Both stay on your machine. Do not commit them.

### 3. Name the companies you want

Open `user-data/sources.yaml`. Every company starts with a board value such as `replace-with-your-board-token`. A scan needs the board name from that company's public careers URL:

- Greenhouse: `https://boards.greenhouse.io/BOARD` → `BOARD`
- Lever: `https://jobs.lever.co/BOARD` → `BOARD`
- Ashby: `https://jobs.ashbyhq.com/BOARD` → `BOARD`

Delete companies you do not want. Keep the `countries` and `sources` sections.

Open `user-data/profile.yaml` and change countries, roles, and skills. `minimum_score` hides lower matches from the dashboard. The scan still saves them.

The local profile may point to a resume, but built-in components do not open or upload it.

### 4. Scan, then open the same page

```bash
job-radar doctor --catalog user-data/sources.yaml
job-radar run --catalog user-data/sources.yaml
```

`doctor` says what is still missing and the next command. `run` writes `scans/latest.json` and rebuilds `dashboard/public/index.html`. Open that file again.

Scoring stays on your machine. You do not need an AI command. Add `--json` when a script or agent needs machine-readable output.

## Agents and scripts

Use the same CLI and JSON contracts with Claude, Codex, other agents, or normal scripts. Any agent or script can run readiness checks, scan official boards, score jobs, and build the dashboard; no agent subscription is required.

The optional [agent workflow adapter](.codex/skills/job-radar/SKILL.md) documents the CLI workflow. AI review is opt-in through a provider command you configure. See the [operator guide](docs/operator-guide.md) for command examples and the [external data contracts](docs/external-data-contracts.md) for the fields sent to that command.

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
Daily `run --resume-sha256 HASH` and saved `build-dashboard` now project those
receipts as separate operator-approved/rejected/pending states. No resume file is
opened and no application is submitted. See the operator guide for version and
time applicability.
Opt-in `sheet-sync` uses a durable, readback-verified outbox on a dedicated owned
Google Sheets tab, not an existing master-sheet migration. Both are disabled
unless explicitly invoked; see the [operator guide](docs/operator-guide.md#8-diagnose-configured-sources)
and [privacy rules](docs/privacy-and-data.md).

## License

MIT
