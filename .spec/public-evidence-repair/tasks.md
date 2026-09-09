# Public release evidence repair

## Scope

Restore the README contract and usable documentation navigation, then repair the current optional-sync development dependency audit so the existing public release gate can complete. Keep runtime behavior and release checks unchanged.

## Acceptance criteria

- [x] Reproduce the existing README contract failure before editing.
- [x] Explain how agents and scripts use the shared CLI without requiring a vendor.
- [x] Replace all eight nonexistent documentation paths with links to existing resources.
- [x] Verify compatible dependency fixes against the npm registry and update the lockfile.
- [x] Pass the existing full public release gate, including Python, browser flows, Worker tests, dependency audits, secret/privacy scans, and deployment dry-run.
- [x] Review the diff and record local verification separately from hosted CI or production deployment.

## Boundaries

- Work from an isolated branch based on the public default branch.
- No private profiles, resumes, scan output, credentials, or machine paths in the patch.
- No production deployment, automatic applications, or changes to user data.
- Do not weaken tests, audit thresholds, or the release gate.
