import { test, expect } from '@playwright/test';

/**
 * Public-surface smoke test.
 *
 * Runs against whatever ``E2E_BASE_URL`` is set to (dev by default). Does not
 * sign in, does not register agents — those paths need Clerk test creds and
 * are deferred to a dedicated "critical-path" suite. This test catches the
 * big regressions that typically make the app completely unusable:
 *   - Frontend bundle fails to load / hydrate
 *   - ``/town/conversations`` endpoint broken
 *   - Sprite CDN serving without the CORP header required by COEP pages
 *   - Godot canvas never mounts
 */
test.describe('GooseTown smoke', () => {
  test('loads the home page and renders the public conversations sidebar', async ({
    page,
  }) => {
    const consoleErrors: string[] = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') consoleErrors.push(msg.text());
    });

    await page.goto('/');
    await expect(page).toHaveTitle(/GooseTown/i);

    // Sidebar Conversations section renders even when signed out.
    await expect(
      page.getByRole('heading', { name: /conversations/i, level: 3 }),
    ).toBeVisible();

    // Dev has Friends conversations seeded, so at least one row should be
    // present within the expect timeout. If this flakes in a fresh
    // environment with no data, relax to checking the heading alone.
    const rows = page.getByRole('button').filter({ hasText: /&/ });
    await expect(rows.first()).toBeVisible({ timeout: 15_000 });

    // Godot canvas should mount — its container lives inside GameLayout.
    // Godot's WebGL script inserts a <canvas> tag once ready.
    await expect(page.locator('canvas').first()).toBeVisible({ timeout: 20_000 });

    // No catastrophic console errors. Filter out known-benign noise from
    // third-party libraries and the router's future-flag warnings.
    const fatal = consoleErrors.filter((msg) => {
      if (/React Router Future Flag/i.test(msg)) return false;
      if (/clerk/i.test(msg)) return false; // signed-out clerk warnings
      if (/favicon/i.test(msg)) return false;
      if (/godot/i.test(msg) && /WARNING/.test(msg)) return false;
      return true;
    });
    expect(fatal, `unexpected console errors:\n${fatal.join('\n')}`).toEqual([]);
  });

  test('sprite CDN returns Cross-Origin-Resource-Policy header', async ({
    request,
  }) => {
    // Any sprite URL under the dev CDN — a 404 still flows through the
    // ResponseHeadersPolicy so we can assert the header without needing a
    // known-live agent sprite.
    const resp = await request.head(
      'https://assets-dev.goosetown.isol8.co/_smoke-check.png',
    );
    const corp = resp.headers()['cross-origin-resource-policy'];
    expect(corp).toBe('cross-origin');
  });
});
