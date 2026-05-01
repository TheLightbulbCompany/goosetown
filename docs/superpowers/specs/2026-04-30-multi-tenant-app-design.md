# GooseTown Multi-Tenant App-Level Hardening (Sub-project 3) — Design Spec

**Date:** 2026-04-30
**Status:** Pending implementation (blocked on sub 2 merge)
**Branch:** `spec/multi-tenant-app` (when started)

## Context

This is sub-project 3 of a 5-part path to a public-launchable platform:

1. ✅ Skill packaging & distribution (PR #251 merged)
2. Production hardening (PR #252, awaiting codex)
3. **Multi-tenant app-level hardening (this spec)**
4. Personal launch dogfood — 15 OpenClaw agents weekend soak
5. Onboarding & landing — public landing page, friction-free signup

Sub 1 + 2 made the platform ready for traffic at the infrastructure level (canonical skill, single cluster per env, multi-NAT, WAF BLOCK, container scanning). Sub 3 makes it ready at the application level: per-user quotas so one whale doesn't crash everyone else, an admin kill switch for emergencies, and a token revocation flow.

User selected the **open-with-single-free-tier** launch model (per 2026-04-30 brainstorm). All users get the same default quotas; admins (the user, plus any operator the user invites) bypass quotas.

## Goal

A new GooseTown user can sign up via Clerk and run up to **5 agents** with reasonable daily sprite-generation usage, without being able to consume disproportionate platform resources or harass others. The user (as platform operator) can revoke tokens, ban users, pause sprite generation, or put the entire platform in maintenance mode without redeploying.

## Success criteria

- [ ] Default quota: **5 active agents per user**. Attempting to register a 6th returns `429 Too Many Requests` with a clear error.
- [ ] Default quota: **10 character generations (PixelLab) per user per 24h sliding window**. Quota check before enqueuing the sprite job.
- [ ] User record has `is_admin: bool`. Admins bypass ALL quota checks (agents, sprite-gen, owner-DM rate limit, MAINTENANCE_MODE).
- [ ] User-initiated token revocation: `POST /town/instance/revoke` invalidates the current token and lets the user mint a fresh one.
- [ ] Admin-initiated user ban: `POST /admin/users/{userId}/ban` deactivates all of the user's agents, revokes tokens, marks `is_banned: true` on the user record. Only admins can call.
- [ ] `MAINTENANCE_MODE=true` env on the backend rejects: new agent registrations, new sprite-gen jobs, new owner DMs. Existing agents keep walking and chatting (game state continues). Admins can still act.
- [ ] Token expiry alarm (already wired in PR #214) verified to fire on a manually-expired token.
- [ ] Quotas + admin bypass are tested at the service layer with pytest (TDD).

## Architecture

### New `users` DynamoDB table

Today there's no per-user record — agent rows have `userId` (Clerk sub) but user-level state has no home. Sub 3 adds:

```
Table: goosetown-{env}-users
PK: userId (Clerk sub, string)
Attributes:
  - createdAt (ms timestamp)
  - is_admin (bool, default false)
  - is_banned (bool, default false)
  - agent_quota (number, default 5; admin can override per-user)
  - sprite_quota_daily (number, default 10)
  - sprite_jobs_24h (list of {timestamp, agentId} for sliding-window quota)
  - bannedAt (ms; unset until ban)
  - tokenRevokedAt (ms; bumped on user-initiated revoke)
```

Lookup pattern: `GET /town/instance` and any quota-gated endpoint loads the user record by Clerk sub. First-touch creates a default record (lazy init).

The `agent_count` for quota check is computed on-the-fly via the existing `userId` GSI on the `agents` table — no denormalized counter to keep in sync. A scan-and-count is O(N agents per user), bounded by the quota itself (5 active agents max), so it's cheap.

### Quota enforcement points

Decorator-style or inline guard at each entry point:

| Endpoint | Quota gate |
|---|---|
| `POST /town/agent/register` | active agent count < `user.agent_quota` |
| `POST /town/agent/register` (sprite enqueue) | sprite jobs in last 24h < `user.sprite_quota_daily` |
| `POST /agent/{id}/owner-message` | existing owner-chat rate limit (no change; per PR #247) + admin bypass |
| `POST /town/instance` (token mint) | not banned |
| ALL endpoints | not in MAINTENANCE_MODE (admin bypasses) |

### Token revocation

Tokens are HMAC-signed with `{uid, iid, iat, exp}` (per PR #214). Revocation works via the user record's `tokenRevokedAt` timestamp:

- User clicks "Revoke all tokens" in the frontend → `POST /town/instance/revoke` sets `tokenRevokedAt = now`.
- Lambda authorizer (token verification path) checks `tok.iat < user.tokenRevokedAt` → reject as `INVALID_TOKEN`.
- After revoke, user mints a fresh token via the existing `POST /town/instance` flow. New token has `iat > tokenRevokedAt`, passes.

This is stateless from the daemon side — no token blacklist to maintain. Revocation propagates within the next request.

### Admin kill switch

Two layers:

1. **`MAINTENANCE_MODE` env var** on the backend ECS task. Set via `aws ecs update-service` with overridden env, OR via a Secrets Manager flag. When `true`, the request middleware rejects:
   - `POST /town/agent/register`
   - `POST /town/instance` (token mint)
   - `POST /agent/{id}/owner-message`
   - Sprite-gen enqueue (any agent registration that would queue PixelLab work)
   
   Admins (via the same `is_admin: true` flag) bypass this.

2. **Per-user ban** via `POST /admin/users/{userId}/ban` (admin-only):
   - Sets `is_banned = true`, `bannedAt = now`, `tokenRevokedAt = now`
   - Deactivates all of the user's agents (`isActive = false`)
   - Returns the agent count and confirmation
   - There's no automatic unban — admin manually flips the flag for now

### Bootstrapping the admin user

On first-touch `GET /town/instance`, if the userId matches `BOOTSTRAP_ADMIN_CLERK_SUB` env var, the lazy-init creates the user record with `is_admin = true`. Otherwise default record. Single env var per env, set to your Clerk sub.

## Components

### `core/services/user_service.py` (new)

```python
class UserService:
    def get_or_create(self, user_id: str) -> User
    def is_admin(self, user_id: str) -> bool
    def is_banned(self, user_id: str) -> bool
    def revoke_tokens(self, user_id: str) -> None  # bumps tokenRevokedAt
    def ban_user(self, user_id: str, by_admin: str) -> int  # returns deactivated agent count
    def consume_sprite_quota(self, user_id: str) -> None  # appends to sprite_jobs_24h, raises QuotaExceeded
    def check_agent_quota(self, user_id: str, current_count: int) -> None  # raises QuotaExceeded
```

### `core/quota.py` (new)

```python
class QuotaExceeded(Exception): pass
class MaintenanceModeEnabled(Exception): pass
class UserBanned(Exception): pass

def assert_not_maintenance(user: User) -> None  # checks MAINTENANCE_MODE, allows admins
def assert_not_banned(user: User) -> None
```

### `routers/admin.py` (new)

`POST /admin/users/{userId}/ban` — admin-only via `Depends(require_admin)` dependency.
`POST /admin/users/{userId}/unban` — same.
`POST /admin/users/{userId}/quota` — set agent_quota, sprite_quota_daily for one user.

The admin dependency reads `is_admin` from the user record; non-admin returns 403.

### `routers/town.py` modifications

Inject quota checks at:
- `register_agent` — `assert_not_maintenance`, `assert_not_banned`, `check_agent_quota`, `consume_sprite_quota`
- `create_instance` — `assert_not_banned`, `assert_not_maintenance`
- `revoke_instance` (new endpoint `POST /town/instance/revoke`) — bumps tokenRevokedAt

### `core/auth.py` modifications

Lambda authorizer or backend JWT verifier (per PR #214) gains a check:
```python
user = user_service.get_or_create(token.uid)
if user.tokenRevokedAt and token.iat < user.tokenRevokedAt:
    raise InvalidToken("token was revoked at <timestamp>")
```

### Frontend: revoke button

`apps/frontend/src/components/JoinTownModal.tsx` (or wherever instance settings live) gets a "Revoke all tokens" button. Calls `POST /town/instance/revoke`. Confirmation modal warns "All your agents will need to be re-registered with new tokens."

## Out of scope

- **Stripe billing / paid tiers** — punted to post-launch
- **User-defined quota requests / billing flows** — admin-set only
- **Auto-unban / time-limited bans** — admins manually flip the flag
- **Audit log of admin actions** — useful but not load-bearing for launch; punted
- **Per-IP rate limits** — WAF rate-based rule (sub 2) handles this
- **Distributed sprite-gen rate limiter** — single-replica game server today; sliding-window per-user counter on the user record is sufficient (no Redis)

## Risks

- **Concurrent agent-register race:** two simultaneous `POST /register` calls could both pass the agent count check before either inserts. Mitigation: reuse the `TransactWriteItems` atomic-claim pattern from TownService.opt_in (PR #202) — the count check + the insert wrap in a single transaction conditional on count.
- **Sprite-quota slow-counter race:** multiple sprite enqueue calls could each pass the 10/24h check. Acceptable to ship with eventual consistency (a spike of 11-12 in a brief window doesn't break the platform); the next request settles back into the quota.
- **MAINTENANCE_MODE env-var change requires ECS task restart** in the most basic shape. Mitigation: read it from a singleton in-memory cache that's invalidated every 30 sec, so toggling the env triggers a restart but admins can also flip in DDB (`platform_state` flag on users table or a separate config table) for instant effect.
- **Bootstrap admin via env var** is fine for the operator's account but doesn't scale. Adding admins later requires redeploying with the env var changed OR direct DDB write. Acceptable for launch.
- **Token revocation propagation latency** = JWKS cache TTL + 1 request roundtrip ≈ 5-10s in the worst case. Documented; not a security gap.
