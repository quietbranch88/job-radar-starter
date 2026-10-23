# Optional Cloudflare status sync

This isolated template is optional. Job Radar remains local-only by default.
It is for one developer synchronizing status values between devices they own;
it is not a shared account, hosted job service, or multi-user platform.

The Worker routes every request to one SQLite-backed Durable Object named
`owner:statuses`. Clients cannot choose an identity or storage object. The
Durable Object serializes reads, merges, and deletion so concurrent devices do
not silently overwrite unrelated status changes. Requests send only stable job
IDs and the four local status values; resumes, job descriptions, interview
notes, company preferences, and source credentials are not part of this
contract.

## Local use

Use Node.js 22.12+ on the 22.x line, or Node.js 24 or later (CI uses 24),
and the committed npm lockfile.
The tests use the maintained `@cloudflare/vitest-plugin` and its declared
Miniflare/workerd dependencies, without forced dependency overrides. The
upstream Miniflare version is currently a prerelease; local test and dry-run
results do not establish production deployment compatibility.

```powershell
npm ci
npm test
npx wrangler secret put SYNC_TOKEN
npm run dev
```

`wrangler dev --local` uses local Durable Object SQLite storage. The checked-in
binding and `new_sqlite_classes` migration intentionally have no account ID,
namespace ID, production domain, or route. Review generated deployment changes
before using `npm run deploy`.

## Retention and deletion

Durable Object storage retains the latest owner statuses until
`DELETE /v1/statuses`, manual Durable Object deletion, or account deletion.
Operators are responsible for their own retention policy, backup policy,
disclosure, and deletion verification. Cloudflare is a third party after sync
is enabled; review its terms, privacy, retention, subprocessor, and
regional-processing settings.

## Request bounds and abuse

Each POST is limited to 64 KiB, 500 statuses, 500 legacy keys, bounded key
lengths, strict JSON, and allowlisted values. The merged stored state is also
limited to 500 statuses; an overflowing update returns
`stored_state_limit_exceeded` without changing storage. These limits reduce
accidental or abusive writes without requiring a paid rate-limiting service.
If the Worker is exposed publicly, add an account-owned Cloudflare rate rule
appropriate to the operator's traffic and rotate the Bearer secret after
suspected disclosure.

Concurrent updates to different stable IDs are merged in serialized order.
Concurrent updates to the same stable ID are also serialized; the later
accepted update becomes the visible value, and each response contains the
canonical document after that request. Identical retries are idempotent. This
remains a single-developer template, not a multi-user conflict-resolution
service.
