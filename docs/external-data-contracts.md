# External data contracts

Only contract version 1 is implemented. Unknown versions and extra fields fail
validation instead of being silently accepted.

## Compact `review` pipeline

Built-in Greenhouse, Lever and Ashby adapters retain up to eight public posting
excerpts of 500 characters each in `Job.jd_evidence`, plus the source/field origin
in `jd_evidence_source`. `jd_hash` is SHA256 over the complete normalized posting
text before excerpt truncation. Full raw descriptions are not persisted by these
adapters. Greenhouse requests `content=true`; Lever includes description, list
sections and additional text; Ashby uses its plain description or HTML fallback.
HTML markup and script/style content are removed. These fields survive scan JSON,
lifecycle persistence and review preparation. They do not contain profile-derived
keywords, local scoring explanations, or application state.

The compact review cache identity includes the posting digest and evidence.
Provenance and hashes remain local; the provider request below is unchanged.
Older jobs without the optional evidence fields remain readable. New scans carry
`observed_on`; scoring preserves it, and offline rendering reuses it. A legacy
scan without an observation date displays no observation date instead of treating
the render date or the posting's publication date as a fresh observation.

Each fast or strong review request contains exactly:

- `contract_version`
- `stable_id`
- `title`
- `company`
- `country`
- `local_fit`
- `jd_evidence`

The pipeline truncates bounded JD evidence before invoking the configured
provider. Not sent: `full_jd`. Not sent: `resume contents`. Not sent:
`application_history`. Not sent: `interview notes`. Not sent: `job url`. The
local cache inputs `jd_hash` and `profile_rubric_hash` are not sent. Not sent:
`local summary`. Not sent: `local risk`. `jd_evidence` is built only from explicit
public posting evidence plus public location and source-provenanced
`public_skills`; the ordinary `skills` field is excluded because local scoring
may derive it from the profile. Evidence never includes locally derived company
preferences, must-have rules, visa preferences, or other scoring explanations.

Responses contain exactly `contract_version`, `stable_id`, `decision`, `score`,
`reason_codes`, and `summary`. See the request and response schemas in
[`schemas/`](schemas/).

The installed CLI accepts the compact provider as an explicit external command:

```text
job-radar review ... --provider-command <executable> [arguments...]
job-radar run ... --provider-command <executable> [arguments...]
```

`--provider-command` must be the final Job Radar option. Each invocation
receives one request object on stdin and returns one response object on stdout.
The adapter uses no shell, incrementally bounds both stdout and stderr to 1 MiB,
and terminates the complete provider process tree immediately when either
stream overflows or the configured per-call timeout expires. Both standalone
`review` and integrated `run` separately enforce the total review deadline
around the complete configured runner.

## Legacy `--ai-command`

The legacy scoring command receives a larger stdin JSON payload. Its
configuration fields are `profile.skills`, `preferences.countries`,
`preferences.roles`, `preferences.tracks`, `preferences.visa_required`,
`companies.preferred`, `companies.excluded`, `matching.minimum_score`,
`matching.must_have`, and `matching.exclude_keywords`, plus `jobs`.

Each item in `jobs` includes source, external ID, company, title, location, job
URL, publication date, country, category, salary, tracks, skills, first-seen
date, and visa-support state. Use the compact `review` pipeline when minimizing
provider data is the priority. The legacy process boundary nevertheless uses
the same isolated process group, streaming stdout/stderr limits, timeout, and
descendant cleanup as compact review.

## Optional Cloudflare status sync

The local `tracking record-application` bridge is separate from this sync
contract. Confirmed application metadata stays in the operator's local tracking
store and its existing export/deletion boundary; it is not sent to the Worker.

The POST body contains exactly `contract_version`, `statuses`, and
`legacy_keys`. `statuses` maps each stable job ID to a status value:
`interested`, `applied`, `skip`, or `dead`. `legacy_keys` lists obsolete keys
that the Worker removes before merging the new statuses.

GET returns `contract_version` and `statuses`; DELETE removes the single
operator-owned document. The Worker deliberately does not accept applications,
events, resume data, interview notes, job descriptions, or review cache data.

Cloudflare is a third party when this optional template is enabled. The
operator is responsible for provider retention, training, sharing, deletion,
credentials, disclosure, and applicable law. This documentation is not legal
advice.

## Local continuation and run evidence

The local scan-envelope and Dashboard contracts remain version 1. `run` scan
artifacts now preserve all scored observations, including low-score jobs; the
Dashboard view model remains an eligibility-filtered projection. Offline
`build-dashboard --profile` rescoring never promotes observation timestamps.

The opt-in checkpoint is a separate local version-2 document with `plan_hash`,
original `observed_on`, `started_at`, `updated_at`, and ordered `companies`.
Each entry holds `scope` (company/source/board/country), `state`, public `jobs`,
`jobs_hash`, `fetched_at` and safe `failure`. Only a validated `complete` entry
with an intact result digest is reused. Digests detect accidental state changes;
they are not signatures against a hostile local account. Checkpoints have a
128 MiB document ceiling and retain the configured per-company job-count limit.
Each completed result's optional scan-job `last_seen` is the UTC date of that
company's actual `fetched_at`, validated on resume. Earlier cached companies are
not redated when another company succeeds later. A lifecycle's evaluation date
may advance beyond the original walk date, without changing either job's actual
observation date. Future-dated postings are checked against their own observation.
Legacy jobs without this optional field use the supplied observation date.
Experimental checkpoint v1 is rejected and preserved; no implicit migration runs.

Run receipts are separate local version-1 documents identified by `run_id` and
record attempt `state`, start/finish timestamps, stage states, optional original
`observed_on`, company `scan_progress` and `error_type`. They are not an application
record, review approval, deployment receipt or the optional status-sync document.
No new public HTTP write API, ATS request field, provider field or Cloudflare
status field is added by these local capabilities.
