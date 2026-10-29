# Audit

Base: `9a0c580`. S1, S3 and S4 are confirmed/open at intake. S2 remains an
optional scaling improvement outside scope. Previous evidence used an isolated
snapshot: malformed envelopes overwrite atomic output; numeric Lever dates fail
the integrated workflow; an empty resumed company loses its latest observation
date at the scan boundary. This patch requires new fail-to-pass evidence against
the checked-out base and unchanged regression expectations.

S4 baseline: the new spawned-scan -> CLI -> lifecycle regression failed at the
expected assertion (`active` rather than `stale`); five invalid-envelope controls
passed. Original workflow/source contracts are the oracle. The patch adds an
optional scan observation bound from completed-company timestamps, preserving
legacy envelopes and original/per-job dates. No checkpoint migration is needed.

## 2026-10-29 repair and verification

Reviewed base: `9a0c580`; repaired code: `4c34b25`. All data in committed tests is
synthetic. Severity is P2 for each confirmed defect because ordinary source
responses or next-day retries could invalidate local job-state decisions.

| ID | Trigger and affected path | Expected / original actual | Validity / disposition | Evidence |
| --- | --- | --- | --- | --- |
| S1 | Malformed official-source response -> adapters -> spawned scan -> atomic file/checkpoint | Reject malformed; original accepted empty envelopes and overwrote prior output as successful | Confirmed / fixed | `tests/test_adapters.py`, `tests/test_adapter_workflow_contracts.py`; malformed roots, arrays, entries and fields fail; valid empty feeds pass |
| S3 | Lever numeric `createdAt` -> scan -> public CLI workflow -> lifecycle | Normalize source timestamp; original stringified milliseconds and lifecycle rejected it as invalid ISO | Confirmed / fixed | `test_lever_epoch_milliseconds_flow_through_spawned_scan_and_public_run`; real local file and lifecycle path, synthetic HTTP |
| S4 | Next-day checkpoint retry returns zero jobs -> scan envelope -> lifecycle | Advance evaluation date to successful fetch date; original retained the walk date and left a stale job active | Confirmed / fixed | `test_empty_next_day_retry_updates_lifecycle_and_replay_keeps_fetch_date`; also verifies score/offline consumers and no refetch on replay |
| S2 | Many boards on one ATS during throttling | Shared cooldown / Retry-After handling is an optional design improvement | Not reproduced as a defect / deferred | Outside the accepted S1/S3/S4 repair; evaluate separately if scale requires it |

Baseline evidence: new adapter/workflow tests against the original source had
81 failures / 14 passes, including malformed empty success and the Lever CLI
failure. S4 had one expected freshness assertion failure / five passing invalid
metadata controls. Independent review then reproduced one remaining S1 variant:
Ashby `isListed: null` was filtered into successful empty output. Its added
regressions failed 8 / passed 2 before exact-bool validation, then passed. Missing
`isListed` and explicit false retain their prior allowed behavior.

The first combined suite exposed the unchanged six-field source-health contract.
The producer was narrowed to emit the optional observation field only when it
adds information; the original assertion was retained. Four other initial
failures were an absent `powershell` executable on macOS, resolved for local
verification with a temporary command alias to installed PowerShell 7.6.3.

Final local checks on the repaired code (macOS, Python 3.15.0):

- `python -m pytest -q -rs`: **779 passed, 3 skipped** (Windows Job Object cases).
- `PYTHON=<test-python> PLAYWRIGHT_CHANNEL=chrome npm run test:browser`:
  **21 passed**. Existing desktop/mobile CLI recovery and review flows ran in
  Chrome; sync cases used the local Worker and synthetic data, with selected
  malformed HTTP/storage cases substituted as stated in their test names.
- Worker `npm run typecheck`, `npm test`, `npx wrangler deploy --dry-run`:
  typecheck passed, **31 tests passed**, local bundle succeeded; no deployment.
- Both root and Worker `npm audit --package-lock-only`: **0 vulnerabilities**.
- Gitleaks **8.30.1**, release checksum verified: no leaks; tracked privacy scan
  and `git diff --check` passed. Repository security/policy tests are included in
  the full Python suite.
- Example dashboard rebuilt with SHA-256 identical to the tracked example.
- Independent reviewer of `4c34b25`: **160 focused tests passed**, no remaining
  material finding after the Ashby correction.

External boundary evidence: one read-only Lever `leverdemo` request returned five
postings with numeric `createdAt`; all five parsed successfully. The official
Ashby endpoint returned HTTP 403, so live Ashby acceptance is unverified. Greenhouse
live service was not exercised. Full scan/workflow regressions use synthetic
HTTP, real spawned workers, real CLI, and disposable local persistence; they do
not establish live availability for every provider. Windows-only cases remain
for the hosted release workflow. Remote checks, merge and deployment are separate
states, not inferred from local passes.

Official response contracts consulted:
[Lever](https://github.com/lever/postings-api),
[Ashby](https://developers.ashbyhq.com/docs/public-job-posting-api), and
[Greenhouse](https://docs.greenhouse.io/job-board.html).
