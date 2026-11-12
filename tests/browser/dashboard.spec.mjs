import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import path from 'node:path';
import { test, expect } from '@playwright/test';

const root = process.cwd();
const fixture = path.join(root, 'test-results', 'dashboard-fixture.html');
const largeFixture = path.join(root, 'test-results', 'dashboard-large-fixture.html');
const venvPython = process.platform === 'win32'
  ? path.join(root, '.venv', 'Scripts', 'python.exe')
  : path.join(root, '.venv', 'bin', 'python');
const python = process.env.PYTHON
  || (existsSync(venvPython) ? venvPython : (process.platform === 'win32' ? 'python.exe' : 'python3'));
const fixtureEnvironment = {
  ...process.env,
  PYTHONPATH: [root, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
};

test.beforeAll(() => {
  execFileSync(python, [
    path.join(root, 'tests', 'browser', 'build_dashboard_fixture.py'),
    fixture,
  ], { cwd: root, env: fixtureEnvironment, stdio: 'inherit' });
  execFileSync(python, [
    path.join(root, 'tests', 'browser', 'build_dashboard_fixture.py'),
    largeFixture,
    '--large',
  ], { cwd: root, env: fixtureEnvironment, stdio: 'inherit' });
});

async function openDashboard(page, viewport) {
  const consoleErrors = [];
  const pageErrors = [];
  page.on('console', message => {
    if (message.type() === 'error') consoleErrors.push(message.text());
  });
  page.on('pageerror', error => pageErrors.push(error.message));
  await page.setViewportSize(viewport);
  await page.goto(pathToFileURL(fixture).href);
  await expect(page.getByTestId('scan-state')).toContainText('Partial scan');
  return { consoleErrors, pageErrors };
}

test('desktop preserves review interactions, migration, persistence, and export', async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem('job-radar-status', JSON.stringify({
      'greenhouse:active': 'interested',
    }));
  });
  const errors = await openDashboard(page, { width: 1440, height: 1000 });

  await expect(page.getByTestId('job-card')).toHaveCount(3);
  await expect(page.getByTestId('tracking-summary')).toContainText('4 tracked applications');
  await expect(page.locator('[data-key="job_active"]')).toHaveAttribute('data-status', 'interested');

  await page.getByRole('button', { name: 'Swipe review' }).click();
  await expect(page.locator('#topSwipeCard')).toBeVisible();
  await page.keyboard.press('ArrowUp');
  const state = await page.evaluate(() => JSON.parse(localStorage.getItem('job-radar-status')));
  expect(Object.values(state)).toContain('applied');
  expect(state['greenhouse:active']).toBeUndefined();
  expect(state.job_active).toBe('interested');

  const download = page.waitForEvent('download');
  await page.getByTestId('export-state').click();
  expect((await download).suggestedFilename()).toBe('job-radar-state.json');

  await page.screenshot({
    path: path.join(root, 'test-results', 'dashboard-evidence', 'desktop.png'),
    fullPage: true,
  });
  expect(errors.consoleErrors).toEqual([]);
  expect(errors.pageErrors).toEqual([]);
});

test('mobile 390px has no horizontal overflow and controls remain usable', async ({ page }) => {
  const errors = await openDashboard(page, { width: 390, height: 844 });
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
  await expect(page.getByRole('button', { name: 'List' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Swipe review' })).toBeVisible();
  await expect(page.getByTestId('export-state')).toBeVisible();
  const touchHeight = await page.getByRole('button', { name: 'Swipe review' })
    .evaluate(element => element.getBoundingClientRect().height);
  expect(touchHeight).toBeGreaterThanOrEqual(44);
  await page.getByRole('button', { name: 'Swipe review' }).click();
  await expect(page.locator('#topSwipeCard')).toBeVisible();
  await page.screenshot({
    path: path.join(root, 'test-results', 'dashboard-evidence', 'mobile-390.png'),
    fullPage: true,
  });
  expect(errors.consoleErrors).toEqual([]);
  expect(errors.pageErrors).toEqual([]);
});

test('large scan renders a bounded window and loads more without losing filters', async ({ page }) => {
  await page.goto(pathToFileURL(largeFixture).href);

  await expect(page.getByTestId('job-card')).toHaveCount(50);
  await expect(page.locator('.rejected-item')).toHaveCount(20);
  await page.locator('.rejected-audit summary').click();
  await page.locator('#loadMoreRejected').click();
  await expect(page.locator('.rejected-item')).toHaveCount(40);
  await expect(page.locator('#visibleCount')).toHaveText('125');
  await page.getByRole('button', { name: /Load 50 more jobs/ }).click();
  await expect(page.getByTestId('job-card')).toHaveCount(100);

  await page.locator('#searchBox').fill('Engineer 124');
  await expect(page.getByTestId('job-card')).toHaveCount(1);
  await expect(page.locator('#visibleCount')).toHaveText('1');
});

test('an Applied card copies a confirmation command only after verification', async ({ page }) => {
  const errors = await openDashboard(page, { width: 1280, height: 900 });
  const card = page.locator('[data-key="job_active"]');
  await expect(card.getByTestId('confirm-application')).toHaveCount(0);
  await card.getByRole('button', { name: '✓ Applied', exact: true }).click();
  const form = card.getByTestId('confirm-application');
  await expect(form).toBeVisible();
  await form.getByRole('button', { name: 'Copy confirmation command' }).click();
  await expect(form.locator('.confirm-error')).toContainText('verify the submission');
  await expect(form.locator('textarea')).toBeHidden();

  await form.getByLabel('Resume version').fill('backend-v2');
  await form.getByLabel('Channel').fill('official-site');
  await form.getByLabel('Confirmation note').fill('See receipt 1/2 locally');
  await form.getByLabel('Confirmed at').fill('2026-10-23T00:05:00+08:00');
  await form.getByRole('checkbox', { name: 'I verified this submission on the official site' }).check();
  await form.getByRole('button', { name: 'Copy confirmation command' }).click();
  await expect(form.locator('textarea')).toHaveValue(/--job-id "job_active"/);
  await expect(form.locator('textarea')).toHaveValue(/--applied-at "unknown"/);
  await expect(form.locator('textarea')).toHaveValue(/--confirmation-evidence "See receipt 1\/2 locally"/);
  await expect(form.locator('textarea')).toHaveValue(/--confirmed-at "2026-10-23T00:05:00\+08:00"/);
  await expect(form.locator('textarea')).toHaveValue(/--confirm-submitted$/);

  await page.getByRole('button', { name: 'Saved matches' }).click();
  const saved = page.locator('[data-match-key="job_active"]');
  await expect(saved.getByTestId('confirm-application')).toBeVisible();
  await saved.getByLabel('Confirmation note').fill('token $ secret');
  await saved.getByLabel('Resume version').fill('backend-v2');
  await saved.getByLabel('Confirmed at').fill('2026-10-23T00:05:00Z');
  await saved.getByRole('checkbox', { name: 'I verified this submission on the official site' }).check();
  await saved.getByRole('button', { name: 'Copy confirmation command' }).click();
  await expect(saved.locator('.confirm-error')).toContainText('simple punctuation');
  await expect(saved.locator('textarea')).toBeHidden();

  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: 'List' }).click();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);
  expect(errors.consoleErrors).toEqual([]);
  expect(errors.pageErrors).toEqual([]);
});

test('an Applied marker needs explicit local confirmation and survives stale browser state after rebuild', async ({ page }) => {
  await openDashboard(page, { width: 1440, height: 1000 });
  const card = page.locator('[data-key="job_active"]');
  await card.getByRole('button', { name: '✓ Applied', exact: true }).click();
  await expect(card).toHaveAttribute('data-status', 'applied');
  await expect(page.getByTestId('tracking-summary')).toContainText('4 tracked applications');
  await expect(page.locator('#applicationRecordingHelp')).toContainText('not proof of submission');
  const downloadPromise = page.waitForEvent('download');
  await page.getByTestId('export-state').click();
  const handoff = path.join(root, 'test-results', 'application-handoff.json');
  await (await downloadPromise).saveAs(handoff);
  const rebuilt = path.join(root, 'test-results', 'application-confirmed.html');
  execFileSync(python, [path.join(root, 'tests', 'browser', 'record_application_fixture.py'), handoff, rebuilt], {
    cwd: root, env: fixtureEnvironment, stdio: 'inherit',
  });
  await page.evaluate(() => localStorage.setItem('job-radar-status', JSON.stringify({ job_active: 'skip' })));
  await page.goto(pathToFileURL(rebuilt).href);
  await expect(page.locator('[data-key="job_active"]')).toHaveAttribute('data-status', 'applied');
  await expect(page.getByTestId('tracking-summary')).toContainText('1 tracked applications');
  await expect(page.locator('#recommendations')).not.toContainText('Active Backend Engineer');
  await page.evaluate(() => localStorage.setItem('job-radar-status', JSON.stringify({ job_active: 'skip' })));
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem('job-radar-status')).job_active)).toBe('skip');
  await page.reload();
  await expect(page.locator('[data-key="job_active"]')).toHaveAttribute('data-status', 'applied');
  await page.screenshot({ path: path.join(root, 'test-results', 'dashboard-evidence', 'confirmed-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator('#applicationRecordingHelp')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);
  await page.screenshot({ path: path.join(root, 'test-results', 'dashboard-evidence', 'confirmed-mobile-390.png'), fullPage: true });
});
