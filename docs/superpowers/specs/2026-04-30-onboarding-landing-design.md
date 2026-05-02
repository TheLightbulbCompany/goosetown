# GooseTown Onboarding & Landing (Sub-project 5) — Design Spec

**Date:** 2026-04-30
**Status:** Pending implementation
**Branch:** `spec/onboarding-landing` (already checked out)

## Context

Sub-project 5 of 5 in the public-launch path. Sequence:

1. ✅ Skill packaging & distribution (PR #251)
2. ✅ Production hardening (PR #252)
3. ✅ Multi-tenant app-level (PR #253)
4. **Personal launch dogfood (deferred until sub 5 ships)** — sub 4 needs the surfaces sub 5 builds in order to test "every single user flow"
5. **Onboarding & landing (this spec)**

User direction 2026-05-01: do sub 5 BEFORE sub 4 because the dogfood only validates the platform if it exercises the actual user-facing surfaces — landing, signup, quota visibility, sprite-wait UX. Without those, the soak tests a stripped-down version of the product.

## Goal

A first-time visitor lands at `goosetown.isol8.co/`, sees what GooseTown is, can either sign up via Clerk to start an agent, drop their email for launch updates, OR click through to see the live town. A signed-in user lands directly in the town and can see their quota state, watch sprites generate, revoke tokens prominently, and paste tokens cleanly.

## Success criteria

- [ ] **Anonymous visitor at `/`** sees a landing page with hero copy, an email-signup form (Resend-backed), and a "See the town live →" link to `/town`.
- [ ] **Email signup form** at `/landing/signup`: validates email format, calls Resend Audiences API with the visitor's email, returns 200 on success / 429 on rate-limit / 400 on invalid email.
- [ ] **Signed-in visitor at `/`** is redirected to `/town` (so they don't see the landing every time they visit).
- [ ] **Anonymous visitor at `/town`** sees the existing public viewer (PR #243) — no change to that flow.
- [ ] **Quota visibility on agent sidebar:** "3/5 agents", "4/10 sprite-gens today" labels next to the existing agent count. Updates from `GET /town/apartment` (existing).
- [ ] **Sprite-wait UX on ApartmentCard:** while `spriteReady=false`, the card shows "Drawing your agent..." with elapsed seconds, polling `/town/agent/status`. When `spriteReady=true`, the sprite swap-fades in.
- [ ] **RevokeTokenButton surfaced** out of the "Advanced" details pane in `JoinTownModal` — visible alongside the token display with no extra clicks needed.
- [ ] **Token-paste UX cleanup:** copy-token button on JoinTownModal with a "Copied!" feedback state. (Auto-detect-when-pasted is out of scope — requires a daemon-side mechanism.)
- [ ] All new and modified surfaces tested with `vitest`.

## Architecture

### Route changes

| Route | Today | After sub 5 |
|---|---|---|
| `/` | town view (Godot canvas) | landing page (signed-out) OR redirect to `/town` (signed-in) |
| `/town` | does not exist | town view (Godot canvas) — anonymous viewer + signed-in agent rendering |
| `/sign-in/*` | Clerk SignIn | unchanged |
| `/sign-up/*` | Clerk SignUp | unchanged |
| `/apartment` | apartment view | unchanged |

The town view component (`<Town/>`) doesn't change behavior — only its mounting route changes.

### Email signup flow

```
User submits form at /
         │
         ▼
POST /api/v1/landing/signup  {email, honeypot?}
         │
         ▼
Backend validates:
  - email regex
  - honeypot empty
  - per-IP rate limit (WAF already handles broader flood)
         │
         ▼
Backend POSTs to Resend Audiences API:
  POST https://api.resend.com/audiences/{audience_id}/contacts
  Authorization: Bearer {RESEND_API_KEY}
  Body: {"email": "<email>", "unsubscribed": false}
         │
         ▼
Returns 200 on success.
On Resend 4xx: log + return our own 4xx (don't leak Resend's error shape).
On Resend 5xx: log + return our own 503 ("temporarily unavailable, try again").
```

**Why no DDB storage:** Resend Audiences IS the source of truth. We don't keep a local copy. If we ever lose the Resend account, we lose the list — acceptable for a launch list. Reduces moving parts and avoids the dual-write consistency problem.

### Resend integration

- New env var on the backend: `RESEND_API_KEY` (Secrets Manager in prod; `.env` in dev — already covered by the secrets pattern from PR #214)
- New env var: `RESEND_AUDIENCE_ID` (which audience to add to; one per env: dev + prod)
- New backend module: `apps/backend/core/services/resend_client.py` — thin wrapper around `httpx` POST to the Audiences API
- One operation: `add_contact(email: str) -> None`. Raises `ResendError` on non-2xx (handled by router → mapped to user-facing HTTP status).

The Resend SDK exists for Node but not Python; we use `httpx` directly per the existing `pixellab_service.py` pattern (also direct-API, no SDK).

### Frontend landing page

New file: `apps/frontend/src/pages/Landing.tsx`. Single component, no router child. Uses:
- Plain Tailwind for the hero section
- A `LandingSignupForm` component for the email field (its own file for testability)
- A "See the town live →" anchor pointing at `/town`

No CMS, no MDX, no markdown. Copy lives in the JSX file. User can edit copy directly without engineering changes.

### Quota visibility

`AgentsSidebar.tsx` (existing) gains a small footer block. Data sources:
- Agent count: `useApartment()` already returns the user's active agents — count `data.agents.length` against `5`.
- Sprite-gen daily: NEW backend endpoint `GET /town/quota` that returns `{agents: {used, limit}, sprite_gen: {used_24h, limit}}` — reads from the `users` table via `UserService` (sub 3 work).

### Sprite-wait UX

`ApartmentCard.tsx` checks `spriteReady`. When false:
- Render placeholder (existing behavior)
- ADD an overlay: "Drawing your agent (Xs)..." where X is elapsed seconds since the agent record was created (`createdAt` from the agent row)
- Poll `useApartment()` (already polls every 2s) — when `spriteReady=true`, swap-fade in the new sprite

When sprite-gen errors out (rare; PixelLab credit exhaustion), backend marks the agent with a `spriteError` field. UI shows "Sprite generation failed — contact support" instead of waiting indefinitely.

### Surface RevokeTokenButton

`JoinTownModal.tsx` — move `<RevokeTokenButton/>` out of the `<details>` block to a top-level "Advanced" section visible without clicking. Same component, same prop, just less hidden.

### Token-paste UX cleanup

`JoinTownModal.tsx` — the existing instruction string (with the token embedded) gets a "Copy" button next to it. Click → writes to clipboard via `navigator.clipboard.writeText`, shows "Copied!" badge for 2s. No backend change.

## Components

### `core/services/resend_client.py` (new)

```python
class ResendError(Exception):
    """Raised on non-2xx response from Resend API."""
    def __init__(self, status: int, body: str):
        super().__init__(f"Resend API {status}: {body[:200]}")
        self.status = status

class ResendClient:
    def __init__(self, api_key: str, audience_id: str):
        self.api_key = api_key
        self.audience_id = audience_id

    async def add_contact(self, email: str) -> None:
        # POST to /audiences/{audience_id}/contacts
        # Raise ResendError on non-2xx
        ...
```

### `routers/landing.py` (new)

`POST /landing/signup` — accepts `{email, honeypot?}`, validates, calls `ResendClient.add_contact`, returns 200 / 4xx / 503.

`GET /landing/healthz` — trivial health check (returns 200 unconditionally) so we can monitor the endpoint without auth.

### `routers/town.py` modifications

`GET /town/quota` — new endpoint. Reads user's agent count + sprite_jobs_24h from the `users` table. Returns the structured payload above.

### Frontend new files

- `apps/frontend/src/pages/Landing.tsx`
- `apps/frontend/src/components/LandingSignupForm.tsx`
- `apps/frontend/src/components/QuotaIndicator.tsx`
- `apps/frontend/src/components/SpriteWaitOverlay.tsx`
- Each with a `.test.tsx` sibling

### Frontend modifications

- `apps/frontend/src/App.tsx` — route changes (`/` is landing; `/town` is the town)
- `apps/frontend/src/components/AgentsSidebar.tsx` — mount `<QuotaIndicator/>`
- `apps/frontend/src/components/ApartmentCard.tsx` — mount `<SpriteWaitOverlay/>` while `spriteReady=false`
- `apps/frontend/src/components/JoinTownModal.tsx` — move RevokeTokenButton, add copy-token button

## Out of scope

- **Marketing copy / hero animations / feature gallery** — sub 5 ships placeholder copy; user provides final wording later
- **Auto-detect when token is pasted into agent's chat** — requires daemon-side hooks; defer
- **Onboarding tour / first-run wizard** — defer
- **Agent management page (edit personality, delete from UI)** — defer
- **Local DDB mirror of Resend contacts** — punt; Resend is source of truth
- **Email confirmation / double opt-in** — Resend handles this if we configure it; sub 5 doesn't gate signup on confirmation

## Risks

- **Resend free tier (3,000/month, 100/day)** is fine for early signups but caps out fast if we go viral. Mitigation: monitor usage, upgrade to paid if needed.
- **Anonymous visitor signup spam** — WAF rate-limit (sub 2) handles flood; the honeypot field handles dumb bots. Sophisticated bots will get through; acceptable for a launch list.
- **Quota endpoint latency** — adds a DDB round-trip per `useApartment()` poll. Mitigation: cache for 5s in-memory on the frontend; the data isn't time-critical.
- **Sprite-wait timer lying** — `createdAt` is set on the agent record, not on the sprite-gen job kickoff. There's a small lag. Acceptable.
- **Resend API outage** — landing form rejects with 503. Acceptable; user can retry. We don't queue locally.
- **Route move breaks existing bookmarks** — anyone with `goosetown.isol8.co/` bookmarked thinking it's the town will hit the landing first. Mitigation: the "See the town live →" link is prominent; once they click and bookmark `/town`, they're set.
