import { request } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';

import { BYPASS_STATE_PATH } from '../playwright.config';

/**
 * Primes the Vercel Deployment Protection bypass cookie.
 *
 * Hits the deployment once with the documented header pair —
 * ``x-vercel-protection-bypass`` authenticates, ``x-vercel-set-bypass-cookie``
 * makes Vercel answer with a ``_vercel_jwt`` cookie — then saves the cookie
 * jar as a storage-state file that every browser context loads. See the
 * rationale in playwright.config.ts for why the browser side rides on a
 * cookie and not an ``extraHTTPHeaders`` entry.
 *
 * Headers (not query params) keep the secret out of access logs and
 * failed-request diagnostics that record the full URL. This is a Node-side
 * request, so the browser-context CORS-preflight problem doesn't apply.
 */
export default async function globalSetup() {
  const secret = process.env.VERCEL_AUTOMATION_BYPASS_SECRET;
  if (!secret) return;

  const baseURL = process.env.E2E_BASE_URL || 'https://dev.goosetown.thelightbulb.company';
  const ctx = await request.newContext();
  const resp = await ctx.get(`${baseURL}/`, {
    headers: {
      'x-vercel-protection-bypass': secret,
      'x-vercel-set-bypass-cookie': 'true',
    },
  });
  if (!resp.ok()) {
    throw new Error(
      `Vercel protection-bypass priming failed: ${resp.status()} for ${baseURL}`,
    );
  }

  fs.mkdirSync(path.dirname(BYPASS_STATE_PATH), { recursive: true });
  await ctx.storageState({ path: BYPASS_STATE_PATH });
  await ctx.dispose();
}
