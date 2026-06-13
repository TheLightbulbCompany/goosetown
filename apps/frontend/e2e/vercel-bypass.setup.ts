import { request } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';

import { BYPASS_STATE_PATH } from '../playwright.config';

/**
 * Primes the Vercel Deployment Protection bypass cookie.
 *
 * Hits the deployment once with the documented query-param pair —
 * ``x-vercel-protection-bypass`` authenticates, ``x-vercel-set-bypass-cookie``
 * makes Vercel answer with a ``_vercel_jwt`` cookie — then saves the cookie
 * jar as a storage-state file that every browser context loads. See the
 * rationale in playwright.config.ts for why this is a cookie and not an
 * ``extraHTTPHeaders`` entry.
 */
export default async function globalSetup() {
  const secret = process.env.VERCEL_AUTOMATION_BYPASS_SECRET;
  if (!secret) return;

  const baseURL = process.env.E2E_BASE_URL || 'https://dev.goosetown.isol8.co';
  const ctx = await request.newContext();
  const resp = await ctx.get(
    `${baseURL}/?x-vercel-protection-bypass=${secret}&x-vercel-set-bypass-cookie=true`,
  );
  if (!resp.ok()) {
    throw new Error(
      `Vercel protection-bypass priming failed: ${resp.status()} for ${baseURL}`,
    );
  }

  fs.mkdirSync(path.dirname(BYPASS_STATE_PATH), { recursive: true });
  await ctx.storageState({ path: BYPASS_STATE_PATH });
  await ctx.dispose();
}
