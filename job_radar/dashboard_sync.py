"""Install an opt-in status client into the framework-free Dashboard HTML."""

_MARKER = 'data-dashboard-optional-sync="1"'
_SAVE = "function saveState(){localStorage.setItem(STORAGE_KEY,JSON.stringify(state))}"
_SAVE_WITH_NOTICE = (
    "function saveState(){try{localStorage.setItem(STORAGE_KEY,JSON.stringify(state))}"
    "finally{document.dispatchEvent(new Event('job-radar:statuses-saved'))}}"
)

_STYLE = """
    .optional-sync{margin:24px 0;padding:18px;border:1px solid var(--line);border-radius:12px}
    .optional-sync summary{cursor:pointer;font-weight:700;min-height:44px}
    .optional-sync p{max-width:85ch;color:var(--muted);font-size:.85rem}
    .optional-sync .sync-controls{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
    .optional-sync input[type=password]{max-width:100%;width:24em;box-sizing:border-box;min-height:44px}
    .optional-sync button{min-height:44px;white-space:normal}
    .optional-sync [hidden]{display:none!important}
    .optional-sync li{overflow-wrap:anywhere;font-size:.85rem}
    .optional-sync label{display:block;margin:12px 0;font-size:.85rem}
"""

_PANEL = """
    <details class="optional-sync" id="optionalSync" data-dashboard-optional-sync="1">
      <summary>Optional status sync (off by default)</summary>
      <p id="syncPrivacy">If enabled, Cloudflare is a third party receiving only stable job IDs and
      interested / applied / skip / dead browser markers. No resume, job description, notes,
      profile, or CLI application records are sent. Cloudflare Durable Object storage retains
      the latest statuses until the operator deletes them; review the operator's retention,
      backup, privacy, and regional-processing settings before enabling sync.</p>
      <p>Disconnect only clears this tab's token; disconnect is not remote deletion.
      Clearing a local status or omitting an ID from Push does not remove its remote status.
      This client cannot delete remote keys. A later Pull can restore an omitted status.
      Push replaces remote values for the IDs sent; concurrent same-ID writes use the last
      accepted value. Pull before Push to inspect remote differences.</p>
      <p>Sync uses only this site's <code>/v1/statuses</code>. Serve this page and the authenticated
      Worker through the same HTTPS origin (HTTP is allowed only on localhost, 127.0.0.1, or [::1]).
      The token stays in memory until disconnect or reload.
      Synced applied is an unconfirmed browser marker; use the local CLI to confirm a submission.
      CLI-confirmed statuses stay locked and are excluded from Push.</p>
      <div id="syncSetup">
        <label><input type="checkbox" id="syncConsent"> I understand the privacy and retention notice and opt in to manual sync.</label>
        <label for="syncToken">Worker Bearer token (this tab only)</label>
        <input id="syncToken" type="password" autocomplete="off" spellcheck="false" maxlength="4096" aria-describedby="syncPrivacy">
        <button class="utility" type="button" id="syncConnect">Enable manual sync</button>
      </div>
      <div class="sync-controls" id="syncActions" hidden>
        <button class="utility" type="button" id="syncPull">Pull remote statuses</button>
        <button class="utility" type="button" id="syncPush">Push current local statuses</button>
        <button class="utility" type="button" id="syncDisconnect">Disconnect</button>
      </div>
      <div id="syncPreview" hidden>
        <p id="syncPreviewSummary"></p><ul id="syncConflicts"></ul>
        <div class="sync-controls">
          <button class="utility" type="button" id="syncKeep">Apply Pull; keep local conflicts</button>
          <button class="utility" type="button" id="syncRemote">Apply Pull; use remote conflicts</button>
          <button class="utility" type="button" id="syncCancel">Cancel Pull</button>
        </div>
      </div>
      <p id="syncMessage" role="status" aria-live="polite">Sync is off. No sync requests have been made.</p>
    </details>
"""

_CLIENT = r"""
    // Explicit lexical hooks: no application records or credentials enter dashboard state.
    (function installOptionalStatusClient(hooks) {
      const nodes = Object.fromEntries([
        'Setup', 'Consent', 'Token', 'Connect', 'Actions', 'Pull', 'Push',
        'Disconnect', 'Preview', 'PreviewSummary', 'Conflicts', 'Keep', 'Remote',
        'Cancel', 'Message',
      ].map(name => [name, document.getElementById('sync' + name)]));
      const allowed = new Set(['interested', 'applied', 'skip', 'dead']);
      const stableKey = /^job_[a-z0-9_-]{8,128}$/;
      const MAX_ITEMS = 500, MAX_BYTES = 65536, MAX_RESPONSE_BYTES = 81920;
      const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
      const record = value => value !== null && typeof value === 'object' && !Array.isArray(value);
      const eligible = (key, value) => key.length <= 128 && stableKey.test(key) && allowed.has(value);
      const secureOrigin = location.protocol === 'https:' || (location.protocol === 'http:'
        && ['localhost', '127.0.0.1', '[::1]'].includes(location.hostname));
      let token = '', epoch = 0, revision = 0, active = null, pending = null, applying = false, externalEdit = false;
      let lastLocal = hooks.snapshot();
      const edited = new Set();

      function message(value) { nodes.Message.textContent = value; }
      function refresh() {
        nodes.Setup.hidden = Boolean(token);
        nodes.Actions.hidden = !token;
        nodes.Connect.disabled = Boolean(active);
        nodes.Pull.disabled = Boolean(active || pending);
        nodes.Push.disabled = Boolean(active || pending);
        nodes.Preview.hidden = !pending;
        nodes.Token.disabled = !secureOrigin;
      }
      function discardPreview() {
        pending = null;
        nodes.Conflicts.replaceChildren();
        nodes.PreviewSummary.textContent = '';
      }
      function disconnect() {
        epoch += 1;
        token = '';
        nodes.Token.value = '';
        nodes.Consent.checked = false;
        discardPreview();
        active?.controller.abort();
        message('Sync is off; token cleared. Remote data was not deleted. An in-flight Push may already have reached the Worker.');
        refresh();
      }
      hooks.subscribe(event => {
        if (applying) return;
        if (event.type === 'storage') externalEdit = true;
        const next = hooks.snapshot();
        for (const key of new Set([...Object.keys(lastLocal), ...Object.keys(next)])) {
          if (lastLocal[key] !== next[key]) edited.add(key);
        }
        lastLocal = next;
        revision += 1;
        const interrupted = Boolean(active || pending);
        discardPreview();
        active?.controller.abort();
        if (externalEdit) message('Another tab changed local statuses. Sync is paused: reload this page to load those edits before Pull or Push. An in-flight Push may already have reached the Worker.');
        else if (interrupted) message('Local statuses changed. The old Pull/Push result was discarded; Pull again to review. An in-flight Push may already have reached the Worker.');
        refresh();
      });

      function documentStatuses(value) {
        if (!record(value) || Object.keys(value).sort().join(',') !== 'contract_version,statuses'
            || value.contract_version !== 1 || !record(value.statuses)) throw new Error('invalid');
        const entries = Object.entries(value.statuses);
        if (entries.length > MAX_ITEMS || !entries.every(([key, status]) => eligible(key, status))) throw new Error('invalid');
        return Object.fromEntries(entries);
      }
      async function readDocument(response) {
        const contentType = response.headers.get('content-type')?.split(';')[0].trim();
        if (contentType !== 'application/json' || !response.body) throw new Error('invalid');
        const reader = response.body.getReader(), chunks = [];
        let length = 0;
        try {
          for (;;) {
            const { value, done } = await reader.read();
            if (done) break;
            length += value.byteLength;
            if (length > MAX_RESPONSE_BYTES) throw new Error('invalid');
            chunks.push(value);
          }
        } finally { await reader.cancel().catch(() => {}); }
        const bytes = new Uint8Array(length);
        let offset = 0;
        for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
        try { return documentStatuses(JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes))); }
        catch { throw new Error('invalid'); }
      }
      function pushPayload() {
        const locked = hooks.locked(), snapshot = hooks.snapshot();
        const entries = Object.entries(snapshot).filter(([key, status]) => !own(locked, key) && eligible(key, status));
        if (entries.length > MAX_ITEMS) throw new Error('bounds');
        const statuses = Object.fromEntries(entries);
        const body = JSON.stringify({ contract_version: 1, statuses, legacy_keys: [] });
        if (new TextEncoder().encode(body).byteLength > MAX_BYTES) throw new Error('bounds');
        return { body, statuses, omitted: Object.keys(snapshot).length - entries.length };
      }
      const current = operation => token && operation.epoch === epoch && operation.revision === revision;
      function responseError(status) {
        if (status === 401 || status === 403) return 'Authentication failed. Disconnect, check the Worker token, and enable sync again.';
        if (status === 404) return 'Worker route unavailable. Ask the operator to serve /v1/statuses on this exact origin.';
        if (status === 409) return 'Remote storage limit reached. Ask the operator to review retained statuses; local statuses are unchanged.';
        if (status === 413) return 'Worker rejected the payload size. Reduce the eligible local status set before retrying.';
        return 'Worker unavailable. Check the same-origin route and Worker configuration, then retry manually.';
      }
      async function run(method) {
        if (!token || active || pending) return;
        if (externalEdit) { message('Another tab changed local statuses. Reload this page before Pull or Push; no request was made.'); return; }
        let payload;
        if (method === 'POST') {
          try { payload = pushPayload(); }
          catch { message('Push stopped: at most 500 eligible statuses and 64 KiB are allowed. Nothing was sent; reduce the local status set.'); return; }
        }
        const operation = { epoch, revision, method, controller: new AbortController() };
        active = operation;
        refresh();
        message(method === 'GET' ? 'Pulling remote statuses for review...' : 'Pushing the current eligible local statuses...');
        const timeout = setTimeout(() => operation.controller.abort(), 15000);
        try {
          const response = await fetch('/v1/statuses', {
            method, mode: 'same-origin', credentials: 'omit', redirect: 'error',
            cache: 'no-store', referrerPolicy: 'no-referrer', signal: operation.controller.signal,
            headers: { authorization: 'Bearer ' + token, ...(payload ? { 'content-type': 'application/json' } : {}) },
            ...(payload ? { body: payload.body } : {}),
          });
          if (!current(operation)) return;
          if (response.status !== 200) {
            message(responseError(response.status) + (payload ? ' Push may have reached the Worker; Pull before retrying.' : ' Local statuses are unchanged.'));
            return;
          }
          const remote = await readDocument(response);
          if (!current(operation)) return;
          if (payload) {
            if (!Object.entries(payload.statuses).every(([key, value]) => remote[key] === value)) throw new Error('invalid');
            for (const key of Object.keys(payload.statuses)) edited.delete(key);
            message(`Push confirmed for ${Object.keys(payload.statuses).length} browser markers; ${payload.omitted} ineligible or CLI-locked entries omitted. Local statuses were not changed. Omitted/cleared IDs remain remote.`);
          } else {
            const local = hooks.snapshot(), locked = hooks.locked();
            const conflicts = Object.entries(remote).filter(([key, value]) => !own(locked, key)
              && (own(local, key) || edited.has(key)) && local[key] !== value);
            pending = { remote, conflicts: new Set(conflicts.map(([key]) => key)), epoch, revision };
            nodes.PreviewSummary.textContent = `Review ${Object.keys(remote).length} remote markers: ${conflicts.length} local conflicts. CLI-locked statuses will be skipped. Unrelated local state will be kept.`;
            nodes.Conflicts.replaceChildren(...conflicts.map(([key, value]) => {
              const item = document.createElement('li');
              item.textContent = `${key}: local ${local[key] || '(cleared locally)'}; remote ${value}`;
              return item;
            }));
            message('Pull ready for review. Choose whether to keep local conflicts or use remote conflicts. No local statuses changed yet.');
          }
        } catch (error) {
          if (current(operation)) message((error.message === 'invalid'
            ? 'Invalid Worker response. Check the v1 status contract with the operator; local statuses are unchanged.'
            : 'Cannot reach the same-origin Worker (offline, timeout, or blocked route). Check the connection and retry manually; local statuses are unchanged.')
            + (payload ? ' Push may have reached the Worker; Pull before retrying.' : ''));
        } finally {
          clearTimeout(timeout);
          if (active === operation) active = null;
          refresh();
        }
      }
      function applyPull(useRemote) {
        if (!pending || active || externalEdit || pending.epoch !== epoch || pending.revision !== revision || !token) return;
        const locked = hooks.locked();
        const updates = Object.fromEntries(Object.entries(pending.remote).filter(([key]) => !own(locked, key)
          && (useRemote || !pending.conflicts.has(key))));
        applying = true;
        try {
          hooks.apply(updates);
          for (const key of Object.keys(updates)) edited.delete(key);
          lastLocal = hooks.snapshot();
          revision += 1;
          discardPreview();
          message(`Pull applied: ${Object.keys(updates).length} browser markers merged; ${useRemote ? 'remote' : 'local'} conflicts chosen. Unrelated local state kept. Applied markers do not confirm applications.`);
        } catch {
          message('Browser storage could not save the Pull. Local statuses were restored. Free browser storage or export local state, then retry.');
        } finally { applying = false; refresh(); }
      }
      nodes.Connect.addEventListener('click', () => {
        if (active) return;
        if (!secureOrigin) {
          message('Sync requires HTTPS, or HTTP on localhost, 127.0.0.1, or [::1], with a same-origin /v1/statuses Worker. File dashboards remain local-only.'); return;
        }
        if (!nodes.Consent.checked || !nodes.Token.value || /[\u0000-\u001f\u007f]/.test(nodes.Token.value)) {
          message('Read and accept the privacy notice, then enter the Worker Bearer token.'); return;
        }
        epoch += 1;
        token = nodes.Token.value;
        nodes.Token.value = '';
        message('Manual sync enabled. No request made yet. Choose Pull or Push; there is no background sync.');
        refresh();
      });
      nodes.Pull.addEventListener('click', () => run('GET'));
      nodes.Push.addEventListener('click', () => run('POST'));
      nodes.Disconnect.addEventListener('click', disconnect);
      nodes.Keep.addEventListener('click', () => applyPull(false));
      nodes.Remote.addEventListener('click', () => applyPull(true));
      nodes.Cancel.addEventListener('click', () => { discardPreview(); message('Pull cancelled. Local statuses unchanged.'); refresh(); });
      window.addEventListener('pagehide', disconnect);
      refresh();
    })({
      snapshot: () => ({ ...state }),
      locked: () => ({ ...DASHBOARD.tracking.statuses }),
      subscribe: listener => {
        document.addEventListener('job-radar:statuses-saved', listener);
        window.addEventListener('storage', event => {
          if (event.storageArea === localStorage && (event.key === STORAGE_KEY || event.key === null)) listener(event);
        });
      },
      apply: updates => {
        const before = { ...state };
        for (const [key, value] of Object.entries(updates)) {
          if (!Object.prototype.hasOwnProperty.call(DASHBOARD.tracking.statuses, key)) state[key] = value;
        }
        try { saveState(); }
        catch (error) {
          for (const key of Object.keys(state)) delete state[key];
          Object.assign(state, before);
          throw error;
        }
        renderAll();
      },
    });
"""


def install_optional_sync(html: str) -> str:
    """Add manual sync UI and explicit hooks to a supported single-file Dashboard.

    Repeated installation is harmless. Unknown templates fail closed instead of
    silently generating a client without the local-edit or CLI-lock safeguards.
    The renderer remains responsible for calling this installer once.
    """
    if _MARKER in html:
        return html
    script_start = "  <script>\n    const DASHBOARD="
    panel_anchor = '    <section class="recommendations">'
    for anchor in (script_start, _SAVE, panel_anchor, "  </style>"):
        if html.count(anchor) != 1:
            raise ValueError("unsupported Dashboard template for optional status sync")
    start = html.index(script_start)
    end = html.find("\n  </script>", start)
    if end == -1 or not start < html.index(_SAVE) < end:
        raise ValueError("missing Dashboard lexical status hooks")
    html = html[:end] + _CLIENT + html[end:]
    html = html.replace(_SAVE, _SAVE_WITH_NOTICE, 1)
    html = html.replace("  </style>", _STYLE + "  </style>", 1)
    html = html.replace(panel_anchor, _PANEL + panel_anchor, 1)
    return html.replace(
        "No applicant data is sent by this page.",
        "Optional sync sends only stable job IDs and browser status markers after opt-in.",
    )
