# Sprite Pipeline Reliability — Design (Approach A)

**Date:** 2026-06-09
**Status:** approved approach (A), pending spec review
**Origin:** dev test pass surfaced task #81 — fresh-agent sprite gen came out `{walk:4, idle:0, talking:1}` under concurrency. Root cause is evidence-based (not guessed).

## Problem

Sprite **animation** generation is unreliable under concurrency. A fresh agent's signup animations dropped `idle` entirely and most of `talking` because of a burst of `httpcore.connect_tcp` timeouts to `api.pixellab.ai`. Ruled out with metrics: NAT `ErrorPortAllocation = 0` (max 30 active conns), backend CPU ~12% max / 33% avg (not event-loop starvation). So it is **not our infra capacity** — it is a **connection burst** plus **uncapped job concurrency**.

Two structural causes:

1. **No connection pooling.** `pixellab_service._request` (and `_get_rotation_base64`, `fetch_all_rotations`) open a **new `httpx.AsyncClient` per call *and per retry*** (3 sites). Concurrent generation opens many simultaneous sockets + TLS handshakes.
2. **Uncapped job concurrency across paths.** `SpriteQueue(max_concurrent=1)` gates **signup** pipelines only. **Activity** custom-animations (`_generate_custom_animation`) and the **admin regenerate** endpoint are spawned via bare `asyncio.create_task` with **no gate** — so they run *concurrently* with a signup pipeline (exactly what happened: the test's `perform` activity burst alongside test-scout's signup gen).

A third, self-inflicted contributor: PR #368's review-fix set `_get_rotation_base64 → get_character(retries=1)` (fail-fast). That call is on the **critical** custom-animation path (it fetches the reference frame), not a graceful fallback, so a single connect blip skips a direction.

## What the PixelLab v2 API actually provides (grounds the design)

Verified against the live OpenAPI spec (`https://api.pixellab.ai/v2/openapi.json`) and an empirical test (below).

- **No official SDK worth adopting.** The `pixellab` PyPI package is v1-only, synchronous `requests`, **no pooling, no retries**, and doesn't cover the v2 character/animation/background-job endpoints. Our own httpx code is ahead; the fix belongs in our code.
- **Limit is concurrency-based, not rate-based.** `429 = "too many concurrent jobs"` per account; no published number (the "~10 Tier 2" in our code is folklore), **no `Retry-After`**. Right primitive: a **tunable concurrency semaphore** + our own backoff.
- **`402 = insufficient credits/generations`** is a *distinct* code — not retryable, should fire a credit-exhaustion alarm (closes a known launch blocker).
- **No webhooks.** Polling `GET /background-jobs/{id}` at 5–10s is the only option; **no bulk-status endpoint** (N jobs = N polls, fire concurrently per cycle). Our 10s interval is fine.
- **🎯 `POST /characters/animations` batches all directions in ONE call.** Takes a `directions` array (omit = all), `mode ∈ {template, v3, pro}`, returns **one `background_job_id` per direction**. It is the **same endpoint we already use for walk/idle** (template mode). For custom animations, `mode:"v3"` + `action_description` uses the **character's own rotations as the per-direction reference internally** — so we no longer fetch/pass `first_frame` ourselves.

### Empirical verification (2026-06-09)

Submitted `POST /characters/animations {character_id: <our robot char>, mode:"v3", action_description:"waving both arms overhead in a cheerful greeting", frame_count:8, directions:["south","west","east","north"]}` — **no reference frame**.

- Returned `status:processing` + **4 `background_job_ids`** (one per direction). ✅
- South job completed: **9 frames, `images[].type = rgba_bytes` (136×136)**, 84% transparent. ✅
- Rendered frames: **character fully preserved** (the exact robot — white body, cyan visor, orange antenna), **correct action**, **transparent background, no gray drift**. ✅

Conclusion: dropping the manual reference frame is **safe**; `mode:v3` is on-model.

## Design — Approach A (targeted architectural fix)

### 1. Pooled, long-lived httpx client

A module-scoped `httpx.AsyncClient` lazily created per running loop, with explicit caps:

```python
httpx.Limits(max_connections=<env, default 10>, max_keepalive_connections=<env, default 5>)
```

Replaces the 3 per-call `httpx.AsyncClient()` sites (`:209`, `:568`, `:685`). All PixelLab API calls and CDN frame/rotation downloads reuse it (keep-alive). Closed on FastAPI shutdown; in worldgm it lives for the process. This caps sockets regardless of how many coroutines call concurrently — directly removing the connect burst.

### 2. ONE unified animation helper

Replace `animate_character` (template) **and** `queue_custom_animation` (custom, per-direction loop) **and** `_get_rotation_base64` with a single helper:

```python
async def queue_animation(self, character_id: str, anim_name: str) -> list[str]:
    """Queue ALL directions of one animation in a single PixelLab call.
    Returns background_job_ids (one per direction). Handles template + custom
    from ANIMATION_REGISTRY. No manual reference-frame fetch (PixelLab uses the
    character's rotations)."""
```

Behavior:
- Look up `ANIMATION_REGISTRY[anim_name]`.
- **template** → `POST /characters/animations {character_id, mode:"template", template_animation_id, directions: CARDINAL_4}`.
- **custom** → `POST /characters/animations {character_id, mode:"v3", action_description: <desc>, frame_count, directions: CARDINAL_4, enhance_prompt: true}`.
- One HTTP call → `background_job_ids` (4). Submission goes through the concurrency gate (§4).

`CARDINAL_4 = ["south","west","east","north"]` (our spritesheet uses 4 dirs even though chars may carry 8 rotations — omitting `directions` would over-generate).

This collapses **8 connection-opens per custom animation (4 v3 + 4 rotation GETs) → 1 submit + polls** and deletes the fragile `retries=1` rotation path.

### 3. Frame extraction

The new jobs' `last_response.images` is `[{type:"rgba_bytes", width, height, base64}]` (confirmed). `get_v2_animation_frames` already decodes this shape — keep it, ensure it handles `rgba_bytes` via `Image.frombytes("RGBA",(w,h),raw)` (it does). Compositing (`composite_v2_animation` / spritesheet) is unchanged. Job order maps to the `directions` array order returned by the submit response (use the response's `directions`, don't assume).

### 4. Unified job-submission concurrency gate

A **single process-global `asyncio.Semaphore`**, env-tunable, that **every** generation path acquires around submit+wait:

- `PIXELLAB_MAX_CONCURRENT_ANIMATIONS` (default **2**) — each animation = up to 4 jobs, so 2 ≈ ≤8 concurrent jobs, conservatively under the undocumented cap.
- Acquired by: signup pipeline (`queue_signup_animations`), activity lazy-gen (`_generate_custom_animation`), admin regenerate. This is the fix for "activity bypasses the gate."
- The existing `SpriteQueue(max_concurrent=1)` for whole **signup pipelines** stays (serializes full registrations); the new semaphore bounds **animation-job** concurrency *globally across all paths*.

### 5. Distinct error classification

In `_request`, classify by status (not the current 4xx-blanket):
- `429` → retryable, exponential backoff + jitter (concurrency).
- `5xx`, connect/read timeouts, transport errors → retryable backoff.
- **`402` → raise `PixelLabCreditError` (no retry); log at ERROR with an alarm-worthy marker** (ties to the missing credit-exhaustion alarm launch blocker).
- `423` (zip not ready) → caller-handled (return, retry by caller).
- `404` / other `4xx` → hard fail, no retry.
- **Polling edge case:** a job can **vanish (404) after completion** (PixelLab auto-cleans finished jobs — observed live: the `north` direction 404'd mid-poll). `wait_for_jobs` must treat a 404 on `get_job_status` as **terminal "gone"** (count it done/skip), not loop until timeout and not abort the batch. We must therefore **fetch each job's frames as soon as it reports `completed`** (don't defer all frame-fetches to after the whole batch finishes, or a fast-completing direction may be cleaned up before we read it).

### 6. Activity lazy-gen covers template + custom

`_generate_custom_animation` currently early-returns when the animation isn't in `CUSTOM_ANIMATIONS` (`activity_service.py:217`) — so `order_coffee → drink` (a **template**) never generates. Switch it to call `queue_animation(char_id, anim_name)` for **any** registry animation, removing the `CUSTOM_ANIMATIONS`-only gate. (Rename the function to `_generate_activity_animation`.)

### 7. Animation registry review ("descriptions are good")

Audit `ANIMATION_REGISTRY`:
- **walk / idle** are templates (`template_animation_id` `walk` / `breathing-idle`) — verify the template IDs are valid current PixelLab templates; templates need no prose description.
- **talking** and the custom prompts (sitting, reading, writing, working, performing, exercising, gaming, gardening, cooking, sleeping) → review each `action_description` for clarity and motion specificity; keep the `_LOOP_SUFFIX` (final frame returns to first). With `mode:v3 enhance_prompt:true`, descriptions can be tightened to the core action; verify each reads as a clean PixelLab `action_description`.
- Confirm **every** `activity_registry.py` `animation=` value resolves to a registry key (audited: all present — sitting, performing, idle, reading, writing, working, drink, exercising, gaming, gardening, sleeping, cooking).

## Files touched

| File | Change |
|---|---|
| `core/services/pixellab_service.py` | shared pooled client; `queue_animation` unified helper; delete `queue_custom_animation` + `_get_rotation_base64` + per-direction loop; fold `animate_character`; error classification + `PixelLabCreditError`; global semaphore |
| `core/services/activity_service.py` | call `queue_animation` for any registry animation; drop CUSTOM-only early return; acquire the gate |
| `core/services/sprite_pipeline.py` / `queue_signup_animations` | use `queue_animation`; acquire the gate |
| `routers/town.py` | admin regenerate acquires the gate; surface 402 credit alarm |
| `core/config.py` | `PIXELLAB_MAX_CONCURRENT_ANIMATIONS`, pool limit envs |
| `main.py` | close the pooled client on shutdown |
| `ANIMATION_REGISTRY` | reviewed descriptions |

## Testing

- **Unit:** `queue_animation` builds the correct request body per type (template vs v3) and returns N job_ids; semaphore bounds concurrent submissions; error classifier maps 429/402/423/404/5xx correctly; 402 raises `PixelLabCreditError` and never retries; pooled client is reused (not recreated per call).
- **Integration smoke (dev, gated):** generate one custom animation end-to-end via the new path on a throwaway character; assert 4 directions, transparent frames, on-model (the verification already done manually).
- **Concurrency:** simulate signup + activity firing together; assert total in-flight animation submissions ≤ semaphore bound and all directions land.
- Full backend suite + ruff.

## Rollout

- Land behind no feature flag (the swap is verified), but ship the semaphore default conservative (2) and pool limits modest (10/5); tune via env after a dev soak.
- Re-run the dev test-pass scenario (fresh agent + concurrent activity) and confirm `{walk:4, idle:4, talking:4}` with no `connect_tcp` drops.

## Non-goals (YAGNI)

- No generic job-queue/worker abstraction (Approach C) — the API is polling-only and §1–§4 remove the root cause.
- No webhook receiver — PixelLab has none.
- No adoption of the official `pixellab` SDK — it's worse than what we have.
