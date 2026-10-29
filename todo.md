# Follow-up work

- Provider-wide cooldown and Retry-After handling: deferred design improvement,
  outside this repair; assess only if multi-board scale requires it.
- Live Ashby smoke: retry an authorized public board when HTTP 403 no longer
  blocks the read-only check. Synthetic contract tests already pass.

No further local code repair is pending for S1, S3 or S4. Hosted Windows checks,
review and merge are tracked in the GitHub PR.
