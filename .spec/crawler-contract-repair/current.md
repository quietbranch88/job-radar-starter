# Crawler data contract repair

## Scope

Repair the three reproduced public-edition defects S1, S3 and S4 from base
`9a0c580`. Provider-wide cooldown (S2), source expansion, UI redesign and deployed
state changes are outside this change.

## Acceptance

1. Observable: malformed source roots/envelopes/entries fail as malformed; valid
   empty feeds succeed. Atomic failures preserve prior output; failed companies
   never become completed checkpoints. Environment: isolated spawned scanner with
   synthetic transport. Verify: adapter and adapter-workflow contract tests.
2. Observable: the numeric Lever createdAt fixture accepted by the existing
   parser becomes a UTC ISO timestamp and completes scan, scoring, lifecycle and
   dashboard generation. Missing dates remain empty; malformed dates fail safely.
   Environment: isolated CLI, local synthetic state, no paid provider.
   Verify: adapter-workflow contract tests, including the unchanged-input baseline.
3. Observable: a company successfully returning zero jobs on a resumed next-day
   walk advances lifecycle evaluation to that actual fetch day. Original walk
   date and cached job dates remain unchanged; replay does not invent new
   observations. Environment: original checkpoint, spawned scanner and local CLI
   with controlled parent clock. Verify: tests/test_empty_observation_resume.py.
4. Observable: existing scan envelopes without the new optional observation-date
   metadata still load, score and render. Invalid/future metadata fails closed.
   Environment: local JSON boundaries. Verify: observation, lifecycle and CLI tests.

## Requirements and path

Sources: existing `.spec/current.md` criteria 4, 5, 14 and 26; official adapter
fixtures; `docs/operator-guide.md` continuation/date contract. The affected path is
CLI -> spawned official adapter -> scan/checkpoint -> scoring -> lifecycle storage
-> generated dashboard. HTTP is substituted in deterministic regressions; any
real-source smoke is separate evidence. No deployment is implied.

The optional scan-envelope extension records the latest completed company
observation date only when it exceeds the walk and per-job dates, including
empty successful fetches. It does not refresh cached job timestamps or change
the original walk date. Older strict consumers must upgrade to read this field.

Implementation and local verification are complete at `4c34b25`. All three
regressions have fail-to-pass evidence; the independent final review found no
unresolved material issue. The audit records local checks and external-service
limits. Remote PR/CI state is authoritative in GitHub; no merge or deployment is
part of this repair.
