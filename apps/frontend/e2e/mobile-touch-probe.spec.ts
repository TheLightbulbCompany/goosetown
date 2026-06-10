// apps/frontend/e2e/mobile-touch-probe.spec.ts
//
// Verification probe (Phase 0 V1 of the mobile plan) — NOT a CI test.
// Run manually: MOBILE_PROBE=1 pnpm exec playwright test e2e/mobile-touch-probe.spec.ts --project=chromium
// Uses Pixel 5 profile (chromium-based) because CDP touch dispatch needs Chromium.
import { test, expect, devices } from '@playwright/test';

const DEV_URL = 'https://dev.goosetown.isol8.co';

// Guard: skipped unless explicitly requested, so CI's plain `playwright test`
// never runs this against the live dev environment.
test.skip(!process.env.MOBILE_PROBE, 'manual probe — set MOBILE_PROBE=1 to run');

test.use({ ...devices['Pixel 5'] });

test('town loads on mobile and touch-drag pans the camera', async ({ page, context }) => {
  test.setTimeout(180_000);
  await page.goto(`${DEV_URL}/town`);

  // Engine boot: loading overlay disappears when startGame resolves.
  await expect(page.getByText('Loading GooseTown...')).toBeHidden({ timeout: 150_000 });
  const canvas = page.locator('#godot-canvas');
  await expect(canvas).toBeVisible();
  await page.waitForTimeout(2_000); // let first world_state paint

  const before = await canvas.screenshot();

  // Synthetic single-finger drag via CDP (Playwright's high-level API can't swipe).
  const client = await context.newCDPSession(page);
  const box = (await canvas.boundingBox())!;
  const cx = box.x + box.width / 2;
  const cy = box.y + box.height / 2;
  await client.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: cx, y: cy }] });
  for (let i = 1; i <= 12; i++) {
    await client.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: cx - i * 20, y: cy }] });
  }
  await client.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  await page.waitForTimeout(500);

  const after = await canvas.screenshot();
  // If the camera panned, the rendered frame must differ. (Agents moving can
  // also cause a diff — interpret a PASS as "probably works, confirm on a real
  // phone"; interpret a FAIL as "emulated pan is broken, build native touch pan".)
  expect(Buffer.compare(before, after)).not.toBe(0);
});
