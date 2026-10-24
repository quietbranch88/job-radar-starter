import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { test, expect } from '@playwright/test';

const root = process.cwd();
const fixture = path.join(root, 'test-results', 'operator-review.html');
const localPython = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const python = process.env.PYTHON || (existsSync(localPython) ? localPython : (process.platform === 'win32' ? 'python' : 'python3'));
test.beforeAll(() => execFileSync(python, [path.join(root, 'tests/browser/review_receipts_fixture.py'), fixture],
  { cwd: root, env: { ...process.env, PYTHONPATH: root }, stdio: 'inherit' }));

for (const width of [390, 1440]) {
  test(`actual saved-scan CLI receipt statuses remain distinct at ${width}px`, async ({ page }) => {
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.setViewportSize({ width, height: 950 });
    await page.goto(pathToFileURL(fixture).href);
    const approved = page.getByTestId('job-card').filter({ has: page.getByRole('heading', { name: 'approved Backend Engineer', exact: true }) });
    const rejected = page.getByTestId('job-card').filter({ has: page.getByRole('heading', { name: 'rejected Backend Engineer', exact: true }) });
    const pending = page.getByTestId('job-card').filter({ has: page.getByRole('heading', { name: 'mismatch Backend Engineer', exact: true }) });
    await expect(approved.locator('[data-operator-review]')).toHaveText('Operator review: approved');
    await expect(rejected.locator('[data-operator-review]')).toHaveText('Operator review: rejected');
    await expect(pending.locator('[data-operator-review]')).toHaveText('Operator review: pending · version mismatch');
    await approved.getByRole('button', { name: 'Interested', exact: false }).click();
    await expect(approved).toHaveAttribute('data-status', 'interested');
    await expect(approved.locator('[data-operator-review]')).toHaveText('Operator review: approved');
    await page.getByRole('button', { name: 'Swipe review', exact: true }).click();
    await expect(page.locator('#topSwipeCard [data-operator-review]')).toBeVisible();
    const payload = await page.locator('#dashboard-data').textContent();
    expect(payload).not.toContain('Private Reviewer');
    expect(payload).not.toContain('private-review-evidence');
    expect(errors).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
    await page.screenshot({ path: path.join(root, 'test-results', `operator-review-${width}.png`), fullPage: true });
  });
}
