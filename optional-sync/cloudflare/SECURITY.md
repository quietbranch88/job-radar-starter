# Security and responsibility notes

- Authentication fails closed when the Bearer secret or Durable Object binding
  is absent.
- The Worker uses a fixed single developer owner key and does not trust a
  client-provided identity.
- Responses use `Cache-Control: private, no-store`; CORS is closed by default.
- Errors are fixed codes and do not return secrets, internal exceptions, or
  stack traces.
- POST accepts at most 64 KiB and 500 bounded status items. The SQLite-backed
  Durable Object also rejects a merge that would store more than 500 statuses.
  Consider Cloudflare rate limiting and abuse monitoring before exposing a
  public route.
- `DELETE /v1/statuses` is the explicit data deletion path. Retention,
  third-party sharing, backup removal, and account deletion remain the
  operator's responsibility.
- One fixed-owner Durable Object serializes concurrent device merges and stores
  state in SQLite. It prevents unrelated updates from being silently lost, but
  it is not shared-tenancy conflict resolution. Do not use this template for
  payments, authorization decisions, or multi-user state.
- Finding this template does not grant authorization to synchronize another
  person's data. Operators remain responsible for privacy disclosure, terms,
  applicable law, and independent legal review; this documentation is not
  legal advice.

## Dashboard client boundary

- Sync is off by default. The privacy/Cloudflare retention disclosure and an
  explicit checkbox/token action precede any manual request. There is no
  background upload. Only same-origin `/v1/statuses` is used; no endpoint or
  identity can be selected. CORS stays closed, redirects fail, and cookies and
  referrers are omitted.
- The token lives in a private client closure, is removed from its password
  input on enable, and is cleared on disconnect or page exit/reload. It is not
  part of Dashboard state, exports, storage, generated HTML, or application
  logs. Operators must prevent Authorization-header logging at any proxy or
  hosting layer. The client requires HTTPS; HTTP is allowed only on the exact
  loopback hosts localhost, 127.0.0.1, and [::1]. Other HTTP origins and file
  pages are refused before token handoff or a request. Memory-only storage is
  not a defense against a compromised page, extension, browser, or device.
- POST always uses an empty `legacy_keys` list. Only valid stable IDs and four
  allowlisted browser marker values are sent; the entire eligible request is
  rejected locally above 500 items or 64 KiB. Pull validates the full v1
  document, item/key/value bounds, JSON content type and bounded body before
  offering a merge. Raw server errors are never shown.
- CLI-confirmed statuses are locked, omitted from Push, and never changed by
  Pull. A remote `applied` marker has no confirmation authority and does not
  change CLI application metrics. Unrelated local data is retained, and Pull
  conflicts (including local clears) require a visible choice.
- Operations are serialized. Status-save hooks invalidate pending previews and
  responses after local edits; session guards reject replies from before
  disconnect. An external browser-storage change blocks sync until reload,
  preventing a stale tab from overwriting another tab's stored edits through
  sync. A cancelled Push may already have committed remotely: inspect
  via Pull before retrying. The Worker contract has no revision/CAS facility,
  so a preview cannot lock another device's future writes.
- There is no remote deletion UI. Local clear, omission, empty Push and
  disconnect do not remove remote keys. Retention, backup removal and
  operator-authorized deletion remain separate responsibilities.
