# Public release repair evidence

## Baseline

- Public base: `821d9b110e28beacac0e16543e509978ce58c2c8`.
- Historical hosted release gate run `30061979763` failed because README omitted the existing agent-neutral contract. The same focused test failed locally on 2026-09-09 at `tests/test_public_release_contract.py:33`, missing `claude`.
- README also listed eight nonexistent documentation paths. Existing operator, policy, contract, and sync documentation covers these topics.
- A fresh optional-sync dependency installation on 2026-09-09 reported 9 audit findings (3 moderate, 6 high). This is a current dependency blocker discovered during repair, separate from the historical README failure.
- The CI Python lock explicitly targets Python 3.11 / Windows x64. A Python 3.12 wheel was correctly rejected by the hash gate; using Python 3.11 installed the unchanged hash-locked dependencies successfully.

## Dependency verification

Registry metadata was checked on 2026-09-09 before installing the changed versions. These are existing package identities from their established upstream projects; no new direct dependency was added.

| Package | Previous | Selected | Evidence and reason |
| --- | --- | --- | --- |
| `@cloudflare/vitest-pool-workers` | 0.18.5 | 0.18.8 | npm registry confirms Cloudflare workers-sdk ownership and this version's Wrangler / Miniflare dependency pair |
| `wrangler` | 4.111.0 | 4.114.0 | Matches the pool adapter's exact Wrangler dependency |
| `@cloudflare/workers-types` | 5.20260716.1 | 5.20260722.1 | Minimum date version satisfying Wrangler's `^5.20260722.1` peer requirement |
| `vitest` | 4.1.10 | 4.1.11 | Fixes [GHSA-82fw-gwwq-j7x9](https://github.com/advisories/GHSA-82fw-gwwq-j7x9) on the same release line |
| `sharp` via Miniflare | 0.34.5 | 0.35.4 | Fixes the current [libheif advisory](https://github.com/advisories/GHSA-rgj7-g3m4-5g8c); the newer Miniflare still pins 0.35.2, so a scoped override is required |
| `undici` via Miniflare | 7.28.0 | 7.29.0 | Fixes the current Undici advisories, including [GHSA-4cwx-7wf7-3272](https://github.com/advisories/GHSA-4cwx-7wf7-3272); the newer Miniflare still pins 7.28.0 |

The `sharp` and `undici` overrides apply only under `miniflare`. Remove them when the selected upstream Miniflare uses fixed versions and the full gate remains green. npm's subsequent force-fix suggestion downgraded Cloudflare tooling; it was not used. No `--force`, peer-dependency bypass, audit suppression, or threshold reduction was used.

The lockfile also refreshes the compatible Vitest / Vite dependency graph, including patched `nanoid` and `postcss`, and platform-specific Sharp / Workerd binaries. `npm ls` confirms a single deduplicated Miniflare / Wrangler pair and the selected overrides.

Health signals: npm weekly package downloads were approximately 2.37M (pool adapter), 19.03M (Wrangler), 9.03M (workers-types), 92.68M (Vitest), 69.64M (Sharp), and 158.54M (Undici). All had registry updates in August or September 2026 and MIT, Apache-2.0, or dual licensing. Cloudflare's publication account and Sharp each list a single maintainer; their established upstream repositories and package histories identify them as the original packages. These are registry signals, not proof that dependencies cannot contain vulnerabilities.

## Local verification

- Original README test: failed before the change; passed after the change (`1 passed`).
- README link check: 12 local Markdown links, zero missing targets.
- Updated optional-sync installation: audit changed from 9 findings to zero.
- Full-gate attempt encountered a local Windows Store Python permission error in the shared pytest temporary directory. The retry uses an isolated ignored `test-results/pytest-temp` root; no repository behavior changed.
- Another attempt had the unchanged historical rejected-audit benchmark take 5.47 seconds against its 5-second limit under concurrent machine load. The focused rerun passed without changing the limit (`2 passed in 2.42s`, including the updated dependency contract).
- Local runtime: Python 3.11.9, Node 25.0.0, npm 11.6.2. Hosted CI separately uses Python 3.11 and Node 24.

The complete final `scripts/verify-public-release.ps1` run exited **0** on 2026-09-09 after using the isolated temporary root. The output and exit code are retained locally in ignored `test-results/public-release-gate-final.log` and `test-results/public-release-gate-final.exitcode`.

| Gate stage | Final result |
| --- | --- |
| Gitleaks 8.30.1 | No leaks found |
| Tracked privacy scan | Passed for 111 tracked files, including the new spec |
| End-to-end fake-source CLI workflow | 1 passed |
| Full Python suite | 291 passed, 1 skipped in 55.64 seconds; the skip requires POSIX SIGTERM semantics |
| Example dashboard build and hash comparison | Passed; committed dashboard unchanged and exactly reproducible |
| Dashboard dependency audit | Zero vulnerabilities |
| Chromium browser flows | 3 passed in 7.2 seconds: desktop persistence/export, mobile 390px, bounded large-scan rendering |
| Optional-sync dependency audit | Zero vulnerabilities |
| Worker TypeScript check | Passed |
| Worker behavior tests | 31 passed across 2 files in 2.88 seconds |
| Wrangler deployment dry-run | Passed; 6.49 KiB package, no deployment |
| Whitespace | `git diff --check` and staged diff check passed |

The mobile test screenshot was visually inspected. Runtime source, generated dashboard, source policy, schemas, deployed configuration, and data-retention behavior were not changed. The retained local result does not establish hosted CI status, production deployment, external provider availability, or real-user adoption.
