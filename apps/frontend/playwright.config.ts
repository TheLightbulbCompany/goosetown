import { defineConfig, devices } from '@playwright/test';

/**
 * Playwright smoke-test config.
 *
 * By default targets the dev deployment so the test doubles as a post-deploy
 * regression guard. Override with the ``E2E_BASE_URL`` env var when running
 * against a local dev server or prod.
 */
const BASE_URL = process.env.E2E_BASE_URL || 'https://dev.goosetown.isol8.co';

export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  expect: { timeout: 10_000 },
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    baseURL: BASE_URL,
    trace: 'on-first-retry',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
