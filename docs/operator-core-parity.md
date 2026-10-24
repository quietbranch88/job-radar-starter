# Operator core compatibility

`contracts/operator-core.json` records the reviewed compatibility baseline for
four small copied operator modules. The normalizer permits LF/CRLF differences
and only the local-state import namespace in `review_receipts.py` and
`sheet_sync.py`. It does not ignore implementation differences, comments or
additional imports.

```powershell
python scripts/check_operator_core.py
python scripts/check_operator_core.py --peer-root PATH_TO_OTHER_CHECKOUT
```

The first command checks the current implementation against the pinned baseline.
The second also requires both manifests and all four normalized implementations
to agree. Supported peer layouts are `job_radar/` and
`job-scan/operator_state/`. No network, credentials, operational job data or
absolute machine paths are needed. Tests run under either unittest or pytest and
include a controlled reject-tie-break mutation that must be detected.

This is a drift alarm, not a shared package or proof of correctness. A legitimate
core update requires reviewing the matching tests and both implementations before
updating both baselines. Each repository's normal tests check its local baseline;
cross-checkout comparison is an explicit verification command, not an assertion
that hosted CI automatically has access to a private peer repository.
