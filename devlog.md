# Development log

## 2026-10-29: Crawler contract repair

Malformed source envelopes now fail safely, Lever millisecond timestamps become
UTC ISO datetimes, and successful empty checkpoint retries preserve their actual
observation date through scoring and lifecycle evaluation. Regressions and final
independent review cover code revision `4c34b25`.

Local results: 779 Python tests passed (three Windows-only skips), 21 Chrome
tests passed, 31 Worker tests passed; dependency audits and secret scan passed.
See `.spec/crawler-contract-repair/audit.md` for baseline evidence and substituted
boundaries. The PR carries hosted-check/delivery state; this entry does not imply
merge, deployment, or live verification of every source.
