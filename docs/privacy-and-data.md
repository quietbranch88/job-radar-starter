# Privacy and local data

Job Radar is local-only by default. Sync never starts unless explicitly enabled.

## Managed local state

The operator chooses an absolute `user-data/` root. `initialize` creates:

- `tracking/applications.json`
- `tracking/events.json`
- `tracking/lifecycle.json`
- `tracking/rejected.json`
- `review/cache.json`
- `review/company-facts.json`

Raw and normalized runs are written under `scans/`. Dashboard decisions and
theme preference are separate browser localStorage values. Clearing files does
not clear browser localStorage, and clearing the browser does not delete files.

Optional commands additionally create `tracking/sheets-outbox.json` and
`review/receipts.json`; initialization does not create these documents.
`LocalTrackingStore.export_to` writes applications, events, lifecycle, rejected
state and any existing optional outbox/receipts to a destination outside managed
`user-data/`. Review cache and
company facts are intentionally excluded. Exported files are not deleted by
`LocalTrackingStore.delete_tracking_data`; the operator must locate and remove
exports separately.

`LocalTrackingStore.delete_tracking_data` deletes the six managed JSON files
above plus existing optional outbox/receipts, after acquiring their writer
guards, and then removes empty managed directories. Persistent `.lock`/`.guard`
files contain no review/application payload and remain. It does not delete `scans/`,
browser localStorage, dashboard exports, or other files outside the configured
root.

Explicitly confirmed applications also retain the submission time (or literal
`unknown`), resume version, channel, confirmation time and a short confirmation
reference in `tracking/applications.json`. Application records are retained until
the operator deletes managed tracking data; no automatic expiry is added.
Existing tracking exports include these fields and require separate deletion.
Browser Applied markers do not create these records. Confirmation references are
not included in provider requests, the Dashboard tracking projection or optional
Cloudflare status sync. Keep references short and free of credentials.

Review cache is pruned to 30 days and rejected audit history to 90 days by
default. Both windows are locally configurable with positive
`cache_retention_days` and `rejected_retention_days` values. Rescued rejection
evidence is preserved for the same rejected-audit window rather than silently
removed when the final recommendation changes. After time pruning, rejected
history retains the newest deterministic records up to 10,000 entries and an
8 MiB serialized document; the first limit reached wins.

Lifecycle records without a tracking status are pruned 90 days after they
become expired (120 days after `last_seen` with the default expiry threshold).
Tracked lifecycle records remain until the user clears managed tracking data.

## External processing

The compact review contract minimizes data sent to a selected model provider,
and excludes locally derived summary, risk, company preference, must-have, and
visa-preference text. Its bounded evidence comes only from explicit public job
evidence, public location, and source-provenanced `public_skills`. The ordinary
`skills` field is not sent because local scoring may derive it from the private
profile. Any configured provider
receives the remaining documented request fields. Before
enabling one, the operator is responsible for reviewing provider retention,
training, sharing, deletion, regional processing, and account settings.

Optional status sync is disabled by default. If enabled, Cloudflare is a third
party and receives the status contract described in
[`external-data-contracts.md`](external-data-contracts.md). The operator is
responsible for credentials, access control, retention, disclosure, deletion,
and applicable law. This project is not legal advice.

Google Sheets delivery is a separate opt-in command, not automatic scan or
Dashboard sync. Enqueue is local-only. `sheet-sync deliver --confirm-write`
shares the allowlisted company/title/public URL/status and, only when explicitly
projected from a confirmed application, submission time, resume version and short
confirmation reference. No resume contents, email, personal notes or full JD are
sent. The OAuth token is caller-provisioned via an environment variable, used in
HTTPS headers only and never copied to the outbox or report. The project does
not log in, discover, refresh or store Google credentials.

The operator must own the dedicated target tab, control its sharing and review
Google/account retention, regional processing and deletion policies. There is no
automatic expiry: local outbox payloads and receipts remain until explicit local
deletion, and the 500-item bound includes delivered history. Local deletion does
not delete or revoke Google rows or separately exported files. Remote removal
must be done separately by the owner; this CLI has no remote delete/share API.
Blocked conflicts require explicit reconciliation, never silent overwrite.

Review approval receipts contain supplied job/JD/resume hashes, reviewer identity,
short evidence reference, decision and aware timestamp. They are operator
attestations, not signatures or proof of independent human review. Receipt
creation never opens/uploads a resume or contacts a reviewer/provider. References
are not dereferenced; keep them non-sensitive. Receipts are retained without
automatic expiry and included only in explicit local export, not the Dashboard,
provider or Sheets payload.

## Observation checkpoints and execution receipts

Opt-in checkpoints contain public posting data, company-board scope, completion
digests, safe failures and actual fetch timestamps. Run receipts contain local
stage/progress metadata and error types. Neither contains resume contents,
application confirmations, tokens or interview messages. They are local artifacts,
normally under gitignored `scans/`; do not publish a custom checkpoint/output
path or upload it as a review attachment. No additional third-party transmission
is introduced by checkpointing or offline profile filtering.

The operator retains checkpoints, receipts, exports and scans until explicitly
removing them. Managed tracking deletion does not remove these files or browser
storage. Persistent guard files must not be deleted while a writer is active.
The 128 MiB checkpoint bound limits size, not retention time. Saved low-score
observations remain available locally; Dashboard projection and configured
review input include only eligible jobs.

Optional `--health-output` records configured board identifiers, safe categories,
HTTP error status, local durations and cached/actual fetch timestamps. It does
not retain raw upstream errors, tokens or response bodies. It follows the same
explicit-removal policy as checkpoints and is not removed by tracking deletion.
