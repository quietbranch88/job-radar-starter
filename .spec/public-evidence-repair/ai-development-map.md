# Development map

| Area | Change | Verification |
| --- | --- | --- |
| README | Agent-neutral usage, real documentation links, fenced PowerShell examples and local directory initialization | Existing release-contract test; local Markdown link check |
| Optional sync development tooling | Minimum compatible audited package and lockfile updates | Registry verification, npm audit, typecheck, Worker behavior tests, deployment dry-run |
| Release evidence | Record the historical failure, current blockers, and exact local verification | Existing full public release gate and diff review |

Runtime modules, source policy, private data boundaries, and deployment configuration remain outside this patch.
