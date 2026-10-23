# Operator guide

## 1. Initialize and scan

Run `job-radar doctor`, then initialize the local tracking store through the
tracking API or your agent/script adapter. Configure only official Greenhouse,
Lever, or Ashby boards you are authorized to query.

Atomic mode does not replace the last complete output when any configured
source fails. Best-effort mode writes partial healthy-source results with
`incomplete: true` and structured failures. Treat incomplete scans as partial,
not as proof that missing companies have no jobs.

## 2. Normalize, score, and review

Stable IDs and aliases prevent the same application from splitting when a
company or title spelling changes. Lifecycle merge records `first_seen` and
`last_seen`; absent jobs become stale and later expired according to the
configured policy.

Expired jobs without a tracking status remain available for 90 days after the
30-day expiry threshold, then are pruned from lifecycle state. Jobs with an
interested, applied, hidden, or expired tracking decision are retained.
`merge_scan_state` exposes `untracked_expired_retention_days` when a different
deterministic local window is required.

### Runtime dependency boundary

Pure scoring rules live in `job_radar.application` and depend only on the
normalized job model and user configuration. The CLI composes concrete
boundaries around them: `official_sources` selects Greenhouse, Lever, or Ashby;
`legacy_command_adapter` owns the bounded external scoring command;
`job_output` owns JSON file replacement; and Dashboard rendering remains a UI
adapter. `public_workflow` receives scoring and repository behavior through
callable or protocol ports instead of importing those concrete adapters.

Local rules run first. A hard exclusion cannot be reversed by a model. Eligible
jobs can receive fast review, while strong review is reserved for high-fit,
near-threshold, visa-uncertain, or conflicting cases. AI output is advisory.
The operator makes the application decision.

Review results use a versioned cache keyed by the stable job, JD hash, profile
rubric hash, prompt version, model, and review mode. Rejected records remain
auditable, and the deterministic rejected sample supports daily quality checks.

Configure providers through the bounded timeout contract
`review_with_timeout(mode, request, timeout_seconds=...)`. The provider adapter
must terminate or cancel its own subprocess or HTTP request when that timeout
expires. The built-in external-command adapter starts an isolated process
group, drains stdout and stderr incrementally with a 1 MiB limit for each
stream, and kills the whole process tree immediately on timeout or overflow.
Both the standalone `review` command and integrated `run` workflow execute the
configured review runner in a separate spawned process and kill that process
tree at the total deadline; custom Python runners must therefore be
importable/pickleable rather than local closures. Recommended
public defaults are a 30-second per-call
timeout, a 300-second total review deadline, and at most 100 fast-model calls
per run.

Configure a compact provider command explicitly as the final option:

```powershell
job-radar --json review --jobs scans\latest.json --output scans\review.json --provider-command python user-data\review_provider.py
job-radar --json run --provider-command python user-data\review_provider.py
```

Use `--per-call-timeout`, `--total-review-deadline`, and `--max-fast-calls`
before `--provider-command` to override the safe defaults. The command receives
one compact request on stdin and must write one response on stdout. It is not
invoked through a shell. The legacy `--ai-command` boundary uses the same
isolated, streaming, timeout, output-limit, and process-tree cleanup behavior.

Completed responses are checkpointed to the cache before the next provider
batch, and rejected audit progress is persisted after the first decision,
periodically in bounded batches, and at normal completion. An interrupted
replay therefore retains an initial durable checkpoint while avoiding
quadratic full-document rewrites for large rejected queues. Cache entries are
kept for 30 days by default; rejected audit entries, including rescued
decisions, are kept for 90 days by default. After date pruning, rejected
history is also limited to the newest deterministic 10,000 records and an
8 MiB serialized document, whichever limit is reached first. Override
`cache_retention_days` and
`rejected_retention_days` with positive integers when local policy requires a
different bounded time window; the safety caps still apply.

## 3. Track the funnel

Record each application and append events for interview, rejection, offer, and
other stage changes. Follow-up calculation covers thank-you notes, application
follow-up, no-response review, promised-response follow-up, and offer
deadlines. Funnel metrics and rejection-stage slices are derived from these
records rather than entered separately.

### Confirm a submission locally

The Dashboard's Applied button records a browser marker, not evidence that an
application was submitted. It does not create an application or update funnel
metrics. After verifying the real submission, export Dashboard state and run:

```powershell
job-radar --json tracking record-application --from-dashboard user-data\job-radar-state.json --job-id "<stable-job-id-from-export>" --applied-at unknown --resume-version "backend-v2" --channel "official-site" --confirmation-evidence "Submission receipt checked locally" --confirmed-at "2026-10-23T00:05:00Z" --confirm-submitted
job-radar build-dashboard --jobs scans\latest.json --user-data user-data --output dashboard\public\index.html
```

Move the downloaded export into your gitignored `user-data/` directory first.
The selected stable ID must match a job in the existing local lifecycle store,
and its exported marker must be Applied. The command uses that persisted job's
identity, company and title, then calls the existing application upsert command.
`--confirm-submitted` is an explicit operator assertion. Supply the actual
submission time with a timezone, or the literal `unknown`; `--confirmed-at` is
the separate time you verified the submission. Unknown submission time does not
generate submission-age follow-ups. Resume version, channel and a short local
confirmation reference are required; do not paste credentials into the reference.

Replaying an identical confirmation returns the existing application, including
later interview/offer status. A conflicting confirmation or an existing legacy
application requires explicit reconciliation instead of overwriting history.
Existing application/event APIs support metadata and stage changes. Corrections
to a confirmed submission identity or time require explicit local reconciliation;
this import does not rewrite them.
Rebuild or reload the rebuilt Dashboard to see the persisted metrics and status.
Persisted application statuses take precedence over stale browser markers and
cannot be cleared with browser-only buttons. `build-dashboard --user-data` is
offline: it projects tracking without fetching sources or refreshing scan dates.
No HTTP write endpoint or application submission automation is added.

## 4. Build and use the dashboard

Run `job-radar build-dashboard` against the normalized view model. The dashboard
shows scan completeness, active/stale/expired lifecycle, recommendations,
rejected audit, application metrics, due actions, filters, cards, and swipe
review. Browser status buttons persist in localStorage.

## 5. Export, delete, or sync

Use `LocalTrackingStore.export_to` to export applications, events, lifecycle,
and rejection data, plus existing optional Sheets outbox/review receipts, outside the managed root. Use
`LocalTrackingStore.delete_tracking_data` to delete managed tracking, lifecycle,
rejected, review cache, company-fact files and optional outbox/receipts. Exports, scans, and browser
localStorage require separate deletion.

Cloudflare sync is optional. If you enable it, configure your own Worker,
SQLite-backed Durable Object,
and bearer token; then verify GET, POST, and DELETE with the versioned status
contract. Do not treat the template as a shared hosted service.

## 6. Preserve observations and resume a company walk

`run` saves every scored observation in its scan output and local lifecycle,
including jobs below `matching.minimum_score`. Only Dashboard/review eligibility
is filtered. A changed profile can therefore rebuild the saved observations
offline without another source request:

```powershell
job-radar build-dashboard --jobs scans/latest.json --profile user-data/profile.yaml --user-data user-data --output dashboard/public/index.html
```

Omit `--profile` only when intentionally rendering the supplied document as-is.
The offline profile option rescales local rules and filters display; it does not
write observations, refresh dates, invoke an AI provider, or alter submissions.

Checkpointing is opt-in for `scan` and `run`:

```powershell
job-radar --json run --mode best-effort --checkpoint scans/walk-2026-10-24.json
job-radar --json run --mode best-effort --checkpoint scans/walk-2026-10-24.json --resume --source-timeout 60
```

A checkpoint represents the configured company boards, not a whole provider
catalogue or page-level cursor. Each fully completed company, including an empty
result, is saved before progressing. Resume reuses those results and retries
failed/pending companies; changing retry timeout or concurrency is allowed.
Company/source/board/country or job-limit changes invalidate the plan. Changing
atomic/best-effort mode also requires a new checkpoint. Starting a new walk at an
existing path, malformed state, duplicate fields or an invalid completion digest
fails closed without deleting the old state. Use a different path for a fresh
walk. `--resume` alone is invalid.

Across calendar days, the resumed walk retains its original `observed_on`, with
actual completed-company fetch timestamps recorded separately. Each job carries
the UTC date of its company's completed fetch in `last_seen`; cached companies
keep their earlier dates while newly successful companies use their actual
retry day. The lifecycle evaluates the latest observed day without relabeling
cached jobs. A posting published on the retry day is therefore valid, while a
posting dated after its own observation remains invalid. Offline profile rebuilds
change only scoring fields, preserving identity, evidence and observation dates.
The local checkpoint format is v2; older experimental v1 files fail closed and
are retained, not migrated or deleted. The public scan envelope remains v1.
Replaying a completed checkpoint is not a fresh scan. Finish an outstanding walk before
starting a newer lifecycle walk against the same local user-data; an older
observation cannot overwrite newer lifecycle dates. Checkpoint storage is bounded
to 128 MiB and the source's configured per-company job limit. A local OS guard
allows one writer per checkpoint and remains on disk after release. It is not a
distributed lock or a network-filesystem guarantee. Do not delete a live `.guard`
file or use a guard as an output target.

Output paths cannot alias their inputs/other outputs, use reserved `.guard`
names, or replace managed `user-data/tracking/` and `user-data/review/` state.
The CLI checks these boundaries before scanning/rendering. Explicit run receipt
creation also uses its own OS guard so two different jobs-output paths cannot
both own the same new receipt.

## 7. Read execution receipts by stage

Every `run` creates a distinct receipt under the jobs output's `run-manifests/`
directory. `--run-manifest PATH` selects an explicit new path; an existing receipt
is never overwritten. JSON and human-readable reports name the receipt. Each
receipt separates scan, workflow, review and build, with sync `not_configured`
and deployment `not_requested`. A normal return is `finished`, not a claim that
all jobs are approved or that a release was deployed. A disabled provider is
`review=not_configured`; a configured runner's completion is execution evidence,
not a per-job/resume approval receipt.

Failures preserve earlier completed stages and leave later stages pending. The
receipt records safe error types, not raw upstream exceptions. An abrupt process
exit may leave a stage `running`; inspect the checkpoint and resume explicitly.
Only complete company reads are reusable, and an interrupted in-flight read is
retried. Run output serialization protects one jobs-output path; it does not make
independent commands sharing a user-data directory a multi-writer database.

## 8. Diagnose configured sources

Add `--health-output scans/source-health.json` to `scan` or `run`. This separate
v1 health receipt leaves the scan envelope unchanged and names its scope as
`configured_company_boards`, never a full provider catalogue. Per-board outcomes
distinguish complete, cached, failed, interrupted and unattempted work. Cached
entries keep their original fetch timestamp and have no new attempt duration.
HTTP error status/categories and network errors contain no raw upstream text.
Connection timeouts use `network_timeout` in health while preserving `timeout`
in the existing scan failure contract. Parent deadlines remain `timeout`.
Diagnostics do not automatically retry; use a matching checkpoint and `--resume`.

## 9. Record a version-bound review attestation

```powershell
job-radar --json review-receipt record --user-data user-data --jobs scans/latest.json --job-id JOB_ID --jd-sha256 JD_SHA256 --resume-sha256 RESUME_SHA256 --reviewer REVIEWER --evidence LOCAL_REVIEW_REFERENCE --decision approve --reviewed-at 2026-10-24T09:00:00+08:00 --confirm-reviewed
job-radar --json review-receipt check --user-data user-data --jobs scans/latest.json --job-id JOB_ID --jd-sha256 JD_SHA256 --resume-sha256 RESUME_SHA256
```

Supply real 64-character lowercase hashes, not those placeholders. The CLI
checks that the JD hash matches the saved job snapshot. The operator supplies
the actual final resume digest; the CLI never opens that file. The explicit flag
attests that review occurred, not that a provider finished. Latest reviewed time
for the exact job/JD/resume governs; equal times prefer rejection. A matching
rejection is applicable but not approved. Changing the JD or resume invalidates
the old approval; historical receipts remain. Future review timestamps are
refused. A receipt is neither cryptographic reviewer authentication nor permission
to apply. No application automation is added.

## 10. Deliver a minimal snapshot to an owned Sheets tab

This optional command is separate from `run` and Cloudflare. It does **not** map
or modify an existing job master sheet. Precreate a dedicated tab with exactly
nine grid columns, 2..1001 rows and these literal row-1 headers:

```text
job_id,payload_hash,company,title,url,status,applied_at,resume_version,confirmation_evidence
```

Use a private target owned by this single local writer. No other host, person or
automation may concurrently edit it. Formulas/unknown rows, duplicate IDs,
unexpected headers or capacity fail closed; the CLI does not repair them.

```powershell
job-radar --json sheet-sync enqueue --user-data user-data --jobs scans/latest.json --job-id JOB_ID --spreadsheet-id SHEET_ID --tab SyncTest
job-radar --json sheet-sync status --user-data user-data
job-radar --json sheet-sync deliver --user-data user-data --spreadsheet-id SHEET_ID --tab SyncTest --max-items 1 --confirm-write
```

Enqueue is local and defaults to `interested`, never `applied`.
`--from-application` uses only an explicitly confirmed local application with
actual submission time (or `unknown`), resume version and evidence. A browser
marker is insufficient. Keep URLs canonical without query/fragment/userinfo;
resume version is an identifier, not a private path. Unsupported text is refused,
not silently sanitized. No personal notes or resume content are sent.

Before delivery, securely provision your own `JOB_RADAR_SHEETS_ACCESS_TOKEN` in
the environment with appropriate Google permissions. Do not paste it into a
command, config, issue or chat. No token creation or login discovery is provided.
Missing credentials/confirmation perform no request. Read
[`privacy-and-data.md`](privacy-and-data.md) before authorizing disclosure.

Each intent is saved before network access. Delivery reads the keyed row,
rechecks its version before a fixed-range RAW write, and reads back before local
acknowledgement. Retry after uncertain delivery reconciles an exact existing
version without appending. Updates require `--expected-remote-hash` from a known
acknowledged version. Conflicts block the target rather than overwriting it.
This is single-writer idempotency, not distributed CAS or global exactly-once.
Repeated enqueue of the current transition is write-free; after intervening
acknowledged versions, a repeated transition gets a new bounded occurrence in
local history so A-to-B-to-A-to-B cannot be mistaken for an old completed B.
Earlier experimental outbox records without an occurrence remain readable.

Default work is five items; `--max-items` allows 1..20, at most seven HTTP calls
per fresh item. Each direct HTTP request has a 10-second socket timeout and
4 MiB response limit. There is no hidden retry/backoff loop. Remote uncertainty
remains pending for manual reconciliation up to three attempts; semantic failures
block immediately. Wait/back off before retrying quota errors. A non-null error
is a nonzero exit. The outbox retains at most 500 items including delivered
receipts; export and reconcile explicitly, without silent history pruning.
Local deletion never deletes an existing Google copy.

Optional managed outbox/receipt files, their guards and parent directories must
be unaliased local paths. Existing symlinks, junctions and hardlinked state/guards
are refused before recording, export or deletion. This is not protection against
an attacker replacing filesystem paths during an operation; keep user-data under
exclusive local control.
