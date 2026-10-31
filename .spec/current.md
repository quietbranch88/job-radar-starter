# integrate-job-tracker

## Type

Feature

## Description

Upgrade `job-radar-starter` into a reusable, local-first single-developer Job
Radar toolkit. In addition to absorbing the generic application-tracking
capabilities from `job-tracker-skill`, publish the defensible engineering
patterns proven in an internal implementation: resilient source
orchestration, stable job identity, compact tiered AI review, richer Dashboard
state, and an optional self-owned Cloudflare synchronization template.

Internal workflows and personal data remain outside this reusable toolkit.
Nothing is synchronized automatically from another installation.

## Affected repository

This repository.

Internal reference material is read-only. GitHub pin/archive/link
changes are Ship metadata actions, not runtime code changes.

## In scope

### Local application tracking

- Application records, interview events, follow-up cadence, offer deadlines,
  funnel metrics, rejection-stage analysis, export, and deletion.
- Stable machine-readable contracts usable by Claude, Codex, another agent, or
  a normal script.
- Atomic local persistence under gitignored `user-data/`.

### Reliable source orchestration

- Per-source timeouts and structured failure records.
- Explicit `atomic` and `best-effort` modes.
- Best-effort partial output that is clearly labeled incomplete and never
  presented as a complete successful scan.
- Existing Greenhouse, Lever, and Ashby official adapters remain built in.

### Stable job lifecycle

- Stable identity independent of display punctuation and harmless title/company
  variations.
- User-configurable aliases without shipping a maintainer's real alias table.
- Deterministic merge rules, `first_seen`, `last_seen`, stale/expired state, and
  migration from legacy status keys.

### Compact AI review

- Provider-neutral command boundary with strict short JSON Schema.
- Fast review for eligible jobs from one configured provider.
- Versioned cache keyed by stable job identity, JD hash, profile/rubric hash,
  prompt version, model, and mode.
- Rejected queue and deterministic stratified quality sampling.
- Company facts require evidence source, observation date, and TTL.
- AI output is advisory and may not override hard exclusions.

### Dashboard

- Preserve the current public Dashboard visual language, dense cards, filters,
  swipe review, recommendations, and local state.
- Add loading, partial-scan, source-failure, rejected-review, stale/expired, and
  tracking summaries without embedding private data.
- Continue producing a framework-free static Dashboard.

### Optional Cloudflare synchronization template

- An optional, separately documented Worker/Pages template for a single
  developer to synchronize status across their own devices.
- Fail-closed authentication, input validation, bounded payloads, and no
  production account IDs, namespaces, domains, passwords, or maintainer deployment
  configuration.
- Local-only remains the default.

## Out of scope

- A maintainer's resume, profile, application history, interview notes, salaries, target
  companies, company priorities, scoring weights, hard-reject vocabulary,
  generated scan output, curated explanations, real aliases, or credentials.
- Internal Dashboard HTML, Cloudflare bindings, production URL, secrets,
  or local machine paths.
- JobSpy or bundled automation for LinkedIn, Indeed, Google Jobs, or 104.
- Cookie warming, browser impersonation, login/session reuse, undocumented
  endpoints, TLS verification downgrade, or access-control bypass.
- Automated applications, emails, messages, or writes to job platforms.
- Shared SaaS accounts, multi-user tenancy, billing, or a hosted public job
  aggregation service.
- Refactoring or publishing an internal installation.

## Architecture boundaries

```text
dashboard/CLI -> application -> domain <- data/adapters
```

- Domain models and rules do not import filesystem, Cloudflare, subprocess,
  HTTP, dashboard, or provider SDK code.
- User workflows are application commands over repository/provider interfaces.
- Official source adapters and optional sync adapters do not call upward.
- Dashboard consumes a versioned normalized view model, never raw provider
  responses or private configuration.
- Fake and production adapters implement the same boundaries.

## Privacy, security, and legal invariants

- A fresh clone contains no user data, company targets, credentials, private
  URLs, or personal scoring logic.
- User data stays under the configured local boundary unless the user
  explicitly enables their own sync or AI command.
- Before external transmission, documentation identifies the exact fields sent,
  retention/deletion responsibility, and third-party boundary.
- Built-in scanners use documented public ATS endpoints only.
- External adapters are user-supplied and disabled by default; finding code on
  GitHub does not establish authorization.
- Partial results, cached facts, visa support, compensation, language, and AI
  judgments are never represented as verified current facts without evidence.
- Existing official scan, local scoring, schedule rendering, and Dashboard
  behavior remain backward compatible unless an acceptance criterion explicitly
  changes it.

## Acceptance criteria

1. A fresh clone can initialize blank tracking state under gitignored
   `user-data/`, export it, and delete it without creating tracked personal data.
2. Application, interview, follow-up, offer, and rejection operations use stable
   identifiers, are retry-safe, preserve valid state, and reject malformed input
   without corrupting existing data.
3. Funnel, resume/channel/country slices, rejection stages, and due actions are
   deterministic short JSON; slices below 10 samples are labeled insufficient.
4. Source execution supports explicit `atomic` and `best-effort` modes with
   per-source timeout, structured failure evidence, deterministic deduplication,
   and an `incomplete: true` marker whenever any source fails or times out.
5. A hanging-source test proves best-effort output completes with healthy-source
   data, while atomic mode exits non-zero and does not replace the last complete
   output.
6. Stable identity, aliases, first/last seen, expiration, and legacy status-key
   migration behave deterministically across repeated and reordered scans.
7. Compact AI review sends only documented minimal fields, validates strict
   bounded JSON, reviews eligible jobs with one configured provider, never
   overrides hard exclusions, and reports provider calls and cache hits.
8. Repeating review with an unchanged stable identity, JD hash, rubric/profile
   hash, prompt version, model, and mode produces a cache hit; changing any
   versioned input invalidates the relevant entry.
9. Rejected jobs are retained locally with complete reason codes and a
   deterministic stratified daily sample. A provider rejection keeps that audit
   record.
10. Dashboard builds from the versioned public schema and visibly distinguishes
    complete, partial, loading, failed-source, rejected, stale, expired, and
    tracked states at desktop and mobile widths while preserving the existing
    design and interactions.
11. Optional Cloudflare sync passes authentication, authorization, schema,
    payload-size, safe-error, and cross-device read/write tests using fake or
    preview bindings; no real user data or production secret is committed.
12. README and source policy treat Claude, Codex, and other agents equally,
    document local retention/export/deletion and optional third-party sharing,
    and continue excluding bundled LinkedIn/Indeed/Google Jobs/104 automation.
13. The full test suite, example Dashboard build, privacy/secret scan, dependency
    audit, generated-artifact inspection, and relevant browser smoke all pass
    before merge.

## Ship actions after acceptance criteria pass

- Merge through a reviewed PR with CI and secret scanning green.
- Archive `job-tracker-skill` after its reusable behavior and documentation are
  represented in `job-radar-starter`.
- Remove `job-tracker-skill` from pins and pin `job-radar-starter`.
- Reconfirm any remaining public fork cleanup separately; do not make unrelated
  deletion a condition for runtime correctness.

## Accepted review remediation criteria

14. The documented public CLI can execute one coherent local workflow from a
    versioned scan envelope through scoring, optional review, lifecycle merge,
    tracking state, and versioned Dashboard output; legacy list input remains
    supported.
15. A status-changing application event is committed as one atomic repository
    operation, and an identical retry succeeds while a conflicting retry is
    rejected without changing state.
16. Source orchestration enforces a configured concurrency ceiling, handles a
    near-limit successful payload without Queue deadlock or false timeout, and
    leaves no child processes or IPC resources behind.
17. AI review enforces per-call timeout, total deadline, and fast-call budget;
    partial progress is checkpointed, rejected/current lookup is linear, and
    cache/rejected retention is bounded and documented.
18. Dashboard generation does not duplicate the full jobs payload, uses indexed
    rejected lookups, and has a tested bounded rendering strategy for large
    scans without changing the existing visual identity.
19. Optional cross-device sync cannot silently lose concurrent writes and
    rejects a request when the merged stored status set would exceed the
    documented maximum.
20. Published JSON Schemas match runtime bounds, tracked release files contain
    no sensitive repository/person/local-path metadata, and the release gate
    exercises the complete advertised workflow while scanning all tracked
    release text.
21. The installed CLI can configure and execute the compact review pipeline
    without Python-only dependency injection, and the release gate exercises a
    configured fake-provider run.
22. A provider or review runner that ignores its timeout is terminated outside
    the blocking call; per-call and total deadlines are hard wall-clock bounds.
23. Review cache/rejected progress remains crash-recoverable without rewriting,
    sorting, and hashing the complete retained history once per reviewed job;
    a 2,000-reject benchmark completes within the accepted test budget.
24. Dashboard rejected audit rendering is windowed and a 5,000-job plus
    5,000-reject artifact stays below 3,500,000 bytes without changing the
    visual identity or losing audit access.
25. Untracked expired lifecycle entries are pruned by a documented retention
    rule while user-tracked jobs remain preserved.
26. Per-source job and response-size limits are enforced before a complete
    oversized result is materialized or sent through IPC.
27. Hosted PR CI runs the public release contract, privacy scanning includes
    tracked source/script text, and documented dependency direction matches
    executable imports.
28. Standalone `review` enforces the same hard total wall-clock deadline as
    integrated `run`, including repository/pipeline stalls.
29. External command stdout/stderr is bounded while streaming, and timeout or
    overflow cleanup proves the entire spawned process tree is gone.
30. Source timeout cleanup uses bounded join, forced kill, and bounded final
    cleanup on every supported platform.
31. Rejected audit retention has deterministic cardinality and serialized-byte
    ceilings in addition to the time window.
32. Privacy scanning covers every tracked text/config file, including
    extensionless and JSON-with-comments files, with exact-line allowlists only.
33. Hosted Python dependencies are installed from a pinned, hash-verified lock.
34. Application code depends on injected source/scoring/persistence boundaries;
    concrete ATS, subprocess, and filesystem orchestration live outside the
    application layer.
35. Compact AI evidence contains only bounded public posting fields or explicit
    JD excerpts; locally derived preference, scoring, and risk text is never
    transmitted.
36. Privacy tests use synthetic sentinels, UTF-8 and UTF-16 tracked text are
    scanned fail-closed, and the publishable branch history contains no removed
    private metadata.
37. Timeout cleanup commands and final process waits/joins are themselves
    bounded, including simulated cleanup-tool stalls.
38. Public disclosure documentation distinguishes the legacy scoring protocol
    from compact review, and process-death tests use deterministic child/event
    assertions with load-tolerant wall-clock ceilings.
