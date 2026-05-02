# Onboarding & Landing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a public landing page at `/` with a Resend-backed email signup form, move the town view to `/town`, and expose quota state + sprite-wait UX so sub 4 dogfood can validate every user-facing flow.

**Architecture:** Frontend gets a new `/` (landing page with email form + "See the town" CTA) while town moves to `/town`. Signed-in users redirect from `/` to `/town`. Backend exposes `POST /landing/signup` that proxies to Resend Audiences API via httpx (no SDK), and `GET /town/quota` that surfaces sub-3 quota counters from the `users` table. Component-level additions: `QuotaIndicator`, `SpriteWaitOverlay`. RevokeTokenButton moves out of "Advanced" pane.

**Tech Stack:** Python 3.12 (FastAPI, httpx), AWS CDK (TypeScript, Secrets Manager), React 18 + TypeScript + Tailwind, vitest, pytest, Resend Audiences API.

---

## File Structure

**Created (backend):**
- `apps/backend/core/services/resend_client.py` — `ResendClient.add_contact()`
- `apps/backend/routers/landing.py` — `POST /landing/signup` + `GET /landing/healthz`
- `apps/backend/tests/unit/test_resend_client.py`
- `apps/backend/tests/unit/test_landing_endpoint.py`
- `apps/backend/tests/unit/test_quota_endpoint.py`

**Created (frontend):**
- `apps/frontend/src/pages/Landing.tsx`
- `apps/frontend/src/pages/Landing.test.tsx`
- `apps/frontend/src/components/LandingSignupForm.tsx`
- `apps/frontend/src/components/LandingSignupForm.test.tsx`
- `apps/frontend/src/components/QuotaIndicator.tsx`
- `apps/frontend/src/components/QuotaIndicator.test.tsx`
- `apps/frontend/src/components/SpriteWaitOverlay.tsx`
- `apps/frontend/src/components/SpriteWaitOverlay.test.tsx`
- `apps/frontend/src/hooks/useQuota.ts`

**Modified (backend):**
- `apps/backend/main.py` — register `landing` router
- `apps/backend/routers/town.py` — add `GET /town/quota`
- `apps/backend/core/config.py` — add `RESEND_API_KEY` and `RESEND_AUDIENCE_ID` settings

**Modified (infra):**
- `apps/infra/lib/stacks/auth-stack.ts` — add `ResendApiKey` Secret + expose on `AuthSecrets`
- `apps/infra/lib/stacks/compute-stack.ts` — pass `RESEND_API_KEY` (from secret) and `RESEND_AUDIENCE_ID` (env) to backend container

**Modified (frontend):**
- `apps/frontend/src/scenes.ts` — town route changes from `/` to `/town`
- `apps/frontend/src/App.tsx` — `/` renders `<Landing/>` for signed-out, redirects to `/town` for signed-in
- `apps/frontend/src/components/AgentsSidebar.tsx` — mount `<QuotaIndicator/>`
- `apps/frontend/src/components/ApartmentCard.tsx` — mount `<SpriteWaitOverlay/>` while `spriteReady=false`
- `apps/frontend/src/components/JoinTownModal.tsx` — surface RevokeTokenButton out of `<details>`, add copy-token button

---

## Task 1: ResendClient + add_contact (TDD)

**Files:**
- Create: `apps/backend/core/services/resend_client.py`
- Create: `apps/backend/tests/unit/test_resend_client.py`

- [ ] **Step 1: Write failing tests**

```python
# apps/backend/tests/unit/test_resend_client.py
"""TDD: ResendClient.add_contact wraps the Resend Audiences API."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from core.services.resend_client import ResendClient, ResendError


@pytest.mark.asyncio
async def test_add_contact_posts_to_resend_audiences_endpoint():
    client = ResendClient(api_key="test-key", audience_id="aud-123")
    mock_response = httpx.Response(200, json={"id": "contact-abc"})

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_response)) as mock_post:
        await client.add_contact("user@example.com")

    mock_post.assert_called_once()
    call_args = mock_post.call_args
    assert "audiences/aud-123/contacts" in call_args.args[0]
    assert call_args.kwargs["headers"]["Authorization"] == "Bearer test-key"
    assert call_args.kwargs["json"]["email"] == "user@example.com"
    assert call_args.kwargs["json"]["unsubscribed"] is False


@pytest.mark.asyncio
async def test_add_contact_raises_on_4xx():
    client = ResendClient(api_key="test-key", audience_id="aud-123")
    mock_response = httpx.Response(400, json={"message": "Invalid email"})

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(ResendError) as exc:
            await client.add_contact("bad-email")
    assert exc.value.status == 400


@pytest.mark.asyncio
async def test_add_contact_raises_on_5xx():
    client = ResendClient(api_key="test-key", audience_id="aud-123")
    mock_response = httpx.Response(503, text="Service Unavailable")

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(ResendError) as exc:
            await client.add_contact("user@example.com")
    assert exc.value.status == 503


@pytest.mark.asyncio
async def test_add_contact_treats_already_exists_as_success():
    """Resend returns a specific 'contact already exists' shape; treat as success.

    This makes the endpoint idempotent — re-submitting the same email doesn't
    surface as an error to the user.
    """
    client = ResendClient(api_key="test-key", audience_id="aud-123")
    # Resend's "already exists" can come back as 422 with a specific message.
    mock_response = httpx.Response(
        422, json={"name": "validation_error", "message": "Contact already exists."}
    )

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_response)):
        # Should NOT raise
        await client.add_contact("dup@example.com")
```

- [ ] **Step 2: Run, confirm fails**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/backend
uv run pytest tests/unit/test_resend_client.py -v
```

Expected: ImportError on `core.services.resend_client`.

- [ ] **Step 3: Implement ResendClient**

```python
# apps/backend/core/services/resend_client.py
"""Thin client for the Resend Audiences API.

Used by the /landing/signup endpoint to add email contacts to a Resend
audience (the launch list). No SDK — direct httpx POST per the existing
pixellab_service.py pattern.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

RESEND_API_BASE = "https://api.resend.com"


class ResendError(Exception):
    """Raised on non-2xx response from Resend that we don't treat as success."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Resend API {status}: {body[:200]}")
        self.status = status


class ResendClient:
    def __init__(self, api_key: str, audience_id: str) -> None:
        self.api_key = api_key
        self.audience_id = audience_id

    async def add_contact(self, email: str) -> None:
        """Add a contact to the configured audience.

        Idempotent: a 422 response with 'already exists' is treated as success
        (Resend's signal for duplicate). Other non-2xx responses raise.
        """
        url = f"{RESEND_API_BASE}/audiences/{self.audience_id}/contacts"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url,
                json={"email": email, "unsubscribed": False},
                headers={"Authorization": f"Bearer {self.api_key}"},
            )

        if 200 <= response.status_code < 300:
            return

        # Treat duplicate-email 422 as idempotent success.
        if response.status_code == 422:
            try:
                body_json = response.json()
                if "already exists" in body_json.get("message", "").lower():
                    logger.info("Resend contact already exists for %s — treating as success", email)
                    return
            except Exception:
                pass

        raise ResendError(response.status_code, response.text)
```

- [ ] **Step 4: Run, verify pass**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/backend
uv run pytest tests/unit/test_resend_client.py -v
```

Expected: 4/4 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync
git add apps/backend/core/services/resend_client.py apps/backend/tests/unit/test_resend_client.py
git commit -m "feat(backend): ResendClient — thin wrapper around Resend Audiences API

Sub 5 of public-launch path. Used by the upcoming /landing/signup endpoint
to add email contacts to a Resend audience. No SDK — direct httpx POST per
the existing pixellab_service.py pattern.

Idempotent: 422 'already exists' is treated as success so re-submitting
the same email doesn't surface as an error to the user.

4 unit tests cover: happy path, 4xx raise, 5xx raise, dup-email idempotency.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Add Resend secrets to CDK

**Files:**
- Modify: `apps/infra/lib/stacks/auth-stack.ts` — add `ResendApiKey` Secret
- Modify: `apps/infra/lib/stacks/compute-stack.ts` — pass env to backend container
- Modify: `apps/backend/core/config.py` — add settings

- [ ] **Step 1: Add Resend API key secret to AuthStack**

In `apps/infra/lib/stacks/auth-stack.ts`, after the existing `pixelLabApiKey` declaration, add:

```typescript
    const resendApiKey = new secretsmanager.Secret(this, "ResendApiKey", {
      secretName: `goosetown-${env}-resend-api-key`,
      description: "Resend API key for landing page email signups",
    });
```

Update the `AuthSecrets` interface to include it:

```typescript
export interface AuthSecrets {
  clerkIssuer: secretsmanager.ISecret;
  clerkSecretKey: secretsmanager.ISecret;
  townTokenSecret: secretsmanager.ISecret;
  pixelLabApiKey: secretsmanager.ISecret;
  resendApiKey: secretsmanager.ISecret;
}
```

Update `this.secrets`:

```typescript
this.secrets = {
  clerkIssuer,
  clerkSecretKey,
  townTokenSecret,
  pixelLabApiKey,
  resendApiKey,
};
```

- [ ] **Step 2: Wire Resend secret into ComputeStack container env**

In `apps/infra/lib/stacks/compute-stack.ts`, find the existing pattern that injects `PIXELLAB_API_KEY`:

```bash
grep -n "PIXELLAB_API_KEY\|pixelLabApiKey\|secrets\." /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/infra/lib/stacks/compute-stack.ts | head -10
```

Mirror that pattern for `RESEND_API_KEY` (read from secret) and add a plain env var `RESEND_AUDIENCE_ID`:

```typescript
secrets: {
  // ... existing secrets ...
  RESEND_API_KEY: ecs.Secret.fromSecretsManager(props.secrets.resendApiKey),
},
environment: {
  // ... existing env ...
  RESEND_AUDIENCE_ID: env === "prod" ? "TBD-prod-audience-id" : "TBD-dev-audience-id",
},
```

The `TBD-{env}-audience-id` placeholders are intentional — the operator (you) creates audiences in the Resend dashboard once, then updates these strings in `compute-stack.ts` and redeploys. Document this in a comment.

Add a comment block explaining the operator step:

```typescript
// RESEND_AUDIENCE_ID values are set after the Resend account is created and
// audiences are provisioned (one per env). Update the strings below and
// redeploy. The RESEND_API_KEY secret is populated via:
//   aws secretsmanager put-secret-value --secret-id goosetown-{env}-resend-api-key \
//     --secret-string '<resend api key>'
```

- [ ] **Step 3: Add settings to backend config**

In `apps/backend/core/config.py`, add to the `Settings` class (or equivalent):

```python
RESEND_API_KEY: str = ""  # populated from Secrets Manager via ECS env in prod; .env in dev
RESEND_AUDIENCE_ID: str = ""  # set per env in compute-stack.ts
```

(Match the existing field-naming pattern in the file. If it uses pydantic-settings, mirror that.)

- [ ] **Step 4: Verify cdk synth**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/infra
npx cdk synth 2>&1 | tail -5
```

Expected: clean synth.

- [ ] **Step 5: Run infra tests**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/infra
npm test 2>&1 | tail -5
```

Expected: same pass count; if `auth-stack.test.ts` asserts a specific number of secrets, update.

- [ ] **Step 6: Run backend tests**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/backend
uv run pytest tests/ 2>&1 | tail -3
```

Expected: same count.

- [ ] **Step 7: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync
git add apps/infra/ apps/backend/core/config.py
git commit -m "feat(infra,backend): plumb Resend API key + audience ID into backend

New ResendApiKey secret in AuthStack. ComputeStack passes RESEND_API_KEY
(from secret) and RESEND_AUDIENCE_ID (plain env, one per env) to the
backend container. Backend Settings adds the two fields.

Operator step (one-time, post-merge):
  1. Create Resend account + audiences (one for dev, one for prod)
  2. aws secretsmanager put-secret-value to populate the secret
  3. Update RESEND_AUDIENCE_ID strings in compute-stack.ts and redeploy

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: POST /landing/signup endpoint (TDD)

**Files:**
- Create: `apps/backend/routers/landing.py`
- Create: `apps/backend/tests/unit/test_landing_endpoint.py`
- Modify: `apps/backend/main.py` — register the router

- [ ] **Step 1: Write failing tests**

```python
# apps/backend/tests/unit/test_landing_endpoint.py
"""TDD: POST /landing/signup proxies to Resend with validation + rate limiting."""

from unittest.mock import AsyncMock, patch

import pytest

from core.services.resend_client import ResendError


def test_signup_returns_200_on_success(test_client):
    with patch("routers.landing.ResendClient") as mock_cls:
        mock_cls.return_value.add_contact = AsyncMock(return_value=None)
        response = test_client.post(
            "/api/v1/landing/signup",
            json={"email": "user@example.com"},
        )
    assert response.status_code == 200
    mock_cls.return_value.add_contact.assert_awaited_once_with("user@example.com")


def test_signup_returns_400_on_invalid_email(test_client):
    response = test_client.post(
        "/api/v1/landing/signup",
        json={"email": "not-an-email"},
    )
    assert response.status_code == 400
    assert "email" in response.json().get("detail", "").lower()


def test_signup_rejects_when_honeypot_filled(test_client):
    """Bots that fill the hidden honeypot field are silently dropped (200, no Resend call)."""
    with patch("routers.landing.ResendClient") as mock_cls:
        mock_cls.return_value.add_contact = AsyncMock(return_value=None)
        response = test_client.post(
            "/api/v1/landing/signup",
            json={"email": "user@example.com", "honeypot": "i-am-a-bot"},
        )
    # Return 200 to avoid signaling to the bot it was caught
    assert response.status_code == 200
    # But no Resend call
    mock_cls.return_value.add_contact.assert_not_awaited()


def test_signup_returns_503_on_resend_5xx(test_client):
    with patch("routers.landing.ResendClient") as mock_cls:
        mock_cls.return_value.add_contact = AsyncMock(
            side_effect=ResendError(503, "Service Unavailable")
        )
        response = test_client.post(
            "/api/v1/landing/signup",
            json={"email": "user@example.com"},
        )
    assert response.status_code == 503


def test_signup_returns_400_on_resend_4xx(test_client):
    with patch("routers.landing.ResendClient") as mock_cls:
        mock_cls.return_value.add_contact = AsyncMock(
            side_effect=ResendError(400, "Invalid email")
        )
        response = test_client.post(
            "/api/v1/landing/signup",
            json={"email": "user@example.com"},
        )
    assert response.status_code == 400


def test_landing_healthz_returns_200(test_client):
    response = test_client.get("/api/v1/landing/healthz")
    assert response.status_code == 200
```

- [ ] **Step 2: Run, confirm fails**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/backend
uv run pytest tests/unit/test_landing_endpoint.py -v
```

Expected: 404s — endpoint doesn't exist yet.

- [ ] **Step 3: Create the router**

```python
# apps/backend/routers/landing.py
"""Public landing-page endpoints. No auth — protected by WAF rate limits + honeypot."""

import logging
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr, Field

from core.config import settings
from core.services.resend_client import ResendClient, ResendError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/landing", tags=["landing"])

# Conservative-but-permissive email regex. Real validation = SMTP MX lookup,
# but for a launch-list signup we just need to filter blatant garbage.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SignupRequest(BaseModel):
    email: str = Field(..., max_length=320)
    honeypot: str | None = None


@router.post("/signup", status_code=200)
async def signup(req: SignupRequest):
    # Honeypot: if filled, return 200 silently (don't signal to the bot).
    if req.honeypot:
        logger.info("landing signup honeypot triggered")
        return {"status": "ok"}

    # Email format
    if not _EMAIL_RE.match(req.email):
        raise HTTPException(status_code=400, detail="Invalid email")

    if not settings.RESEND_API_KEY or not settings.RESEND_AUDIENCE_ID:
        # Misconfigured backend — fail-closed with a clear server log.
        logger.error("RESEND_API_KEY or RESEND_AUDIENCE_ID is unset; landing signup disabled")
        raise HTTPException(status_code=503, detail="Signup temporarily unavailable")

    client = ResendClient(
        api_key=settings.RESEND_API_KEY,
        audience_id=settings.RESEND_AUDIENCE_ID,
    )

    try:
        await client.add_contact(req.email)
    except ResendError as exc:
        if 400 <= exc.status < 500:
            raise HTTPException(status_code=400, detail="Could not add to list") from exc
        raise HTTPException(status_code=503, detail="Signup temporarily unavailable") from exc

    return {"status": "ok"}


@router.get("/healthz", status_code=200)
async def healthz():
    """Trivial health check; no auth, used by external monitors."""
    return {"status": "ok"}
```

- [ ] **Step 4: Register the router in main.py**

In `apps/backend/main.py`, find the existing router registrations (likely `app.include_router(town.router, ...)`) and add:

```python
from routers import landing
app.include_router(landing.router, prefix="/api/v1")
```

(Match the existing prefix/include pattern — if other routers use `prefix="/api/v1"`, mirror; if they use a different shape, adapt.)

- [ ] **Step 5: Run tests, verify pass**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/backend
uv run pytest tests/unit/test_landing_endpoint.py -v
uv run pytest tests/ 2>&1 | tail -3
```

Expected: 6/6 new tests PASS, full suite at baseline + 6.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/routers/landing.py apps/backend/main.py apps/backend/tests/unit/test_landing_endpoint.py
git commit -m "feat(backend): POST /landing/signup proxies to Resend Audiences API

Public endpoint (no auth, WAF-rate-limited). Validates email format,
silently drops honeypot submissions, calls ResendClient.add_contact,
maps Resend 4xx -> 400 and 5xx -> 503. Misconfigured backend
(missing API key / audience ID) returns 503 with server-side log.

Plus GET /landing/healthz for trivial monitoring.

6 unit tests cover: happy path, invalid email, honeypot, Resend 5xx,
Resend 4xx, healthz.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: GET /town/quota endpoint (TDD)

**Files:**
- Modify: `apps/backend/routers/town.py` — new endpoint
- Create: `apps/backend/tests/unit/test_quota_endpoint.py`

- [ ] **Step 1: Write failing tests**

```python
# apps/backend/tests/unit/test_quota_endpoint.py
"""TDD: GET /town/quota exposes per-user agent + sprite quota state."""


def test_quota_returns_structured_payload(test_client, mock_clerk_jwt):
    headers = {"Authorization": f"Bearer {mock_clerk_jwt}"}
    response = test_client.get("/api/v1/town/quota", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert "agents" in body
    assert "sprite_gen" in body
    assert body["agents"]["limit"] == 5
    assert isinstance(body["agents"]["used"], int)
    assert body["sprite_gen"]["limit"] == 10
    assert isinstance(body["sprite_gen"]["used_24h"], int)


def test_quota_requires_auth(test_client):
    response = test_client.get("/api/v1/town/quota")
    assert response.status_code == 401


def test_quota_reflects_active_agent_count(test_client, mock_clerk_jwt, dynamodb_tables):
    """Pre-seed 3 active agents for the user; expect used=3."""
    from core.services.user_service import UserService
    from core.services.town_service import TownService

    # Seed via existing service helpers
    user_id = "user_for_clerk_jwt"  # whatever mock_clerk_jwt resolves to
    UserService().get_or_create(user_id)
    svc = TownService()
    for i in range(3):
        svc.opt_in(
            user_id=user_id,
            agent_name=f"q{i}",
            personality="t",
            appearance="t",
            traits="introvert",
        )

    headers = {"Authorization": f"Bearer {mock_clerk_jwt}"}
    response = test_client.get("/api/v1/town/quota", headers=headers)
    body = response.json()
    assert body["agents"]["used"] == 3


def test_quota_reflects_sprite_jobs_in_window(test_client, mock_clerk_jwt, dynamodb_tables):
    from core.services.user_service import UserService

    user_id = "user_for_clerk_jwt"
    svc = UserService()
    svc.get_or_create(user_id)
    for _ in range(4):
        svc.consume_sprite_quota(user_id)

    headers = {"Authorization": f"Bearer {mock_clerk_jwt}"}
    response = test_client.get("/api/v1/town/quota", headers=headers)
    body = response.json()
    assert body["sprite_gen"]["used_24h"] == 4
```

(Adjust `user_for_clerk_jwt` to whatever userId the existing `mock_clerk_jwt` fixture resolves to — read `tests/conftest.py`.)

- [ ] **Step 2: Run, confirm fails**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/backend
uv run pytest tests/unit/test_quota_endpoint.py -v 2>&1 | tail -10
```

Expected: 404s.

- [ ] **Step 3: Add the endpoint**

In `apps/backend/routers/town.py`, near the other `/town/*` endpoints, add:

```python
@router.get("/quota")
async def get_quota(
    auth: AuthContext = Depends(get_current_user),
):
    """Per-user quota state for UI display.

    - agents.used: count of active agents for this user
    - agents.limit: AGENT_QUOTA constant
    - sprite_gen.used_24h: count of sprite jobs in the rolling 24h window
    - sprite_gen.limit: SPRITE_QUOTA_DAILY constant
    """
    import time

    from core.services.town_service import TownService
    from core.services.user_service import (
        AGENT_QUOTA,
        SPRITE_QUOTA_DAILY,
        SPRITE_QUOTA_WINDOW_MS,
        UserService,
    )

    user = UserService().get_or_create(auth.user_id)
    now_ms = int(time.time() * 1000)
    cutoff = now_ms - SPRITE_QUOTA_WINDOW_MS
    recent_sprites = [
        j for j in (user.get("sprite_jobs_24h") or [])
        if int(j.get("timestamp_ms", 0)) >= cutoff
    ]

    active_agents = TownService().get_active_agents_for_user(auth.user_id)

    return {
        "agents": {
            "used": len(active_agents),
            "limit": AGENT_QUOTA,
        },
        "sprite_gen": {
            "used_24h": len(recent_sprites),
            "limit": SPRITE_QUOTA_DAILY,
        },
    }
```

(If `TownService.get_active_agents_for_user` doesn't exist by that name, use the existing helper — search for the pattern used by `register_agent`'s quota check from sub 3.)

- [ ] **Step 4: Run tests, verify pass**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/backend
uv run pytest tests/unit/test_quota_endpoint.py -v
uv run pytest tests/ 2>&1 | tail -3
```

Expected: 4/4 PASS, full suite at baseline + 4.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/routers/town.py apps/backend/tests/unit/test_quota_endpoint.py
git commit -m "feat(backend): GET /town/quota exposes per-user quota state for UI

Returns {agents: {used, limit}, sprite_gen: {used_24h, limit}}. Reads
the user's active agent count + sprite_jobs_24h sliding window from
the users table (sub 3 work). Used by the upcoming QuotaIndicator
component on the agents sidebar.

4 unit tests cover: happy path, auth required, agent count reflects
seeded agents, sprite count reflects consumed quota.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: useQuota frontend hook

**Files:**
- Create: `apps/frontend/src/hooks/useQuota.ts`

- [ ] **Step 1: Write the hook**

```typescript
// apps/frontend/src/hooks/useQuota.ts
import { useEffect, useState } from "react";
import { useAuth } from "@clerk/clerk-react";

export interface QuotaData {
  agents: { used: number; limit: number };
  sprite_gen: { used_24h: number; limit: number };
}

const POLL_INTERVAL_MS = 30_000; // 30 seconds — quota changes rarely

export function useQuota(): QuotaData | null {
  const { getToken, isSignedIn } = useAuth();
  const [data, setData] = useState<QuotaData | null>(null);

  useEffect(() => {
    if (!isSignedIn) return;
    let cancelled = false;

    async function fetchQuota() {
      try {
        const jwt = await getToken();
        const response = await fetch(
          `${import.meta.env.VITE_BACKEND_URL}/town/quota`,
          { headers: { Authorization: `Bearer ${jwt}` } },
        );
        if (!response.ok) return;
        const body = (await response.json()) as QuotaData;
        if (!cancelled) setData(body);
      } catch {
        // Silent — quota is informational, not blocking.
      }
    }

    fetchQuota();
    const id = setInterval(fetchQuota, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [getToken, isSignedIn]);

  return data;
}
```

- [ ] **Step 2: Smoke-test compile**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/frontend
pnpm typecheck 2>&1 | tail -3
```

- [ ] **Step 3: Commit**

```bash
git add apps/frontend/src/hooks/useQuota.ts
git commit -m "feat(frontend): useQuota hook polls /town/quota every 30s

Used by QuotaIndicator on the agents sidebar. Silent on errors —
quota is informational, not blocking. 30s poll interval since quota
changes rarely.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: QuotaIndicator component (TDD)

**Files:**
- Create: `apps/frontend/src/components/QuotaIndicator.tsx`
- Create: `apps/frontend/src/components/QuotaIndicator.test.tsx`
- Modify: `apps/frontend/src/components/AgentsSidebar.tsx` — mount it

- [ ] **Step 1: Write failing test**

```typescript
// apps/frontend/src/components/QuotaIndicator.test.tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QuotaIndicator } from "./QuotaIndicator";

vi.mock("../hooks/useQuota", () => ({
  useQuota: () => ({
    agents: { used: 3, limit: 5 },
    sprite_gen: { used_24h: 4, limit: 10 },
  }),
}));

describe("QuotaIndicator", () => {
  it("shows agents and sprite-gen counts", () => {
    render(<QuotaIndicator />);
    expect(screen.getByText(/3.*\/.*5.*agents/i)).toBeInTheDocument();
    expect(screen.getByText(/4.*\/.*10.*sprite/i)).toBeInTheDocument();
  });

  it("highlights when at limit", () => {
    vi.doMock("../hooks/useQuota", () => ({
      useQuota: () => ({
        agents: { used: 5, limit: 5 },
        sprite_gen: { used_24h: 10, limit: 10 },
      }),
    }));
    // Re-import after mock change
    return import("./QuotaIndicator").then(({ QuotaIndicator: Q }) => {
      render(<Q />);
      const agents = screen.getByText(/5.*\/.*5.*agents/i);
      expect(agents.className).toMatch(/red|warning|destructive/);
    });
  });
});
```

- [ ] **Step 2: Run, confirm fails**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/frontend
pnpm test -- QuotaIndicator 2>&1 | tail -10
```

- [ ] **Step 3: Implement the component**

```typescript
// apps/frontend/src/components/QuotaIndicator.tsx
import { useQuota } from "../hooks/useQuota";

export function QuotaIndicator() {
  const data = useQuota();
  if (!data) return null;

  const agentsAtLimit = data.agents.used >= data.agents.limit;
  const spriteAtLimit = data.sprite_gen.used_24h >= data.sprite_gen.limit;

  return (
    <div className="mt-2 space-y-1 px-3 py-2 text-xs text-muted-foreground border-t">
      <div className={agentsAtLimit ? "text-red-600 font-medium" : ""}>
        {data.agents.used} / {data.agents.limit} agents
      </div>
      <div className={spriteAtLimit ? "text-red-600 font-medium" : ""}>
        {data.sprite_gen.used_24h} / {data.sprite_gen.limit} sprite-gens (24h)
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Mount in AgentsSidebar**

In `apps/frontend/src/components/AgentsSidebar.tsx`, find the appropriate spot (footer / below the agent list / inside the sidebar wrapper) and add:

```typescript
import { QuotaIndicator } from "./QuotaIndicator";

// Near the bottom of the sidebar JSX:
<QuotaIndicator />
```

- [ ] **Step 5: Run tests + typecheck**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/frontend
pnpm test -- QuotaIndicator 2>&1 | tail -5
pnpm typecheck 2>&1 | tail -3
```

- [ ] **Step 6: Commit**

```bash
git add apps/frontend/
git commit -m "feat(frontend): QuotaIndicator on agents sidebar

Shows '3/5 agents' and '4/10 sprite-gens (24h)'. Red highlight when at
limit so the user sees why a register attempt would 429.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: SpriteWaitOverlay component (TDD)

**Files:**
- Create: `apps/frontend/src/components/SpriteWaitOverlay.tsx`
- Create: `apps/frontend/src/components/SpriteWaitOverlay.test.tsx`
- Modify: `apps/frontend/src/components/ApartmentCard.tsx` — mount it

- [ ] **Step 1: Write failing test**

```typescript
// apps/frontend/src/components/SpriteWaitOverlay.test.tsx
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, act } from "@testing-library/react";
import { SpriteWaitOverlay } from "./SpriteWaitOverlay";

describe("SpriteWaitOverlay", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows 'Drawing your agent' with elapsed seconds", () => {
    const createdAt = Date.now() - 15_000; // 15s ago
    render(<SpriteWaitOverlay createdAt={createdAt} />);
    expect(screen.getByText(/drawing/i)).toBeInTheDocument();
    expect(screen.getByText(/15s/i)).toBeInTheDocument();
  });

  it("ticks elapsed seconds every second", () => {
    const createdAt = Date.now();
    render(<SpriteWaitOverlay createdAt={createdAt} />);
    expect(screen.getByText(/0s/i)).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(3_000);
    });
    expect(screen.getByText(/3s/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run, confirm fails**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/frontend
pnpm test -- SpriteWaitOverlay 2>&1 | tail -10
```

- [ ] **Step 3: Implement**

```typescript
// apps/frontend/src/components/SpriteWaitOverlay.tsx
import { useEffect, useState } from "react";

interface Props {
  createdAt: number; // ms timestamp
}

export function SpriteWaitOverlay({ createdAt }: Props) {
  const [elapsed, setElapsed] = useState(() =>
    Math.max(0, Math.floor((Date.now() - createdAt) / 1000)),
  );

  useEffect(() => {
    const id = setInterval(() => {
      setElapsed(Math.max(0, Math.floor((Date.now() - createdAt) / 1000)));
    }, 1_000);
    return () => clearInterval(id);
  }, [createdAt]);

  return (
    <div className="absolute inset-0 flex items-center justify-center bg-black/50 rounded text-sm text-white">
      <div className="text-center">
        <div className="animate-pulse">Drawing your agent…</div>
        <div className="text-xs opacity-70 mt-1">{elapsed}s</div>
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Mount in ApartmentCard**

In `apps/frontend/src/components/ApartmentCard.tsx`, find where the sprite renders. Add:

```typescript
import { SpriteWaitOverlay } from "./SpriteWaitOverlay";

// Inside the card JSX, alongside the sprite:
{!agent.spriteReady && agent.createdAt && (
  <SpriteWaitOverlay createdAt={agent.createdAt} />
)}
```

(The sprite container should be `position: relative` so the absolute overlay positions correctly. Verify or add the relative class.)

- [ ] **Step 5: Run tests + typecheck**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/frontend
pnpm test -- SpriteWaitOverlay 2>&1 | tail -5
pnpm typecheck 2>&1 | tail -3
```

- [ ] **Step 6: Commit**

```bash
git add apps/frontend/
git commit -m "feat(frontend): SpriteWaitOverlay shows 'Drawing your agent (Xs)'

Mounted on ApartmentCard while spriteReady=false. Elapsed seconds tick
every 1s. Eliminates the silent 2-minute pause that today shows a
blank placeholder while PixelLab does its thing.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: LandingSignupForm component (TDD)

**Files:**
- Create: `apps/frontend/src/components/LandingSignupForm.tsx`
- Create: `apps/frontend/src/components/LandingSignupForm.test.tsx`

- [ ] **Step 1: Write failing tests**

```typescript
// apps/frontend/src/components/LandingSignupForm.test.tsx
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { LandingSignupForm } from "./LandingSignupForm";

describe("LandingSignupForm", () => {
  beforeEach(() => {
    vi.spyOn(global, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ status: "ok" }), { status: 200 }),
    );
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders an email input and submit button", () => {
    render(<LandingSignupForm />);
    expect(screen.getByPlaceholderText(/email/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /(notify|sign up|join)/i })).toBeInTheDocument();
  });

  it("submits to /landing/signup on click", async () => {
    render(<LandingSignupForm />);
    fireEvent.change(screen.getByPlaceholderText(/email/i), {
      target: { value: "user@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: /(notify|sign up|join)/i }));

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/landing/signup"),
        expect.objectContaining({
          method: "POST",
          body: expect.stringContaining("user@example.com"),
        }),
      );
    });
  });

  it("shows success message on 200", async () => {
    render(<LandingSignupForm />);
    fireEvent.change(screen.getByPlaceholderText(/email/i), {
      target: { value: "user@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: /(notify|sign up|join)/i }));

    expect(await screen.findByText(/(thanks|got it|added)/i)).toBeInTheDocument();
  });

  it("shows error message on non-200", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "Invalid email" }), { status: 400 }),
    );
    render(<LandingSignupForm />);
    fireEvent.change(screen.getByPlaceholderText(/email/i), {
      target: { value: "bad" },
    });
    fireEvent.click(screen.getByRole("button", { name: /(notify|sign up|join)/i }));

    expect(await screen.findByText(/(error|invalid|try again)/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run, confirm fails**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/frontend
pnpm test -- LandingSignupForm 2>&1 | tail -10
```

- [ ] **Step 3: Implement**

```typescript
// apps/frontend/src/components/LandingSignupForm.tsx
import { useState } from "react";

type Status = "idle" | "submitting" | "success" | "error";

export function LandingSignupForm() {
  const [email, setEmail] = useState("");
  const [honeypot, setHoneypot] = useState("");
  const [status, setStatus] = useState<Status>("idle");
  const [errorMsg, setErrorMsg] = useState("");

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setStatus("submitting");
    setErrorMsg("");

    try {
      const response = await fetch(
        `${import.meta.env.VITE_BACKEND_URL}/landing/signup`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email, honeypot: honeypot || undefined }),
        },
      );

      if (response.ok) {
        setStatus("success");
        setEmail("");
      } else {
        const body = await response.json().catch(() => ({}));
        setStatus("error");
        setErrorMsg(body.detail || "Could not sign up — please try again");
      }
    } catch {
      setStatus("error");
      setErrorMsg("Network error — please try again");
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-2 max-w-sm">
      <input
        type="email"
        required
        placeholder="email@example.com"
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        disabled={status === "submitting"}
        className="rounded border px-3 py-2"
      />
      {/* Honeypot: hidden from users, only filled by bots */}
      <input
        type="text"
        name="website"
        value={honeypot}
        onChange={(e) => setHoneypot(e.target.value)}
        tabIndex={-1}
        aria-hidden="true"
        autoComplete="off"
        style={{ position: "absolute", left: "-9999px" }}
      />
      <button
        type="submit"
        disabled={status === "submitting"}
        className="rounded bg-black px-4 py-2 text-white disabled:opacity-50"
      >
        {status === "submitting" ? "Submitting…" : "Notify me at launch"}
      </button>
      {status === "success" && (
        <p className="text-sm text-green-600">
          Thanks — we got it. You'll hear from us at launch.
        </p>
      )}
      {status === "error" && (
        <p className="text-sm text-red-600">{errorMsg}</p>
      )}
    </form>
  );
}
```

- [ ] **Step 4: Run tests + typecheck**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/frontend
pnpm test -- LandingSignupForm 2>&1 | tail -5
pnpm typecheck 2>&1 | tail -3
```

Expected: 4/4 PASS, typecheck clean.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/LandingSignupForm.tsx apps/frontend/src/components/LandingSignupForm.test.tsx
git commit -m "feat(frontend): LandingSignupForm — email capture for launch list

POSTs to /landing/signup with a honeypot field hidden via off-screen
positioning. Inline success/error states; resets email on success.

4 vitest cases cover render, submit, success, error.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Landing page + route move

**Files:**
- Create: `apps/frontend/src/pages/Landing.tsx`
- Create: `apps/frontend/src/pages/Landing.test.tsx`
- Modify: `apps/frontend/src/scenes.ts` — town route changes from `/` to `/town`
- Modify: `apps/frontend/src/App.tsx` — `/` renders `<Landing/>` (signed-out) or redirects to `/town` (signed-in)

- [ ] **Step 1: Write failing tests for Landing**

```typescript
// apps/frontend/src/pages/Landing.test.tsx
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { Landing } from "./Landing";

describe("Landing", () => {
  it("renders hero copy and the signup form", () => {
    render(
      <MemoryRouter>
        <Landing />
      </MemoryRouter>,
    );
    // Hero / tagline
    expect(screen.getByText(/goosetown/i)).toBeInTheDocument();
    // Email input
    expect(screen.getByPlaceholderText(/email/i)).toBeInTheDocument();
    // Town link
    expect(screen.getByRole("link", { name: /(see|live|town)/i })).toHaveAttribute("href", "/town");
  });
});
```

- [ ] **Step 2: Run, confirm fails**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/frontend
pnpm test -- Landing 2>&1 | tail -10
```

- [ ] **Step 3: Implement Landing**

```typescript
// apps/frontend/src/pages/Landing.tsx
import { Link } from "react-router-dom";
import { LandingSignupForm } from "../components/LandingSignupForm";

export function Landing() {
  return (
    <main className="min-h-screen flex flex-col items-center justify-center px-6 py-12 max-w-2xl mx-auto text-center">
      <h1 className="text-4xl md:text-6xl font-bold mb-4">GooseTown</h1>
      <p className="text-lg md:text-xl text-muted-foreground mb-8">
        A persistent virtual town where AI agents live, walk around, chat with each
        other, and live a daily routine. Bring your own agent.
      </p>

      <div className="mb-6">
        <LandingSignupForm />
      </div>

      <Link
        to="/town"
        className="text-sm underline hover:no-underline"
      >
        See the town live →
      </Link>
    </main>
  );
}
```

- [ ] **Step 4: Update scenes.ts — move town from `/` to `/town`**

In `apps/frontend/src/scenes.ts`, find the existing `town` scene entry:

```typescript
{ id: 'town', displayName: 'Town', route: '/', requireAuth: false, showInNav: 'always' },
```

Change to:

```typescript
{ id: 'town', displayName: 'Town', route: '/town', requireAuth: false, showInNav: 'always' },
```

- [ ] **Step 5: Update App.tsx — route `/` to Landing or redirect**

In `apps/frontend/src/App.tsx`, find the existing `<Routes>` block. Add a route for `/` BEFORE the SCENE-driven routes:

```typescript
import { Navigate } from "react-router-dom";
import { Landing } from "./pages/Landing";

// Inside <Routes>:
<Route
  path="/"
  element={isSignedIn ? <Navigate to="/town" replace /> : <Landing />}
/>
```

The existing scene-driven routes still cover `/town`, `/library`, `/apartment` etc. via the SCENES array.

- [ ] **Step 6: Update sceneForPath to default to `/town` for signed-in `/` visits**

In `scenes.ts`, find `sceneForPath` and ensure it doesn't try to match `/` against the town scene's now-`/town` route. If it had a fallback that mapped `/` → 'town', remove it; the route-level redirect handles it now.

- [ ] **Step 7: Run all frontend tests + typecheck**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/frontend
pnpm test 2>&1 | tail -5
pnpm typecheck 2>&1 | tail -3
```

Expected: all green. Existing scene tests should still pass after the town-route change.

- [ ] **Step 8: Commit**

```bash
git add apps/frontend/
git commit -m "feat(frontend): Landing page at / + move town to /town

Anonymous visitors land on a hero + email-signup form + 'See the town
live' link. Signed-in visitors are redirected from / to /town.

Existing anonymous viewer (PR #243) at the town view still works since
the only change is the route — the canvas + viewer protocol are
unchanged.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: Surface RevokeTokenButton + copy-token button

**Files:**
- Modify: `apps/frontend/src/components/JoinTownModal.tsx`

- [ ] **Step 1: Read current JoinTownModal**

```bash
cat /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/frontend/src/components/JoinTownModal.tsx | head -50
```

Find the existing `<details>` block hiding the RevokeTokenButton, and find the token-display section.

- [ ] **Step 2: Move RevokeTokenButton out of `<details>`**

Replace the `<details>` block with a flat section. The button stays where the previous open-details content was, just no longer collapsed.

```typescript
{/* Replace the previous <details><summary>Advanced</summary>... pattern with: */}
<div className="mt-4 border-t pt-3">
  <RevokeTokenButton getToken={getToken} />
</div>
```

- [ ] **Step 3: Add a copy-token button next to the displayed token/instruction string**

```typescript
import { useState } from "react";

// Inside the component, near where the token / instruction string is displayed:
const [copied, setCopied] = useState(false);

async function copyInstructionString() {
  try {
    await navigator.clipboard.writeText(instructionString);
    setCopied(true);
    setTimeout(() => setCopied(false), 2_000);
  } catch {
    // Browser blocked clipboard access — silent.
  }
}

// In the JSX, alongside the token/instruction string:
<button
  type="button"
  onClick={copyInstructionString}
  className="text-xs underline ml-2"
>
  {copied ? "Copied!" : "Copy"}
</button>
```

(Bind `instructionString` to whatever variable holds the assembled paste-string in the existing component.)

- [ ] **Step 4: Smoke-test (no new test required — UX polish)**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/frontend
pnpm typecheck 2>&1 | tail -3
pnpm test 2>&1 | tail -3
```

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/JoinTownModal.tsx
git commit -m "feat(frontend): surface RevokeTokenButton + copy-token button on JoinTownModal

RevokeTokenButton was buried under an 'Advanced' details pane; now it's
a top-level action so the dogfood actually exercises it.

Copy button next to the displayed instruction string; 'Copied!' feedback
for 2s after click.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 11: Open PR + codex review

- [ ] **Step 1: Run all suites**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/backend
uv run pytest tests/ 2>&1 | tail -3
uv run ruff check . 2>&1 | tail -3
uv run ruff format --check . 2>&1 | tail -3

cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/infra
npm test 2>&1 | tail -3

cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/frontend
pnpm test 2>&1 | tail -3
pnpm typecheck 2>&1 | tail -3
```

- [ ] **Step 2: Push + open PR**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync
git push -u origin spec/onboarding-landing
gh pr create --title "feat(backend,frontend,infra): onboarding & landing — Resend signups, /town route move, quota UI" --body "$(cat <<'EOF'
## Summary

Sub-project 5 of 5 in the public-launch path.

- **Landing page at `/`** — hero + email-signup form + "See the town live →" link to `/town`. Signed-in visitors redirect to `/town`.
- **Town moves to `/town`** — anonymous viewer (PR #243) keeps working at the new route.
- **POST /landing/signup** proxies to Resend Audiences API. Honeypot field for bots, idempotent on duplicate email.
- **GET /town/quota** returns per-user agent + sprite-gen quota state.
- **QuotaIndicator** on agents sidebar shows `3/5 agents`, `4/10 sprite-gens (24h)` with red highlight at limit.
- **SpriteWaitOverlay** on ApartmentCard shows "Drawing your agent (Xs)…" while sprite is generating.
- **RevokeTokenButton** surfaced out of the "Advanced" details pane.
- **Copy-token button** on JoinTownModal.

## Operator step (required post-merge)

1. Create a Resend account
2. Create two audiences (one for dev, one for prod); copy each audience ID
3. Update the `RESEND_AUDIENCE_ID` strings in `apps/infra/lib/stacks/compute-stack.ts` and redeploy
4. Populate the secret:
   ```
   aws secretsmanager put-secret-value --secret-id goosetown-dev-resend-api-key --secret-string '<resend api key>'
   aws secretsmanager put-secret-value --secret-id goosetown-prod-resend-api-key --secret-string '<resend api key>'
   ```

Until those steps complete, `/landing/signup` returns 503 with a server-side log explaining the misconfiguration.

## Test plan

- [x] Backend: 4 ResendClient + 6 landing-endpoint + 4 quota-endpoint = 14 new pytest cases
- [x] Frontend: 2 QuotaIndicator + 2 SpriteWaitOverlay + 4 LandingSignupForm + 1 Landing = 9 new vitest cases
- [x] Existing backend, infra, frontend test suites stay green
- [ ] Post-deploy dev: actual signup hits Resend dev audience
- [ ] Post-deploy dev: signed-out visit at / shows landing
- [ ] Post-deploy dev: signed-in visit at / redirects to /town
- [ ] Post-deploy dev: agent register shows sprite-wait overlay during PixelLab gen

## Sequence context

1. ✅ Skill packaging & distribution (sub 1, PR #251)
2. ✅ Production hardening (sub 2, PR #252)
3. ✅ Multi-tenant app-level (sub 3, PR #253)
4. **Personal launch dogfood (sub 4) — gated on this PR merging + Resend secret populated**
5. **This PR — Onboarding & landing (sub 5)**

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Tag codex**

```bash
gh pr comment <PR#> --body "@codex please review this PR.

Sub 5 of public-launch path. Adds landing page (Resend-backed), route move (/ -> /town), quota UI, sprite-wait UX, surfacing revocation.

Particular things worth a careful look:
- ResendClient idempotency on duplicate email: 422 with 'already exists' is treated as success. Is the substring match brittle?
- /landing/signup honeypot: returns 200 silently on bot detection. Right call, or should we 200-with-marker for monitoring?
- Route move: signed-in users redirect / -> /town. Anyone with bookmarks at / will see landing once before re-bookmarking. Acceptable?
- /town/quota polling at 30s: too eager? not eager enough?
- SpriteWaitOverlay ticks createdAt-based elapsed. Lag between createdAt and actual PixelLab job kickoff is small but real."
```

- [ ] **Step 4: Wait for codex review** (per CLAUDE.md memory)

---

## Self-Review Notes

**Spec coverage:**
- ✅ Landing page at `/` with email form: Tasks 8, 9
- ✅ Town moves to `/town`: Task 9
- ✅ Signed-in redirect from `/` to `/town`: Task 9 step 5
- ✅ POST /landing/signup with Resend: Tasks 1-3
- ✅ GET /town/quota: Task 4
- ✅ QuotaIndicator on agents sidebar: Tasks 5-6
- ✅ SpriteWaitOverlay on ApartmentCard: Task 7
- ✅ Surface RevokeTokenButton: Task 10
- ✅ Copy-token button: Task 10
- ✅ Resend secrets in CDK: Task 2
- ✅ Resend Audience IDs documented as operator step: Task 11 PR body

**Type/path consistency:**
- `RESEND_API_KEY` / `RESEND_AUDIENCE_ID` consistent across CDK secret, env var, backend Settings, Resend Audiences API URL
- `useQuota` hook returns `QuotaData` with `agents.{used,limit}` + `sprite_gen.{used_24h,limit}` — same shape on backend response
- `QuotaIndicator` consumes `useQuota`, no prop drilling
- `SpriteWaitOverlay` takes `createdAt: number`; ApartmentCard passes `agent.createdAt`

**No placeholders.** Operator-step content (audience IDs unset until you create the Resend account) is explicitly called out as such, not glossed.
