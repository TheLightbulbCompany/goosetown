# Anonymous Viewer — public WS access for Town and Library

**Date:** 2026-04-29
**Status:** Design — pending implementation plan
**Owner:** prasiddha

## Problem

After PR #242, the library route exists at `/library` but is auth-gated like apartment, and the WebSocket viewer flow requires a Clerk-signed token. As a result, signed-out users see neither town agents nor library agents — the canvas loads but no `world_state` ever arrives.

We want town and library to be public viewers (like YouTube — anyone can watch without signing in), while keeping apartment private per-user.

## Goal

Signed-out users navigating to `/` or `/library` see live `world_state` updates and rendered agents, equivalent to a signed-in user's viewer experience for those scenes. Apartment stays auth-gated.

## Non-goals

- Anonymous users acting in the town (`town_act`, `chat`, `say`, etc.). They are read-only spectators.
- Per-user view rate limiting. The existing WAF (IP-rate cap 2000 req / 5min, AWS Managed Rules) is the only throttle. Like YouTube — IP-level abuse defense, no per-user caps.
- Frontend "sign in to interact" overlays / nudges. Future polish.
- Anonymous WS for the WebSocket-message endpoint (`/ws-message` agent commands). Anonymous tokens grant viewer access only; the existing token type checks for the agent path stay strict.

## Design

The Lambda authorizer at `apps/infra/lib/stacks/api-stack.ts:43-302` already verifies HMAC-signed town tokens via `sign_town_token(uid, iid, ...)`. It does not care about `uid` value — it only checks signature + expiry. We exploit this: anonymous tokens are just town tokens minted with sentinel `uid="anonymous"`, `iid="anonymous-viewer"`.

### Backend

New public endpoint in `apps/backend/routers/town.py`:

```python
@router.get("/anonymous-viewer-token")
async def get_anonymous_viewer_token():
    """Mint a short-lived viewer token for unauthenticated WebSocket access.

    No auth required. Token is HMAC-signed via the same TOWN_TOKEN_SECRET
    as registered agents and signed-in viewers. The Lambda authorizer
    accepts it transparently. Anonymous viewers can subscribe to
    world_state but cannot register, act, or send chat — those endpoints
    enforce a real user_id (Clerk subject) at the router layer.
    """
    from core.town_token import sign_town_token

    token = sign_town_token("anonymous", "anonymous-viewer", ttl_seconds=1800)
    return {"token": token}
```

`sign_town_token` already supports `ttl_seconds` (defaults to 30 days for agent tokens). We pass `ttl_seconds=1800` (30 min) for anonymous tokens — short, refreshable on reconnect.

### Frontend

Three changes:

**`apps/frontend/src/scenes.ts`**: flip library row.

```ts
{ id: 'library', displayName: 'Library', route: '/library', requireAuth: false, showInNav: 'always' },
```

**`apps/frontend/src/hooks/useTownState.ts`**: drop the `!getToken` early-return, add anonymous fallback.

```ts
useEffect(() => {
  if (!WS_URL) return;          // No WS configured — pure REST mode (used in tests)
  // ... existing setup ...

  const connect = async () => {
    if (cancelled) return;
    let token: string | null = null;
    try {
      if (getToken) {
        token = await getToken();
      }
    } catch { /* fall through to anonymous */ }
    if (cancelled) return;
    if (!token) {
      try {
        const r = await fetch(`${API_URL}/town/anonymous-viewer-token`);
        if (r.ok) token = (await r.json()).token;
      } catch { /* retry on next reconnect */ }
    }
    if (cancelled) return;
    if (!token) {
      setTimeout(connect, reconnectDelay);
      return;
    }
    ws = new WebSocket(`${WS_URL}?token=${token}`);
    // ... existing onopen/onmessage/onclose ...
  };
  // ...
}, [getToken, processState, userId]);
```

The userId injected into `world_state` for the apartment owner-filter remains "" for anonymous (the empty string is already handled — `agent_manager.gd:_on_world_state` skips the filter when `my_user_id == ""`). Anonymous viewers in apartment context would see all apartment agents, but they cannot reach the apartment route (`requireAuth: true` redirect), so this case never fires.

**`apps/frontend/src/scenes.test.ts`**: update assertions to reflect library being public.

### Apartment stays auth-gated

`scenes.ts` keeps `requireAuth: true, showInNav: 'signed_in'` for apartment. Signed-out users navigating directly to `/apartment` get redirected to `/`. The per-user privacy of apartment SubViewports stays intact at both layers (server-side per-user instances, client-side userId filter).

### Tests

- `apps/backend/tests/unit/test_town_router.py` (or appropriate existing file): test the new endpoint returns 200, returns `{token}`, the token verifies via `verify_town_token` with `uid="anonymous"` and `iid="anonymous-viewer"`, and the `exp` is roughly 30 minutes in the future.
- `apps/frontend/src/scenes.test.ts`: assert library is `requireAuth: false`, `showInNav: 'always'`.
- `apps/frontend/src/hooks/useTownState.test.ts`: extend with a test that asserts the hook fetches `/anonymous-viewer-token` and opens the WS when `getToken` is undefined or returns null.

### Manual smoke test

1. Sign out (or open in incognito).
2. Visit `https://dev.goosetown.isol8.co/` — Town link visible, agents visible in canvas.
3. Click `Library` (now visible signed-out) → `/library`. Library scene loads.
4. Drive an agent into the library. Confirm visible in the canvas.
5. Visit `/apartment` directly while signed-out → redirected to `/`. Verify in URL bar.
6. Sign back in — confirm Apartment link reappears, normal flow still works.

## Risks

- **Token endpoint abuse:** anyone can call `/anonymous-viewer-token` repeatedly to mint tokens. Each token is small (HMAC, ~200 bytes) and the endpoint is cheap. WAF rate-limits at the IP layer. Acceptable.
- **WS connection abuse:** anonymous WS connections consume a per-connection slot. At meaningful scale we'd want a max-anonymous-connections cap or a per-IP connection limit. Defer until scale signal exists.
- **No identity in logs:** anonymous viewer events log as `principalId="anonymous"` everywhere. That's intentional — we don't want any analytics inferring identity from connection metadata. Tradeoff accepted.

## Out of scope (follow-ups)

- Per-IP connection cap on the WS API Gateway.
- "Sign in to interact" inline UI nudges when an anonymous viewer hovers an agent.
- Anonymous "ghost" cursor presence on the map (showing where viewers are looking). Cute but not now.

## Effort

One PR, ~6 files, ~150–200 LOC + tests.
- New endpoint: 10 LOC + 3-4 tests.
- `useTownState.ts` rewire: ~30 LOC delta.
- `scenes.ts` + tests: 5 LOC delta.
