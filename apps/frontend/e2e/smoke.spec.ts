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

    // PR #254 (2026-04-30) moved the canvas + conversations sidebar from
    // ``/`` (now the landing page) to ``/town``. The smoke spec was missed
    // in that route move and silently failed on every PR for the next four
    // days. Surfaced 2026-05-04 readiness review.
    await page.goto('/town');
    await expect(page).toHaveTitle(/GooseTown/i);

    // Sidebar Conversations section renders even when signed out.
    await expect(
      page.getByRole('heading', { name: /conversations/i, level: 3 }),
    ).toBeVisible();

    // Don't assert any specific conversation rows — dev gets wiped between
    // test campaigns, so the sidebar may legitimately be empty. The heading
    // assertion above is enough to catch a broken sidebar render.

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

  test('at least one agent sprite renders in the canvas', async ({ page }) => {
    // Regression guard for the JsBridge.call_js migration (security-hardening
    // sub-project #1). If the __gt_load_sprite handler or window.set() path
    // broke, no sprite ever gets uploaded to the Godot texture, so agents
    // render as blank shadow placeholders. We detect a non-blank sprite by
    // polling the counter incremented in the __gt_load_sprite handler.
    //
    // Runs against live dev, so on a PR whose code hasn't deployed yet the
    // handler may not exist. In that case we skip — the deploy pipeline
    // re-runs smoke tests post-deploy, which is where the real guard fires.
    // PR #254 (2026-04-30) moved the canvas + conversations sidebar from
    // ``/`` (now the landing page) to ``/town``. The smoke spec was missed
    // in that route move and silently failed on every PR for the next four
    // days. Surfaced 2026-05-04 readiness review.
    //
    // The waits below (engine boot + world-state window + sprite poll) sum
    // well past the suite-default 30s, so this test needs its own budget.
    test.setTimeout(120_000);
    await page.goto('/town');
    await expect(page.locator('canvas').first()).toBeVisible({ timeout: 20_000 });

    const handlerExists = await page.evaluate(
      () =>
        typeof (window as unknown as { __gt_load_sprite?: unknown })
          .__gt_load_sprite === 'function',
    );
    test.skip(
      !handlerExists,
      'Live dev deploy predates the JsBridge migration; post-deploy run will enforce.',
    );

    // The canvas mounts well before the Godot engine finishes booting —
    // GodotCanvas renders the ``<canvas>`` unconditionally in JSX with a
    // loading overlay on top, so ``toBeVisible()`` proves nothing about the
    // engine — and ``godotUpdateWorldState`` only exists once the JsBridge
    // autoload is up. Wrapping before that silently installs nothing, so
    // the world-state wait below could never fire and the test died on its
    // timeout. This wait is a hard assertion, not a skip: a broken Godot
    // export is exactly the regression class this test exists to catch,
    // and no other test verifies engine boot.
    await page.waitForFunction(
      () =>
        typeof (window as unknown as { godotUpdateWorldState?: unknown })
          .godotUpdateWorldState === 'function',
      { timeout: 45_000 },
    );

    // Watch the latest world_state to learn whether any agent is currently
    // connected. The handler can only fire when an agent is in the broadcast.
    // Hook godotUpdateWorldState (set by Godot at engine boot) and record
    // the most recent agent count.
    await page.evaluate(() => {
      const w = window as unknown as {
        godotUpdateWorldState?: (json: string) => void;
        __gt_test_last_agents?: number;
      };
      const orig = w.godotUpdateWorldState;
      if (orig && !(orig as unknown as { __wrapped?: boolean }).__wrapped) {
        const wrapped = function (this: unknown, json: string) {
          try {
            const m = JSON.parse(json) as { agents?: unknown[] };
            w.__gt_test_last_agents = Array.isArray(m.agents)
              ? m.agents.length
              : 0;
          } catch {
            /* ignore parse errors */
          }
          return (orig as (this: unknown, j: string) => void).call(this, json);
        };
        (wrapped as unknown as { __wrapped: boolean }).__wrapped = true;
        w.godotUpdateWorldState = wrapped;
      }
    });

    // Wait up to 6s for at least one world_state to arrive so we can decide.
    const sawWorldState = await page
      .waitForFunction(
        () =>
          typeof (window as unknown as { __gt_test_last_agents?: number })
            .__gt_test_last_agents === 'number',
        { timeout: 6_000 },
      )
      .then(() => true)
      .catch(() => false);
    test.skip(
      !sawWorldState,
      'No world_state broadcast observed within 6s; nothing to render against.',
    );

    const lastAgentCount = await page.evaluate(
      () =>
        (window as unknown as { __gt_test_last_agents?: number })
          .__gt_test_last_agents ?? 0,
    );
    test.skip(
      lastAgentCount === 0,
      'Live dev currently has no connected agents; sprite-render guard requires at least one.',
    );

    // At least one agent IS in the broadcast. Now enforce the real guard:
    // __gt_sprite_load_count must increment.
    await expect
      .poll(
        async () =>
          await page.evaluate(
            () =>
              (window as unknown as { __gt_sprite_load_count?: number })
                .__gt_sprite_load_count || 0,
          ),
        { timeout: 30_000, intervals: [1000, 2000, 3000] },
      )
      .toBeGreaterThan(0);
  });
});
