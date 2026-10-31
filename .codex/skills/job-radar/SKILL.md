---
name: job-radar
description: Operate the local-first Job Radar CLI for readiness checks, official Greenhouse, Lever, and Ashby scans, deterministic or external-command scoring, dashboard builds, and safe schedule rendering. Use when a developer asks an AI agent to configure, scan, score, diagnose, or automate this repository's private single-user job workflow.
---

# Job Radar

Run commands from the repository root. Preserve local privacy and source-policy boundaries.

## Workflow

1. Run `job-radar --json init` when `user-data/profile.yaml` or `user-data/sources.yaml` is missing. It copies the example profile and catalog and does not overwrite existing files. Never put a real profile or company list in a tracked path.
2. Run `job-radar --json doctor --catalog user-data/sources.yaml` and resolve reported catalog, placeholder-board, profile, output-path, or AI-executable failures. Placeholder boards use the `replace-with-your-board` prefix and must be replaced with a public Greenhouse, Lever, or Ashby board name, or removed, before a live scan.
3. Inspect configured public sources with `job-radar --json catalog list --catalog user-data/sources.yaml` when changing country or company coverage.
4. Run `job-radar --json run --catalog user-data/sources.yaml` to scan official APIs, apply local rules, write `scans/latest.json`, and rebuild `dashboard/public/index.html`.
5. Report scanned and published counts plus both output paths. Open the generated dashboard when UI verification is part of the request.

Use `job-radar --json scan` and `job-radar --json score` separately when diagnosing a pipeline stage. Keep `--ai-command <executable> [args...]` last. Only use an AI command explicitly supplied or approved by the user.

## Safety boundaries

- Do not open or transmit `profile.resume_path`; the built-in protocol sends structured preferences and public job data only.
- Do not add LinkedIn, Indeed, Google Jobs, 104, authentication bypasses, or third-party scraper repositories. Follow `SOURCE_POLICY.md`.
- Treat scores, visa support, salary, and summaries as review aids. Verify them on the employer's official posting.
- Keep `user-data/`, `scans/`, credentials, application state, and personal company priorities untracked.
- `job-radar schedule render` only prints configuration. Do not install a cron entry or Scheduled Task unless the user separately authorizes that system change.
- Run tests and the repository secret checks before publishing. CI `gitleaks` is required for push and pull requests.

## Schedule examples

Render without installing:

```powershell
job-radar schedule render --platform windows --daily-at 08:15 --output user-data/job-radar-task.ps1
```

```bash
job-radar schedule render --platform cron --daily-at 08:15 --output user-data/job-radar.cron
```
