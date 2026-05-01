# Anonymous Viewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let signed-out users see live agents in Town and Library by minting an anonymous viewer token (HMAC-signed via existing `sign_town_token`) and using it to open the WebSocket. Apartment stays auth-gated.

**Architecture:** Backend exposes `GET /town/anonymous-viewer-token` (no auth). Frontend `useTownState` falls back to fetching this when Clerk's `getToken` is absent or returns null, then opens the WS the same way it does for signed-in users. The existing Lambda authorizer accepts the token transparently — no infra change.

**Tech Stack:** FastAPI + pytest, TypeScript + vitest, React, Clerk auth (optional), HMAC tokens.

**Spec:** `docs/superpowers/specs/2026-04-29-anonymous-viewer-design.md`

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `apps/backend/routers/town.py` | **Modify** | Add `GET /town/anonymous-viewer-token` public endpoint (~10 LOC). |
| `apps/backend/tests/unit/test_town_router.py` | **Modify** | 3 tests for the new endpoint (success, token-verifies, ttl≈30min). |
| `apps/frontend/src/scenes.ts` | **Modify** | Flip library row: `requireAuth: false`, `showInNav: 'always'`. |
| `apps/frontend/src/scenes.test.ts` | **Modify** | Update assertions for the new library values. |
| `apps/frontend/src/hooks/useTownState.ts` | **Modify** | Drop `!getToken` early-return; add anonymous fallback fetch + reconnect. |
| `apps/frontend/src/hooks/useTownState.test.ts` | **Modify** | New test: hook fetches anonymous token + opens WS when no Clerk token. |

Net source LOC: ~+45 / -5. Net test LOC: ~+80.

---

## Task 1: Backend — anonymous-viewer-token endpoint (TDD)

**Files:**
- Modify: `apps/backend/routers/town.py`
- Modify: `apps/backend/tests/unit/test_town_router.py` (or wherever existing town-router tests live — check `apps/backend/tests/unit/` for an existing file with `/viewer-token` tests; if no existing file matches, create `apps/backend/tests/unit/test_anonymous_viewer.py`)

- [ ] **Step 1.1: Find the existing test file pattern**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/backend
grep -rn "/viewer-token\|get_viewer_token\|sign_town_token" tests/ | head
```

If `tests/unit/test_town_router.py` exists and has `/viewer-token` tests, add the new tests there. Otherwise create `tests/unit/test_anonymous_viewer.py` following the pattern of nearby test files (e.g., `tests/unit/test_town_token.py`).

- [ ] **Step 1.2: Write the failing tests**

In the chosen test file, add:

```python
import time
from fastapi.testclient import TestClient
from main import app
from core.town_token import verify_town_token


def test_anonymous_viewer_token_endpoint_returns_token():
    """Anonymous viewer endpoint mints a token without requiring auth."""
    client = TestClient(app)
    response = client.get("/api/v1/town/anonymous-viewer-token")
    assert response.status_code == 200
    body = response.json()
    assert "token" in body
    assert isinstance(body["token"], str)
    assert len(body["token"]) > 20  # HMAC tokens are non-trivial


def test_anonymous_viewer_token_verifies_with_sentinel_payload(monkeypatch):
    """The minted token verifies and carries the anonymous sentinel uid/iid."""
    monkeypatch.setenv("TOWN_TOKEN_SECRET", "test-secret-for-unit-tests")
    client = TestClient(app)
    response = client.get("/api/v1/town/anonymous-viewer-token")
    token = response.json()["token"]
    payload = verify_town_token(token)
    assert payload is not None
    assert payload["uid"] == "anonymous"
    assert payload["iid"] == "anonymous-viewer"


def test_anonymous_viewer_token_has_short_ttl(monkeypatch):
    """Anonymous tokens expire ~30 minutes after issue (not the 30-day default)."""
    monkeypatch.setenv("TOWN_TOKEN_SECRET", "test-secret-for-unit-tests")
    client = TestClient(app)
    before = int(time.time())
    response = client.get("/api/v1/town/anonymous-viewer-token")
    after = int(time.time())
    token = response.json()["token"]
    payload = verify_town_token(token)
    assert payload is not None
    exp = int(payload["exp"])
    iat = int(payload["iat"])
    # 30 min = 1800s; allow 5s slack on both sides for test execution.
    assert 1795 <= (exp - iat) <= 1805
    assert before <= iat <= after
```

- [ ] **Step 1.3: Run tests, confirm they fail**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/backend
uv run pytest tests/unit/test_town_router.py::test_anonymous_viewer_token_endpoint_returns_token -v 2>&1 | tail -20
# (or whichever path you chose in 1.1)
```

Expected: 3 FAIL with 404 from FastAPI (route doesn't exist).

- [ ] **Step 1.4: Add the endpoint to `apps/backend/routers/town.py`**

Find the existing `@router.get("/viewer-token")` definition (around line 637). Add the new endpoint immediately after it:

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

    # 30-min TTL: short enough that leaked tokens have minimal blast radius,
    # long enough for a casual viewing session before the WS hook needs to
    # refetch on reconnect.
    token = sign_town_token("anonymous", "anonymous-viewer", ttl_seconds=1800)
    return {"token": token}
```

- [ ] **Step 1.5: Run tests, confirm pass**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/backend
uv run pytest tests/unit/test_town_router.py -k anonymous_viewer -v 2>&1 | tail -20
```

Expected: 3 PASS.

- [ ] **Step 1.6: Run full backend suite + ruff**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/backend
uv run pytest tests/ -q 2>&1 | tail -15
uv run ruff check . && uv run ruff format --check .
```

Expected: all green, no regressions, ruff clean.

- [ ] **Step 1.7: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync
git add apps/backend/routers/town.py apps/backend/tests/
git commit -m "feat(backend): add /town/anonymous-viewer-token public endpoint

Mints HMAC-signed viewer tokens with sentinel uid='anonymous' /
iid='anonymous-viewer' and 30-min TTL. The existing Lambda authorizer
accepts town tokens regardless of uid value, so no infra change is
required. Frontend will use this when no Clerk session is available.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Frontend — flip library to public in scenes.ts

**Files:**
- Modify: `apps/frontend/src/scenes.ts`
- Modify: `apps/frontend/src/scenes.test.ts`

- [ ] **Step 2.1: Edit `scenes.ts`**

In `apps/frontend/src/scenes.ts`, find the SCENES array. The library row currently reads:

```ts
{ id: 'library',   displayName: 'Library',   route: '/library',   requireAuth: true,  showInNav: 'signed_in' },
```

Replace with:

```ts
{ id: 'library',   displayName: 'Library',   route: '/library',   requireAuth: false, showInNav: 'always'    },
```

(Same alignment as the `town` row.)

- [ ] **Step 2.2: Update `scenes.test.ts`**

Find the test "library and apartment require auth, town does not". Update its assertions to reflect that library is now public:

```ts
it('apartment requires auth; town and library do not', () => {
  expect(sceneById('town')?.requireAuth).toBe(false);
  expect(sceneById('apartment')?.requireAuth).toBe(true);
  expect(sceneById('library')?.requireAuth).toBe(false);
});
```

Also find the test "falls back to 'town' when path requires auth but user is signed out" — it currently asserts BOTH `/library` and `/apartment` redirect. Library no longer redirects, so split into:

```ts
it('falls back to "town" when /apartment requested while signed out', () => {
  expect(sceneForPath('/apartment', false)).toBe('town');
});

it('returns "library" when /library requested while signed out', () => {
  // Library is public — signed-out users see the same library.
  expect(sceneForPath('/library', false)).toBe('library');
});
```

- [ ] **Step 2.3: Run tests + typecheck + lint**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/frontend
pnpm test -- scenes.test.ts && pnpm typecheck && pnpm lint
```

Expected: all green.

- [ ] **Step 2.4: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync
git add apps/frontend/src/scenes.ts apps/frontend/src/scenes.test.ts
git commit -m "refactor(frontend): make library a public route like town

Drops requireAuth + showInNav: signed_in for library. Signed-out
users will be able to navigate to /library and see the canvas.
World-state subscription via the WS still requires a token —
anonymous fallback is wired in the next commit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Frontend — useTownState anonymous fallback

**Files:**
- Modify: `apps/frontend/src/hooks/useTownState.ts`
- Modify: `apps/frontend/src/hooks/useTownState.test.ts`

- [ ] **Step 3.1: Edit `useTownState.ts`**

The current shape (around lines 64-85):

```ts
useEffect(() => {
  if (!WS_URL || !getToken) return;

  let ws: WebSocket | null = null;
  let cancelled = false;
  let reconnectDelay = 1000;

  const connect = async () => {
    if (cancelled) return;
    let token: string | null = null;
    try {
      token = await getToken();
    } catch {
      /* retry */
    }
    if (cancelled) return;
    if (!token) {
      setTimeout(connect, reconnectDelay);
      return;
    }

    ws = new WebSocket(`${WS_URL}?token=${token}`);
    // ...
  };
```

Replace the early-return + connect body. The full replacement:

```ts
useEffect(() => {
  if (!WS_URL) return;

  let ws: WebSocket | null = null;
  let cancelled = false;
  let reconnectDelay = 1000;

  const connect = async () => {
    if (cancelled) return;
    let token: string | null = null;

    // Try Clerk-signed viewer token first (signed-in users).
    if (getToken) {
      try {
        token = await getToken();
      } catch {
        /* fall through to anonymous */
      }
    }

    // Fall back to anonymous viewer token (signed-out users, or Clerk
    // ``getToken`` failure). The endpoint is public; the resulting token
    // grants WS subscription only — no act / chat / register paths.
    if (!token && !cancelled) {
      try {
        const r = await fetch(`${API_URL}/town/anonymous-viewer-token`);
        if (r.ok) {
          const body: { token?: string } = await r.json();
          token = body.token ?? null;
        }
      } catch {
        /* retry on next reconnect */
      }
    }

    if (cancelled) return;
    if (!token) {
      setTimeout(connect, reconnectDelay);
      return;
    }

    ws = new WebSocket(`${WS_URL}?token=${token}`);
    // ... rest of onopen / onmessage / onclose unchanged ...
```

The dependency array should remain `[getToken, processState, userId]` — we still want re-connect when `getToken` changes (e.g., user signs in/out).

- [ ] **Step 3.2: Add a vitest case for the anonymous path**

The existing `useTownState.test.ts` already mocks `WebSocket` and exercises the WS path. Add a new test (placed after the existing WS tests):

```ts
it('falls back to anonymous-viewer-token when no Clerk getToken is provided', async () => {
  const fetchMock = vi.fn(async (url: string) => {
    if (url.includes('/anonymous-viewer-token')) {
      return new Response(JSON.stringify({ token: 'anon-test-token' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    return new Response(JSON.stringify({}), { status: 200 });
  });
  vi.stubGlobal('fetch', fetchMock);

  // No getToken → hook should reach the anonymous fallback.
  renderHook(() => useTownState());

  await waitFor(() => {
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/town/anonymous-viewer-token'),
    );
  });

  vi.unstubAllGlobals();
});
```

If the existing test file doesn't already mock `WebSocket`, look for how it does — there should be a stub helper or beforeEach. Reuse it.

- [ ] **Step 3.3: Run tests + typecheck + lint**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/frontend
pnpm test && pnpm typecheck && pnpm lint
```

Expected: all green.

- [ ] **Step 3.4: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync
git add apps/frontend/src/hooks/useTownState.ts apps/frontend/src/hooks/useTownState.test.ts
git commit -m "feat(frontend): anonymous WS fallback for signed-out viewers

When Clerk getToken is absent or returns null, fetch
/town/anonymous-viewer-token and use it to open the WebSocket.
Signed-out users now see live world_state in town and library
(apartment stays auth-gated by route guard).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Open PR + smoke test

- [ ] **Step 4.1: Push the branch and open PR**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync
git push -u origin HEAD
gh pr create --title "feat(anonymous-viewer): public WS access for town and library" --body "$(cat <<'EOF'
## Summary

Lets signed-out users see live agent state on \`/\` and \`/library\`.

- Backend: new public \`GET /town/anonymous-viewer-token\` mints a short-lived (30 min) HMAC-signed token. Existing Lambda authorizer accepts it transparently.
- Frontend: \`scenes.ts\` flips library to public; \`useTownState\` falls back to anonymous token when Clerk \`getToken\` is absent.
- Apartment stays auth-gated (\`requireAuth: true\`); per-user privacy preserved.

Spec: \`docs/superpowers/specs/2026-04-29-anonymous-viewer-design.md\`

## Test plan
- [ ] Backend: \`uv run pytest\` passes (incl. 3 new endpoint tests)
- [ ] Frontend: \`pnpm test && pnpm typecheck && pnpm lint\` passes (incl. new anonymous fallback test)
- [ ] Dev deploy → sign out → visit \`/\` → see agents in town
- [ ] Sign out → click \`Library\` → see agents in library SubViewport
- [ ] Sign out → direct-nav \`/apartment\` → redirected to \`/\`
- [ ] Sign back in → all flows still work, no regression

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4.2: Wait for CI green**

Watch the run via `gh pr checks <PR#>` until all checks pass. Per project rule, wait for codex review before merging unless the user authorizes a fast merge.

- [ ] **Step 4.3: Smoke test on dev (after merge + deploy)**

1. Open Chrome at `https://dev.goosetown.isol8.co/` in an incognito window (or sign out first).
2. Confirm `Town` and `Library` nav links visible. `Apartment` should NOT be visible (signed-out).
3. Drive an agent into town: from a signed-in test workspace, `act move plaza` (or already-running dwight). Confirm visible in Town canvas while you (the chrome window) are signed-out.
4. Click `Library`. Drive the same agent: `act move library`. Confirm visible inside the library scene.
5. In the same incognito window, type `/apartment` in the URL bar. Confirm redirect to `/`.
6. Open a normal (non-incognito) window, sign in, verify `Apartment` link reappears and the apartment view still works.

If steps 1-5 succeed, the feature is shipped.

---

## Self-review notes

**Spec coverage check:**
- Backend endpoint: Task 1 ✅
- Frontend scenes.ts flip: Task 2 ✅
- Frontend useTownState fallback: Task 3 ✅
- Apartment route stays auth-gated (no change needed — Task 2 only touches the library row): ✅
- All tests: Tasks 1, 2, 3 ✅
- Manual smoke: Task 4 ✅

**Out-of-spec scope:**
- No per-user rate limiting added (matches non-goal #2).
- No "sign in to interact" UI nudge (matches non-goal #3).
- No infra (CDK) changes — authorizer accepts the token unchanged.
- No agent-flow changes (anonymous tokens never used by agent daemons).

**Type-consistency:**
- `sign_town_token(uid, iid, ttl_seconds=...)` — verified the parameter names match the existing helper.
- `verify_town_token(token)` returns `dict | None` with `uid`, `iid`, `iat`, `exp`.
- `useTownState({getToken?, userId?})` signature unchanged externally.

No placeholders.
