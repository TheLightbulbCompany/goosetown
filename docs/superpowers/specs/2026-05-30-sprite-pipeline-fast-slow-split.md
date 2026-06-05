# Sprite Pipeline Fast/Slow Split

**Status:** Draft — awaiting user approval before implementation plan

**Goal:** Decouple "agent appears in town with a usable sprite" (fast path, runs at signup, parallel-friendly) from "agent gets walk/idle/talking animations" (slow path, background, serialized). Removes the current `max_concurrent=1` throttle as a scaling ceiling without exceeding PixelLab Tier 2's 10-concurrent-job cap.

## Why

Today's pipeline does ~13 PixelLab background jobs per agent in one go (1 char-creation + 4 walk + 4 idle + 4 talking). With Tier 2's 10-job cap, this caps practical signup throughput at ~1 agent in flight at a time. At launch this is a hard ceiling: 50 signups/hr → ~5hr queue depth.

The two phases have **independently useful artifacts**:

- **Phase B output** (static rotation) is enough for the agent to walk around, get nearby/proximity events, and look like a real character. The game already uses static rotations as walk frames when no walk animation is uploaded (see `agent.gd:_parse_facing` + frame slicing — static spritesheet is 4 directions × 1 frame, walk is 4 directions × 6 frames, both render correctly).
- **Phase C output** (walk/idle/talking animations) is polish — better animation fidelity but not load-bearing for agent functionality.

So Phase B should block "agent is in town" but Phase C should not.

## Architecture

Two in-process queues, one consumer module each:

```
                          ┌─────────────────────┐
register / connect ─────► │ SpriteQueue (fast)  │ ──► Phase A+B  ──► spriteReady=true
                          │ max_concurrent=8    │     create+static    agent connected
                          └──────────┬──────────┘     ~1 PixelLab job/agent  to game server
                                     │
                                     └─ on Phase B success: enqueue Phase C ┐
                                                                            │
                          ┌─────────────────────┐                           │
                          │ AnimationQueue (slow)│◄──────────────────────┘
                          │ max_concurrent=1     │ ──► Phase C ──► walk/idle/talking
                          └─────────────────────┘     ~12 jobs/agent  spriteAnimations populated
                                                                       phase=alive
```

**Fast path (SpriteQueue):**
- `max_concurrent=8` — Phase B issues 1 PixelLab background job per agent (`create_character`), so 8 parallel agents = 8 concurrent jobs, safely under the 10-job cap.
- Output: static sprite uploaded to S3, `spriteReady=true`, `sprite_status='static_ready'`, agent connected to game server.
- Triggers Phase C on success.

**Slow path (AnimationQueue):**
- `max_concurrent=1` — Phase C issues up to 12 jobs per agent (4 directions × walk/idle/talking) with `animate-with-text-v3` for talking that does NOT support queuing. Serializing eliminates cross-agent collision on that endpoint.
- Output: `spriteAnimations` populated with walk/idle/talking URLs, `sprite_status='complete'`, `phase='alive'`.
- Throughput: ~5 min per agent. 100 signups/hr → 8.3 hr animation backlog, but agents are fully usable in the meantime.

## State machine

Current `sprite_status` lifecycle: `creating → building_static → static_ready → animating → downloading → complete`.

After split, this stays — but the SpriteQueue's `_sprite_pipeline_task` returns after writing `sprite_status='static_ready'`, and AnimationQueue's new `_animation_pipeline_task` picks up from there.

New "stuck-in-static" detection: an agent with `sprite_status='static_ready'` and no animations is a Phase C candidate. Auto-kick + resume paths cover this.

## Code changes

**Refactor** `apps/backend/core/services/sprite_pipeline.py`:
- Split `_sprite_pipeline_task` at line 178 into two functions:
  - `_sprite_pipeline_task` (renamed `_phase_b_pipeline_task` or kept as alias): Phase A + B only. After successful static upload + game-server connect, calls `get_animation_queue().enqueue(agent_id, char_id, display_name)` and returns.
  - `_phase_c_pipeline_task(agent_id, char_id, display_name)`: lines 178-336. Reads `pixellabCharacterId` from arg (passed by AnimationQueue) so it can run standalone after a restart.

**New** `apps/backend/core/services/animation_queue.py`:
- Mirror of `sprite_queue.py` structure: `AnimationQueue` class with `max_concurrent=1`, `enqueue(agent_id, char_id, display_name)`, internal `_process_job` calling `_phase_c_pipeline_task`.
- Same retry-with-backoff and `_active_jobs` dedup.
- Singleton `get_animation_queue()`.

**Modify** `apps/backend/core/services/sprite_queue.py`:
- Bump default `max_concurrent: int = 3` → `max_concurrent: int = 8` (the class default; tests pass their own values).
- Bump singleton `SpriteQueue(max_concurrent=1)` → `SpriteQueue(max_concurrent=8)`.
- Update comment to reflect Phase A+B-only scope.
- Update the regression test `test_singleton_throttles_to_one_concurrent_job` → `test_singleton_caps_at_eight`.

**Modify** `apps/backend/core/services/agent_connect_service.py`:
- Existing auto-kick (PR #339) triggers `get_sprite_queue().enqueue(...)` when `not spriteReady AND sprite_status is None AND no pixellabCharacterId`. Keep that.
- Add a second auto-kick: when `spriteReady AND sprite_status == 'static_ready' AND pixellabCharacterId` (Phase B done, Phase C never ran or failed), enqueue onto AnimationQueue.

**Modify** `apps/backend/main.py:_reconnect_active_agents` (resume path):
- Today recovers `sprite_status` in `{creating, animating, downloading}` via `resume_sprite_generation`.
- After split: agents with `sprite_status='static_ready'` and missing animations should be enqueued onto AnimationQueue (not via `resume_sprite_generation`).
- Agents with `sprite_status in {animating, downloading}` should also route to AnimationQueue's `_phase_c_pipeline_task` (which can pick up from either phase via existing branching in `resume_sprite_generation`'s body).

**Start both queues** in `main.py` startup: `get_sprite_queue().start()` and `get_animation_queue().start()`.

## Failure modes

- **Phase B succeeds, Phase C enqueue raises** (race condition, OOM, etc.): the auto-kick on next agent connect catches it. Worst case the agent stays on static sprite until their daemon reconnects.
- **Phase B succeeds, Phase C fails after N retries**: AnimationQueue logs and drops. Auto-kick on next connect re-enqueues. Static sprite remains usable.
- **Backend restart mid-Phase-C**: `_reconnect_active_agents` re-enqueues based on `sprite_status` (same pattern as today).
- **PixelLab quota exhaustion on Phase B**: `create_character` raises 429 → pipeline's existing exception handler at line 89-92 logs + clears status → SpriteQueue retries with backoff. After max retries, agent stays with `spriteReady=false` and the connect auto-kick re-tries on next reconnect. Same as today.

## Backward compat / migration

- No DDB schema changes. The `sprite_status='static_ready'` value already exists in the state machine.
- Resident soak: the 20 existing residents already have `spriteReady=true` and `sprite_status='complete'`. Won't be touched.
- Any agent currently mid-pipeline at deploy time: existing `_reconnect_active_agents` resume path will route them through the new queues correctly because status values are unchanged.

## Test plan

New unit tests:
- `test_animation_queue.py` — mirrors `test_sprite_queue.py`: enqueue/dedup/concurrency/retry/singleton-throttle (this one asserts `_semaphore._value == 1`).
- `test_phase_b_enqueues_phase_c` — patches `get_animation_queue()`, asserts `_sprite_pipeline_task` calls `.enqueue(...)` after successful Phase B.
- `test_handle_agent_connect_auto_kicks_animation_for_static_ready` — adds to `test_handle_agent_connect.py`: when DDB row has `spriteReady=true, sprite_status='static_ready', pixellabCharacterId='x'`, the connect path enqueues onto AnimationQueue.

Dev validation:
- Deploy, register a fresh test agent, watch DDB row transitions: `creating → building_static → static_ready` (~30s), `animating → downloading → complete` (~5 min). Confirm `spriteReady=true` flips at static_ready (not at complete).
- Open the game in browser, confirm agent appears and walks (using static frames) immediately after static_ready, then walk animation upgrades a few minutes later.
- Multi-agent stress: register 5 agents in 10s, confirm all 5 are walking in town within 30s, all 5 finish animations within ~25 min, zero 429s.

## Out of scope

- PixelLab Tier 3 upgrade (separate ops decision).
- Animation prioritization (online-user-first, etc.) — straightforward extension once the split is in.
- Pre-cached archetype sprites — separate fallback strategy.
- Multi-provider dual-stack (Stable Diffusion fallback) — future scale concern.

## Rollout

Single PR. No feature flag — the data model is unchanged and existing rows continue working unchanged. The risk is in the two queue interactions and the auto-kick paths; the test plan covers both. The fallback if it goes wrong is reverting the SpriteQueue concurrency to 1 and the auto-kick to single-path, which restores current behavior.
