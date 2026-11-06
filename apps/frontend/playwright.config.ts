import { defineConfig, devices } from '@playwright/test';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * Playwright smoke-test config.
 *
 * By default targets the dev deployment so the test doubles as a post-deploy
 * regression guard. Override with the ``E2E_BASE_URL`` env var when running
 * against a local dev server or prod.
 */
const BASE_URL = process.env.E2E_BASE_URL || 'https://dev.goosetown.thelightbulb.company';

/**
 * Vercel Deployment Protection bypass. ``dev.goosetown.thelightbulb.company`` is aliased
 * to a *preview* deployment (the 2026-06-09 branch-scoping fix made CI's
 * ``vercel alias`` the domain's sole owner), and Vercel Authentication gates
 * preview deployments — without a bypass every request gets a 401
 * "Authentication Required" page instead of the app.
 *
 * The bypass is primed as a COOKIE via global setup rather than sent as an
 * ``extraHTTPHeaders`` entry: Playwright attaches extra headers to every
 * request from the browser context, and the non-simple custom header forces
 * a CORS preflight on cross-origin loads (Clerk's CDN rejects it, so the app
 * hangs at "Loading agents..."). The cookie is scoped to the app's domain
 * and rides along invisibly instead.
 *
 * The secret comes from the Vercel project's Protection Bypass for
 * Automation setting and reaches CI via the
 * ``VERCEL_AUTOMATION_BYPASS_SECRET`` repo secret. Unset (e.g. against a
 * local dev server) the suite runs without any bypass machinery.
 */
const BYPASS_SECRET = process.env.VERCEL_AUTOMATION_BYPASS_SECRET;

const configDir = path.dirname(fileURLToPath(import.meta.url));
export const BYPASS_STATE_PATH = path.join(
  configDir,
  'test-results',
  'vercel-bypass-state.json',
);

export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  expect: { timeout: 10_000 },
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? 'github' : 'list',
  globalSetup: BYPASS_SECRET ? './e2e/vercel-bypass.setup.ts' : undefined,
  use: {
    baseURL: BASE_URL,
    trace: 'on-first-retry',
    ...(BYPASS_SECRET ? { storageState: BYPASS_STATE_PATH } : {}),
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
