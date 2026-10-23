import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { test, expect } from '@playwright/test';

const root = process.cwd();
const candidate = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const python = process.env.PYTHON || (existsSync(candidate) ? candidate : 'python');
let evidence;

test.beforeAll(() => {
  const parent = path.join(root, 'test-results', 'recovery-chain');
  mkdirSync(parent, { recursive: true });
  const fixtureRoot = mkdtempSync(path.join(parent, 'attempt-'));
  const result = execFileSync(python, [path.join(root, 'tests/browser/recovery_fixture.py'), fixtureRoot], {
    cwd: root,
    env: { ...process.env, PYTHONPATH: [root, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter) },
    encoding: 'utf8',
    timeout: 45000,
  });
  evidence = JSON.parse(result.trim());
  writeFileSync(path.join(fixtureRoot, 'verification.json'), JSON.stringify(evidence, null, 2));
});

for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
  test(`actual CLI recovery and offline preference change at ${viewport.width}px`, async ({ page }) => {
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route(/^https?:\/\//, route => route.abort());
    await page.setViewportSize(viewport);
    await page.goto(pathToFileURL(path.join(evidence.root, 'partial.html')).href);
    await expect(page.getByTestId('scan-state')).toContainText('Partial scan');
    await expect(page.getByTestId('job-card')).toHaveCount(1);
    await expect(page.getByTestId('job-card')).toContainText('Healthy Go Backend Engineer');

    await page.goto(pathToFileURL(path.join(evidence.root, 'complete.html')).href);
    await expect(page.getByTestId('scan-state')).toContainText('Scan complete');
    await expect(page.getByTestId('job-card')).toHaveCount(2);
    expect(evidence.calls).toEqual({ healthy: 1, retry: 2 });
    expect(evidence.scan_jobs).toBe(3);
    expect(evidence.persisted_jobs).toBe(3);
    expect(evidence.observed_on).toBe('2026-07-17');
    expect(evidence.last_seen).toEqual(['2026-07-17', '2026-07-18']);
    expect(evidence.dates_by_id).toEqual({
      healthy: ['2026-07-17', '2026-07-17'], marketing: ['2026-07-17', '2026-07-17'],
      retry: ['2026-07-18', '2026-07-18'],
    });
    expect(evidence.first_receipt.stages.scan).toBe('partial');
    expect(evidence.first_receipt.scan_progress).toMatchObject({ completed: 1, remaining: 1 });
    expect(evidence.second_receipt.scan_progress).toMatchObject({ completed: 2, remaining: 0 });
    expect(evidence.second_receipt.stages).toEqual({
      scan: 'complete', workflow: 'complete', review: 'not_configured', build: 'complete',
      sync: 'not_configured', deployment: 'not_requested',
    });

    await page.goto(pathToFileURL(path.join(evidence.root, 'rescored.html')).href);
    await expect(page.getByTestId('job-card')).toHaveCount(1);
    await expect(page.getByTestId('job-card')).toContainText('Marketing Engineer');
    expect(await page.locator('#dashboard-data').evaluate(element => JSON.parse(element.textContent).jobs[0]))
      .toMatchObject({ first_seen: '2026-07-17', last_seen: '2026-07-17' });
    await page.reload();
    await expect(page.getByTestId('job-card')).toHaveCount(1);
    expect(evidence.scan_bytes_preserved_by_rebuild).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);
    await page.screenshot({ path: path.join(evidence.root, `recovery-${viewport.width}.png`), fullPage: true });
    expect(errors).toEqual([]);
  });
}
