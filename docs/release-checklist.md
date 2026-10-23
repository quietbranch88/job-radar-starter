# Public release checklist

Run `scripts/verify-public-release.ps1` from a clean checkout. Record the exact
commit and command output.

Before a real publication, inject maintainer-specific names, handles, hosts,
and local path prefixes from an uncommitted environment value. Separate values
with semicolons; do not add the real list to this repository:

```powershell
$env:JOB_RADAR_PRIVATE_MARKERS = "maintainer-name;maintainer-handle;private-host.invalid;C:\Users\maintainer"
$env:JOB_RADAR_PRIVACY_ALLOWED_PATH_LINES = "LICENSE::Copyright (c) 2026 maintainer-name"
.\scripts\verify-public-release.ps1
```

Allowed values use `relative/path::exact complete line`. Do not use patterns or
partial lines; the allowlist is case-sensitive and exact.

- Full Python suite passes.
- Dashboard build succeeds from the invented example data.
- Worker tests and typecheck pass.
- Browser tests pass against the generated dashboard.
- Secret scan passes with the pinned Gitleaks executable.
- Dependency audit passes for the dashboard and optional Worker packages.
- Cloudflare Worker dry-run packaging succeeds; this is not a production deploy.
- The committed dashboard is privacy-scanned before generation; temporary
  generated artifact inspection proves a byte-for-byte match and the expected
  dashboard contract.
- `git diff --exit-code -- dashboard/public/index.html` passes.
- `git diff --check` passes.
- Manual review confirms source policy, privacy boundaries, example data,
  links, responsive layout, loading state, partial state, and the happy path.

Keep the evidence with the release record. A green local checklist proves the
tested commit only; it does not prove external provider availability or a live
deployment.

For source health, Sheets outbox or review-receipt changes, also record exact
dirty-file hashes, retry/readback/conflict and version-mismatch tests, optional
state export/deletion/active-writer checks, and independent review. Real source
checks must name the configured boards and UTC fetch times. A synthetic Google
test must name its isolated target and assert final row count/content after a
controlled uncertain acknowledgement. State any connector/HTTP substitution and
unverified credential path; never use a job master as a test target. Keep private
verification records and provider payloads out of published artifacts.
