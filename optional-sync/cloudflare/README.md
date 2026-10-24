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
Miniflare/workerd versions. Two explicitly authorized, exact-version
overrides replace Miniflare's `undici` with 7.30.1 and the Vitest plugin's `zod`
with 4.6.6 for upstream dependency advisories. Wrangler, Miniflare, workerd and
Vitest versions are not changed or force-downgraded. Do not broaden these
overrides; recheck them when upgrading their pinned parent packages. The upstream
Miniflare version is currently a prerelease; local test and dry-run results do
not establish production deployment compatibility.

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

## Optional Dashboard client

The generated single-file Dashboard includes an **Optional status sync** panel.
It makes zero sync requests by default, including after changing browser
markers. Read the privacy and retention notice, opt in, and enter the Bearer
token for this tab. Enabling the panel itself makes no request. Only the manual
Pull and Push buttons contact the Worker; there is no polling or background
autopush. Disconnect or reload clears the token. The client never writes it
into browser storage, exports, URLs, generated HTML, or logs.

Serve the Dashboard and `/v1/statuses` through the **same HTTPS origin**, using
an operator-owned route or reverse proxy that forwards the Authorization
header without logging it. This Worker template does not serve the HTML by
itself. A file-opened Dashboard stays local-only. The client has no endpoint
input, rejects redirects, omits cookies and referrers, and cannot enable CORS.
HTTP is accepted only for the exact loopback hosts `localhost`, `127.0.0.1`,
and `[::1]`. Other HTTP origins are refused before token handoff or a request.
No account, route, or production configuration
is supplied by this repository.

- **Pull remote statuses** fetches and validates the entire document before
  presenting a preview. Choose **keep local conflicts** or **use remote
  conflicts** before applying it. Differing local values and locally cleared
  markers are conflicts. Unrelated local entries remain intact. Cancel leaves
  local state untouched. Any local edit invalidates an old preview or response.
  If another tab changes browser status storage, reload before using sync so
  that a stale tab cannot Push or save a Pull over the other tab's edits.
- **Push current local statuses** sends exactly
  `{contract_version: 1, statuses: {...}, legacy_keys: []}`. It includes only
  stable `job_` IDs and interested/applied/skip/dead markers, up to 500 items
  and 64 KiB in one request, without truncation. Legacy keys, invalid entries,
  and embedded CLI-confirmed statuses are omitted. Push does not import the
  response into local state. It replaces remote markers for the IDs sent;
  Pull first to review differences. There is no compare-and-swap guarantee
  between devices: concurrent same-ID updates still use the last accepted
  value.
- **Disconnect** clears this tab's token and invalidates outstanding responses.
  An in-flight Push may already have reached the Worker. Disconnect is not
  remote deletion. Clearing a local marker, omitting an ID, or pushing an empty
  set does not delete remote keys; a later Pull can restore them. This client
  offers no remote-delete action and never sends legacy-key deletion requests.

Remote `applied` is only a browser marker. It does not create a confirmed CLI
application, change the embedded application records/metrics, or override a
CLI-locked status. No resume, JD, profile, notes, receipt, or application record
is synchronized. Auth, offline, invalid-response, and browser-storage errors
preserve local state and display fixed, actionable errors. After an uncertain
Push, Pull to inspect the remote result before retrying.

## Reproduce local Worker + Chrome verification

From the repository root, install the root browser-test dependencies and the
optional Worker dependencies from their existing lockfiles, and use the
project Python environment. No dependency upgrade is needed:

```powershell
rtk npm ci
rtk npm --prefix optional-sync/cloudflare ci --ignore-scripts
$env:PLAYWRIGHT_CHANNEL = "chrome"
rtk npm run test:browser -- tests/browser/sync.spec.mjs --workers=1
```

`tests/browser/sync.spec.mjs` builds a synthetic Dashboard through the real
renderer/installer, starts the existing Worker via local Wrangler/workerd
with the checked-in Durable Object configuration, and uses a loopback HTTP
proxy for the fixed same-origin path. Its synthetic token is generated in
memory. Storage is ephemeral and isolated; setup resets only that test-owned
Durable Object. The harness has no remote binding or deployment step.

The Chrome tests exercise real authentication (including denial), conflict
choices, SQLite-backed writes, an independent browser context's read, CLI
locks, omission semantics, exact 500-item/64-KiB acceptance, offline recovery,
and 390px layout. Malformed-response, redirect, storage-quota, and delayed
response tests explicitly substitute those failure boundaries. These tests
establish the local chain, not a hosted Cloudflare deployment, backup deletion,
or cross-device reliability over a production network.
