import { execFileSync } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { test, expect } from '@playwright/test';

// Real local Worker + SQLite DO, behind a loopback-only same-origin HTTP route.
// No remote bindings, credentials, account, persisted store, or deployed traffic.
const root = process.cwd();
const requireWorker = createRequire(path.join(root, 'optional-sync/cloudflare/package.json'));
const { unstable_dev } = requireWorker('wrangler');
const python = process.env.PYTHON || (existsSync(path.join(root, '.venv/Scripts/python.exe'))
  ? path.join(root, '.venv/Scripts/python.exe') : (process.platform === 'win32' ? 'python' : 'python3'));
const fixture = path.join(root, 'test-results', 'sync-fixture.html');
const A = 'job_aaaaaaaa', B = 'job_bbbbbbbb', LOCK = 'job_cccccccc', OTHER = 'job_localonly';
const token = randomUUID(); // Synthetic, memory-only, never printed or put on a command line.
let worker, server, origin;
let requests = [];
const startedAt = new Date().toISOString(), outcomes = [];

test.describe.configure({ mode: 'serial' });

test.beforeAll(async () => {
  test.setTimeout(60000);
  mkdirSync(path.dirname(fixture), { recursive: true });
  execFileSync(python, ['-c', `
from copy import deepcopy
from pathlib import Path
import os, runpy, sys
import job_radar.dashboard_sync as sync
from job_radar.dashboard import render_dashboard_view_model
model = deepcopy(runpy.run_path('tests/browser/build_dashboard_fixture.py')['MODEL'])
for item, stable in zip(model['jobs'], ['job_aaaaaaaa', 'job_bbbbbbbb', 'job_cccccccc']):
    item['stable_id'] = stable
    item['external_id'] = stable[4:]
    item['legacy_status_keys'] = []
model['tracking']['statuses'] = {'job_cccccccc': 'applied'}
model['review']['rejected'] = []
model['review']['sampled_rejected_ids'] = []
if os.environ.get('JOB_RADAR_SYNC_MUTATION') == 'push-cli-lock':
    original = '!own(locked, key) && eligible(key, status)'
    assert sync._CLIENT.count(original) == 1
    sync._CLIENT = sync._CLIENT.replace(original, 'eligible(key, status)', 1)
output = Path(sys.argv[1])
render_dashboard_view_model(model, output)
output.write_text(sync.install_optional_sync(output.read_text(encoding='utf-8')), encoding='utf-8')
`, fixture], { cwd: root, env: { ...process.env, PYTHONPATH: root }, stdio: 'pipe' });
  worker = await unstable_dev(path.join(root, 'optional-sync/cloudflare/src/index.ts'), {
    config: path.join(root, 'optional-sync/cloudflare/wrangler.jsonc'),
    local: true, persist: false, envFiles: [], vars: { SYNC_TOKEN: token },
    ip: '127.0.0.1', port: 0, logLevel: 'none', inspect: false,
    experimental: { forceLocal: true, disableDevRegistry: true, watch: false },
  });
  server = http.createServer(async (req, res) => {
    try {
      if (req.url === '/v1/statuses') {
        const chunks = [];
        for await (const chunk of req) chunks.push(chunk);
        const body = Buffer.concat(chunks);
        // Capture only synthetic status payloads and safe policy flags, never headers/tokens.
        requests.push({ method: req.method, bytes: body.length,
          body: body.length ? JSON.parse(body.toString('utf8')) : undefined,
          authenticated: req.headers.authorization === `Bearer ${token}`,
          query: false, referrer: Boolean(req.headers.referer) });
        const response = await worker.fetch('/v1/statuses', {
          method: req.method,
          headers: { authorization: req.headers.authorization || '',
            ...(req.headers['content-type'] ? { 'content-type': req.headers['content-type'] } : {}) },
          ...(body.length ? { body } : {}),
        });
        // arrayBuffer is decoded; do not forward upstream transport/compression headers.
        const headers = Object.fromEntries(response.headers);
        for (const key of ['content-encoding', 'content-length', 'transfer-encoding', 'connection']) delete headers[key];
        res.writeHead(response.status, headers);
        res.end(Buffer.from(await response.arrayBuffer()));
      } else if (req.url === '/' || req.url === '/index.html') {
        res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' });
        res.end(readFileSync(fixture));
      } else { res.writeHead(404); res.end(); }
    } catch { res.writeHead(500); res.end('test harness failed'); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  origin = `http://127.0.0.1:${server.address().port}`;
});

test.afterEach(async ({}, info) => {
  outcomes.push({ title: info.title, status: info.status, expected: info.expectedStatus });
});

test.afterAll(async ({ browser }) => {
  const hashes = Object.fromEntries(['job_radar/dashboard_sync.py', 'job_radar/dashboard.py',
    'tests/browser/sync.spec.mjs', 'optional-sync/cloudflare/src/index.ts',
    'optional-sync/cloudflare/wrangler.jsonc', 'test-results/sync-fixture.html']
    .map(file => [file, createHash('sha256').update(readFileSync(path.join(root, file))).digest('hex')]));
  const reportName = process.env.JOB_RADAR_SYNC_MUTATION === 'push-cli-lock' ? 'sync-mutation-run.json' : 'sync-run.json';
  writeFileSync(path.join(root, 'test-results', reportName), JSON.stringify({
    startedAt, finishedAt: new Date().toISOString(), url: origin, node: process.version,
    browser: browser.version(), channel: process.env.PLAYWRIGHT_CHANNEL || 'bundled-chromium',
    mutation: process.env.JOB_RADAR_SYNC_MUTATION || null, hashes, outcomes,
  }, null, 2));
  if (server) await new Promise(resolve => { server.closeAllConnections(); server.close(resolve); });
  await worker?.stop();
});

async function api(method, statuses) {
  return worker.fetch('/v1/statuses', {
    method, headers: { authorization: `Bearer ${token}`, ...(statuses ? { 'content-type': 'application/json' } : {}) },
    ...(statuses ? { body: JSON.stringify({ contract_version: 1, statuses, legacy_keys: [] }) } : {}),
  });
}
async function stored() { return (await (await api('GET')).json()).statuses; }
async function local(page) { return page.evaluate(() => JSON.parse(localStorage.getItem('job-radar-status') || '{}')); }
async function open(page, statuses = {}) {
  await page.goto(origin);
  await page.evaluate(value => localStorage.setItem('job-radar-status', JSON.stringify(value)), statuses);
  await page.reload();
  await page.locator('#optionalSync summary').click();
}
async function enable(page, secret = token) {
  await page.locator('#syncConsent').check();
  await page.locator('#syncToken').fill(secret);
  await page.locator('#syncConnect').click();
  await expect(page.locator('#syncMessage')).toContainText('Manual sync enabled');
}
async function pull(page, remoteChoice = false) {
  await page.locator('#syncPull').click();
  await expect(page.locator('#syncPreview')).toBeVisible();
  await page.locator(remoteChoice ? '#syncRemote' : '#syncKeep').click();
  await expect(page.locator('#syncMessage')).toContainText('Pull applied');
}

test.beforeEach(async () => {
  // Only this ephemeral test-owned DO is cleared. The UI has no deletion action.
  expect((await api('DELETE')).status).toBe(204);
  requests = [];
});

test('off by default; informed opt-in and token remain local until a manual action', async ({ page }) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await open(page);
  await expect(page.locator('#syncPrivacy')).toContainText('Cloudflare');
  await expect(page.locator('#optionalSync')).toContainText('disconnect is not remote deletion');
  await page.locator('#syncConnect').click();
  await expect(page.locator('#syncMessage')).toContainText('Read and accept');
  await enable(page);
  await page.locator(`[data-key="${A}"] [data-state=interested]`).click();
  expect(requests).toEqual([]);
  expect(await page.locator('#syncToken').inputValue()).toBe('');
  const download = page.waitForEvent('download');
  await page.getByTestId('export-state').click();
  const destination = path.join(root, 'test-results', 'sync-state-export.json');
  await (await download).saveAs(destination);
  expect(readFileSync(destination, 'utf8')).not.toContain(token);
  expect(await page.content()).not.toContain(token);
  expect(page.url()).not.toContain(token);
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toContain(token);
  await page.locator('#syncDisconnect').click();
  await expect(page.locator('#syncSetup')).toBeVisible();
  await expect(page.locator('#syncToken')).toHaveValue('');
  await expect(page.locator('#syncConsent')).not.toBeChecked();
  await enable(page);
  await page.reload();
  await expect(page.locator('#syncActions')).toBeHidden();
  expect(requests).toEqual([]);
  expect(errors).toEqual([]);
});

test('real Worker auth, conflict choices, CLI locks, schema, and independent-device durable read', async ({ page, browser }) => {
  const REMOTE_ONLY = 'job_remotely';
  expect((await api('POST', { [A]: 'applied', [B]: 'dead', [LOCK]: 'skip', [REMOTE_ONLY]: 'interested' })).status).toBe(200);
  await open(page, { [A]: 'interested', [OTHER]: 'skip', legacy: 'applied', notes: 'synthetic-note' });
  await enable(page);
  await page.locator('#syncPull').click();
  await expect(page.locator('#syncConflicts')).toContainText(`${A}: local interested; remote applied`);
  expect((await local(page))[A]).toBe('interested');
  await page.locator('#syncKeep').click();
  expect(await local(page)).toMatchObject({ [A]: 'interested', [B]: 'dead', [LOCK]: 'applied', [OTHER]: 'skip', legacy: 'applied', notes: 'synthetic-note' });
  await pull(page, true);
  await expect(page.locator(`[data-key="${A}"]`)).toHaveAttribute('data-status', 'applied');
  await expect(page.getByTestId('tracking-summary')).toContainText('4 tracked applications');
  await expect(page.locator(`[data-key="${LOCK}"] [data-state=skip]`)).toBeDisabled();
  const embedded = await page.locator('#dashboard-data').textContent();
  expect(JSON.parse(embedded).tracking.statuses).toEqual({ [LOCK]: 'applied' });
  await page.locator(`[data-key="${A}"] [data-state=interested]`).click();
  await page.locator('#syncPush').click();
  await expect(page.locator('#syncMessage')).toContainText('Push confirmed');
  const posts = requests.filter(item => item.method === 'POST');
  expect(posts).toHaveLength(1);
  expect(posts[0].body).toEqual({ contract_version: 1,
    statuses: { [A]: 'interested', [B]: 'dead', [OTHER]: 'skip', [REMOTE_ONLY]: 'interested' }, legacy_keys: [] });
  expect(posts[0].authenticated).toBe(true);
  expect(posts[0].referrer).toBe(false);
  const independent = await browser.newContext();
  const response = await independent.request.get(`${origin}/v1/statuses`, { headers: { authorization: `Bearer ${token}` } });
  expect(response.status()).toBe(200);
  expect(response.headers()['access-control-allow-origin']).toBeUndefined();
  expect((await response.json()).statuses).toEqual({ [A]: 'interested', [B]: 'dead', [LOCK]: 'skip', [OTHER]: 'skip', [REMOTE_ONLY]: 'interested' });
  await independent.close();
});

test('local clearing and empty Push never pretend to delete remote state', async ({ page }) => {
  await api('POST', { [A]: 'interested' });
  await open(page, { [A]: 'interested' });
  await enable(page);
  await page.locator(`[data-key="${A}"] [data-state=interested]`).click();
  await page.locator('#syncPush').click();
  await expect(page.locator('#syncMessage')).toContainText('Omitted/cleared IDs remain remote');
  expect(requests.find(item => item.method === 'POST').body).toEqual({ contract_version: 1, statuses: {}, legacy_keys: [] });
  expect(await stored()).toEqual({ [A]: 'interested' });
  await page.locator('#syncPull').click();
  await expect(page.locator('#syncConflicts')).toContainText('(cleared locally)');
  await page.locator('#syncKeep').click();
  expect((await local(page))[A]).toBeUndefined();
  await page.locator('#syncDisconnect').click();
  expect(await stored()).toEqual({ [A]: 'interested' });
  expect(requests.some(item => item.method === 'DELETE')).toBe(false);
});

test('real unauthorized Worker response and offline failure preserve every local entry', async ({ page, context }) => {
  await open(page, { [A]: 'interested', notes: 'synthetic-note' });
  await enable(page, 'synthetic-wrong-token');
  const before = await local(page);
  await page.locator('#syncPull').click();
  await expect(page.locator('#syncMessage')).toContainText('Authentication failed');
  expect(await local(page)).toEqual(before);
  await context.setOffline(true);
  await page.locator('#syncPush').click();
  await expect(page.locator('#syncMessage')).toContainText('Cannot reach');
  expect(await local(page)).toEqual(before);
  await context.setOffline(false);
  expect(await stored()).toEqual({});
});

test('malformed responses reject the whole document without partial merges (substituted HTTP bodies)', async ({ page }) => {
  await open(page, { [A]: 'interested', notes: 'synthetic-note' });
  await enable(page);
  const before = await local(page);
  const malformed = [
    { contract_version: 2, statuses: {} },
    { contract_version: 1, statuses: [] },
    { contract_version: 1, statuses: { [B]: 'offer' } },
    { contract_version: 1, statuses: { [B]: 'dead', 'https://private.invalid/jd': 'applied' } },
    { contract_version: 1, statuses: { [`job_${'a'.repeat(125)}`]: 'applied' } },
    { contract_version: 1, statuses: { [B]: 'dead' }, profile: 'synthetic-profile' },
    { contract_version: 1, statuses: Object.fromEntries(Array.from({ length: 501 }, (_, i) => [`job_${String(i).padStart(8, '0')}`, 'skip'])) },
    '<html>not JSON</html>',
    ' '.repeat(81921),
  ];
  for (const body of malformed) {
    await page.route('**/v1/statuses', route => route.fulfill({ status: 200, contentType: 'application/json', body: typeof body === 'string' ? body : JSON.stringify(body) }));
    await page.locator('#syncPull').click();
    await expect(page.locator('#syncMessage')).toContainText('Invalid Worker response');
    expect(await local(page)).toEqual(before);
    await expect(page.locator('#syncPreview')).toBeHidden();
    await page.unroute('**/v1/statuses');
  }
  await page.route('**/v1/statuses', route => route.fulfill({ status: 200, contentType: 'text/html', body: '{}' }));
  await page.locator('#syncPull').click();
  await expect(page.locator('#syncMessage')).toContainText('Invalid Worker response');
  expect(await local(page)).toEqual(before);
});

function statusesOfSize(bytes) {
  const entries = Array.from({ length: 500 }, (_, i) => [`job_${String(i).padStart(8, '0')}`, 'interested']);
  let remaining = bytes - Buffer.byteLength(JSON.stringify({ contract_version: 1, statuses: Object.fromEntries(entries), legacy_keys: [] }));
  for (const entry of entries) {
    const extra = Math.min(remaining, 128 - entry[0].length);
    entry[0] += '_'.repeat(extra);
    remaining -= extra;
  }
  expect(remaining).toBe(0);
  return Object.fromEntries(entries);
}

test('real Worker accepts exactly 500 items / 64 KiB; client blocks one byte or item over', async ({ page }) => {
  await open(page, statusesOfSize(65536));
  await enable(page);
  await page.locator('#syncPush').click();
  await expect(page.locator('#syncMessage')).toContainText('Push confirmed for 500');
  expect(requests.filter(item => item.method === 'POST')[0].bytes).toBe(65536);
  expect(Object.keys(await stored())).toHaveLength(500);
  for (const statuses of [statusesOfSize(65537), Object.fromEntries(Array.from({ length: 501 }, (_, i) => [`job_${String(i).padStart(8, '0')}`, 'skip']))]) {
    requests = [];
    await open(page, statuses);
    await enable(page);
    await page.locator('#syncPush').click();
    await expect(page.locator('#syncMessage')).toContainText('Push stopped');
    expect(requests).toEqual([]);
  }
});

test('local edit, preview edit and disconnect invalidate late responses even if abort is ignored', async ({ page }) => {
  // Deliberately substitute abort-insensitive transport to prove revision/session guards.
  await page.addInitScript(() => {
    const original = window.fetch.bind(window);
    window.fetch = (url, options) => {
      if (url !== '/v1/statuses') return original(url, options);
      const { signal, ...rest } = options;
      return original(url, rest);
    };
  });
  await open(page, { [A]: 'interested' });
  await enable(page);
  for (const action of ['edit', 'disconnect']) {
    let release;
    const gate = new Promise(resolve => { release = resolve; });
    let entered;
    const started = new Promise(resolve => { entered = resolve; });
    await page.route('**/v1/statuses', async route => {
      entered(); await gate;
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ contract_version: 1, statuses: { [A]: 'applied', [B]: 'dead' } }) });
    });
    await page.locator('#syncPull').click();
    await started;
    await expect(page.locator('#syncPush')).toBeDisabled();
    if (action === 'edit') await page.locator(`[data-key="${A}"] [data-state=skip]`).click();
    else await page.locator('#syncDisconnect').click();
    release();
    await expect(page.locator('#syncConnect')).toBeEnabled();
    await expect(page.locator('#syncPreview')).toBeHidden();
    expect((await local(page))[A]).toBe('skip');
    expect((await local(page))[B]).toBeUndefined();
    await page.unroute('**/v1/statuses');
    if (action === 'disconnect') await enable(page);
  }
  await api('POST', { [A]: 'applied' });
  await page.locator('#syncPull').click();
  await expect(page.locator('#syncPreview')).toBeVisible();
  await page.locator(`[data-key="${A}"] [data-state=interested]`).click();
  await expect(page.locator('#syncPreview')).toBeHidden();
  expect((await local(page))[A]).toBe('interested');
});

test('failed browser storage rolls back the entire Pull (substituted storage failure)', async ({ page }) => {
  await api('POST', { [A]: 'applied', [B]: 'dead' });
  await open(page, { [A]: 'interested' });
  await enable(page);
  const before = await local(page);
  await page.locator('#syncPull').click();
  await expect(page.locator('#syncPreview')).toBeVisible();
  await page.evaluate(() => {
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = function(key, value) {
      if (key === 'job-radar-status') throw new DOMException('synthetic quota', 'QuotaExceededError');
      return original.call(this, key, value);
    };
  });
  await page.locator('#syncRemote').click();
  await expect(page.locator('#syncMessage')).toContainText('Local statuses were restored');
  expect(await local(page)).toEqual(before);
  await expect(page.locator(`[data-key="${A}"]`)).toHaveAttribute('data-status', 'interested');
});

test('late real Worker Push acknowledgement cannot erase a newer local edit', async ({ page }) => {
  // Ignore cancellation only in the transport, while still forwarding the real Worker request.
  await page.addInitScript(() => {
    const original = window.fetch.bind(window);
    window.fetch = (url, options) => {
      if (url !== '/v1/statuses') return original(url, options);
      const { signal, ...rest } = options;
      return original(url, rest);
    };
  });
  await open(page, { [A]: 'interested' });
  await enable(page);
  let release, entered;
  const gate = new Promise(resolve => { release = resolve; });
  const received = new Promise(resolve => { entered = resolve; });
  await page.route('**/v1/statuses', async route => {
    const response = await route.fetch();
    entered(); await gate;
    await route.fulfill({ response });
  });
  await page.locator('#syncPush').click();
  await received;
  await expect(page.locator('#syncPull')).toBeDisabled();
  await page.locator(`[data-key="${A}"] [data-state=skip]`).click();
  release();
  await expect(page.locator('#syncPull')).toBeEnabled();
  await expect(page.locator('#syncMessage')).toContainText('Local statuses changed');
  expect((await local(page))[A]).toBe('skip');
  expect(await stored()).toEqual({ [A]: 'interested' });
  expect(requests.filter(item => item.method === 'POST')).toHaveLength(1);
});

test('another tab changing browser storage invalidates a preview and blocks stale Push until reload', async ({ page, context }) => {
  await api('POST', { [A]: 'applied' });
  await open(page, { [A]: 'interested' });
  await enable(page);
  await page.locator('#syncPull').click();
  await expect(page.locator('#syncPreview')).toBeVisible();
  const other = await context.newPage();
  await other.goto(origin);
  const updated = { [A]: 'skip', [OTHER]: 'dead' };
  await other.evaluate(value => localStorage.setItem('job-radar-status', JSON.stringify(value)), updated);
  await expect(page.locator('#syncPreview')).toBeHidden();
  await expect(page.locator('#syncMessage')).toContainText('Another tab changed');
  requests = [];
  await page.locator('#syncPush').click();
  await expect(page.locator('#syncMessage')).toContainText('Reload this page');
  expect(requests).toEqual([]);
  expect(await local(page)).toEqual(updated);
  await page.reload();
  await page.locator('#optionalSync summary').click();
  await enable(page);
  await page.locator('#syncPush').click();
  await expect(page.locator('#syncMessage')).toContainText('Push confirmed');
  expect(await stored()).toMatchObject(updated);
  await other.close();
});

test('redirect and malformed Push acknowledgement cannot leak tokens or change local state', async ({ page }) => {
  await open(page, { [A]: 'interested' });
  await enable(page);
  const before = await local(page);
  const destinations = [];
  page.on('request', request => destinations.push(request.url()));
  await page.route('**/v1/statuses', route => route.fulfill({ status: 302, headers: { location: `${origin}/must-not-follow` } }));
  await page.locator('#syncPull').click();
  await expect(page.locator('#syncMessage')).toContainText('Cannot reach');
  expect(destinations.some(url => url.includes('must-not-follow'))).toBe(false);
  await page.unroute('**/v1/statuses');
  await page.route('**/v1/statuses', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ contract_version: 1, statuses: {} }) }));
  await page.locator('#syncPush').click();
  await expect(page.locator('#syncMessage')).toContainText('Invalid Worker response');
  expect(await local(page)).toEqual(before);
});

test('mobile 390px real Pull is usable without overflow; file pages cannot connect', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await api('POST', { [A]: 'applied', [B]: 'dead' });
  await open(page, { [A]: 'interested' });
  await enable(page);
  await pull(page, true);
  await expect(page.locator(`[data-key="${A}"]`)).toHaveAttribute('data-status', 'applied');
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);
  await page.screenshot({ path: path.join(root, 'test-results', 'sync-mobile-390.png'), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({ path: path.join(root, 'test-results', 'sync-desktop.png'), fullPage: true });
  requests = [];
  await page.goto(pathToFileURL(fixture).href);
  await page.locator('#optionalSync summary').click();
  await expect(page.locator('#syncToken')).toBeDisabled();
  await page.locator('#syncConnect').click();
  await expect(page.locator('#syncMessage')).toContainText('File dashboards remain local-only');
  expect(requests).toEqual([]);
});

test('non-loopback HTTP refuses opt-in; HTTPS and the three HTTP loopback hosts allow it without requests', async ({ page }) => {
  const apiRequests = [];
  page.on('request', request => {
    if (new URL(request.url()).pathname === '/v1/statuses') apiRequests.push(request.url());
  });
  // Route only page documents to synthetic origins; no remote network is contacted.
  for (const target of ['http://public.example.test/', 'https://public.example.test/',
    'http://localhost/', 'http://127.0.0.1/', 'http://[::1]/']) {
    await page.route(target, route => route.fulfill({ contentType: 'text/html', body: readFileSync(fixture, 'utf8') }));
    await page.goto(target);
    await page.locator('#optionalSync summary').click();
    const before = await local(page);
    if (target === 'http://public.example.test/') {
      await expect(page.locator('#syncToken')).toBeDisabled();
      await page.locator('#syncConsent').check();
      // Exercise the guard even if a caller bypasses the disabled password input.
      await page.locator('#syncToken').evaluate(element => { element.value = 'synthetic-not-sent'; });
      await page.locator('#syncConnect').click();
      await expect(page.locator('#syncMessage')).toContainText('Sync requires HTTPS');
      await expect(page.locator('#syncActions')).toBeHidden();
    } else {
      await expect(page.locator('#syncToken')).toBeEnabled();
      await enable(page);
      await expect(page.locator('#syncActions')).toBeVisible();
    }
    expect(await local(page)).toEqual(before);
    await page.unroute(target);
  }
  expect(apiRequests).toEqual([]);
});
