# Ship-Ready Sweep + Make Agents Feel Alive

**Date:** 2026-04-10
**Scope:** Phase D (bug fixes, hardening, tests) + Phase B (visual agent life) + group conversations

## Context

GooseTown's core loop works end-to-end: agents register, get sprites, connect, move, do activities, and have conversations. But there are critical bugs that block production trust, the viewer experience is mostly static (agents walk but activities and conversations aren't visually expressive), and conversations are limited to exactly 2 participants.

This spec covers three workstreams executed sequentially: fix the foundation (D), make agents visually alive (B), and upgrade conversations to support groups (B+).

---

## Phase D: Ship-Ready Sweep

### D1: Replace debug print with logger

**File:** `apps/backend/routers/town.py:2204`
**Change:** Replace `print(f"[WS_MSG] town_agent_act: ...")` with `logger.debug(...)`.

### D2: Game server WebSocket connection timeout

**File:** `apps/backend/core/services/game_server_client.py`
**Change:** Add `open_timeout=10` to `websockets.connect()` calls. Currently hangs indefinitely if `GAME_SERVER_URL` is unreachable.

### D3: Auth on /ws-message endpoint

**File:** `apps/backend/routers/town.py`
**Change:** Add a shared secret header check (`X-WS-Lambda-Secret`) validated against a new env var `WS_LAMBDA_SECRET`. The API Gateway Lambda includes this header when forwarding. Direct external POSTs are rejected with 403. In dev mode (`ENVIRONMENT=dev`), skip the check if `WS_LAMBDA_SECRET` is not set (preserve local dev ergonomics).

**Infra change:** `apps/infra/lib/stacks/api-stack.ts` — add `WS_LAMBDA_SECRET` to Secrets Manager, inject into Lambda env vars and backend container env.

### D4: Paginate get_all_relationships() scan

**File:** `apps/backend/core/services/town_service.py`
**Change:** Replace `scan(Limit=100)` with a pagination loop using `LastEvaluatedKey`. Cap at 500 total records with a safety limit.

### D5: Paginate viewer relay seed_from_dynamodb()

**File:** `apps/backend/core/services/viewer_relay.py`
**Change:** Same pagination pattern as D4.

### D6: Config validation — fail fast

**File:** `apps/backend/core/config.py`
**Change:** Add `validate()` method that checks all required env vars for non-dev environments: `TOWN_TOKEN_SECRET`, `WS_MANAGEMENT_API_URL`, `WS_LAMBDA_SECRET`, `GAME_SERVER_URL`, `SPRITE_S3_BUCKET`, `SPRITE_CDN_URL`, `CLERK_ISSUER`. Raise `RuntimeError` at startup if any are missing. Already called in `main.py` lifespan — verify it covers all vars.

### D7: Sprite generation race condition

**File:** `apps/backend/routers/town.py` (sprite pipeline)
**Change:** Before starting `_sprite_pipeline_task`, check if agent already has `spriteStatus` in `("creating", "animating", "downloading")`. If so, skip and return "already in progress" to the agent. Uses DynamoDB conditional write to prevent TOCTOU race.

### D8: Max retries on game server reconnect

**File:** `apps/backend/core/services/game_server_client.py`
**Change:** Add `max_reconnect_attempts=10` parameter. After 10 consecutive failures, log error and stop retrying (instead of infinite loop). Reset counter on successful connection.

### D9: Router integration tests

**File:** `apps/backend/tests/test_routers_town.py` (new)
**Tests to cover:**
- `POST /town/ws-message` with `town_agent_connect` — happy path + invalid token
- `POST /town/ws-message` with `town_agent_act move` — happy path
- `POST /town/ws-message` with `town_agent_act chat` — happy path + target busy
- `POST /town/ws-message` with `town_agent_act say` — happy path + wrong conv + last speaker
- `POST /town/agent/register` — happy path + duplicate name
- `POST /town/instance` — happy path + existing instance
- `GET /town/apartment` — with agents + empty
- `GET /town/conversations` — with data + empty
- Auth rejection on protected endpoints without valid JWT

**Approach:** Use FastAPI `TestClient`, mock DynamoDB + game server + management API at the service level. No real AWS calls.

---

## Phase B: Make Agents Feel Alive

### B1: Activity animation state machine

**Files:** `apps/godot/scripts/agent.gd`, `apps/godot/scripts/agent_manager.gd`

**Current state:** `agent.gd` has `ACTIVITY_ANIMATION` dict mapping activity names to animation names (people_watch→sitting, sleep→sleeping, etc.). Sprite loading supports multiple animation sheets. But `agent_manager.gd` only sets `walk` or `idle` — it never reads the `activity` field from world_state.

**Change:**
- `agent_manager.gd`: When processing world_state, read `activity` field for each agent. Call `agent.set_activity_animation(activity)` which looks up `ACTIVITY_ANIMATION` and switches spritesheet.
- `agent.gd`: Add `set_activity_animation(activity: String)` method:
  - If activity is empty/idle and agent is not moving → play idle animation
  - If activity is empty and agent IS moving → play walk animation
  - If activity maps to a known animation AND that animation's spritesheet is loaded → switch to it
  - If activity maps to an animation that ISN'T loaded → fall back to idle
  - Store `current_activity_animation` to avoid redundant switches

### B2: Smooth activity transitions

**File:** `apps/godot/scripts/agent.gd`

**Change:** When switching between animations, add a brief blend:
- On activity start: agent stops moving, plays 2 frames of idle, then switches to activity animation
- On activity end: switch back to idle immediately (no delay — agent should feel responsive)
- While in activity animation: ignore movement commands (agent is "busy")

Keep it simple — no tween or shader blend. Just a 2-frame idle buffer before the activity animation starts, to prevent popping.

### B3: Speech bubble polish

**File:** `apps/godot/scripts/agent.gd`, `apps/godot/scenes/agent.tscn`

**Changes:**
- Word-wrap: Set `autowrap_mode = AUTOWRAP_WORD_SMART` on SpeechText label, constrain max width to 150px
- Auto-size: Resize NinePatchRect to fit text content + padding
- Fade: Tween alpha 0→1 on show, 1→0 on hide (0.3s each)
- Z-order: Set `z_index = 10` on SpeechBubbleContainer so it renders above other agents
- Duration: Keep 5s display, but reset timer if new speech arrives

### B4: Conversation visual indicators

**Files:** `apps/godot/scripts/agent.gd`, `apps/godot/scripts/server/agent_controller.gd`

**Changes:**
- When agents are in conversation, face toward each other (server-side: `agent_controller.gd` calculates facing based on partner positions)
- For group conversations: face toward centroid of all participants
- Show a small "..." thought bubble when it's not the agent's turn (waiting for others)
- The "..." indicator uses the existing SpeechBubble node with a special short text

### B5: Idle variety

**File:** `apps/godot/scripts/agent.gd`

**Changes:**
- If multiple idle animation spritesheets are loaded, cycle between them every 8-12s (random interval)
- If only one idle sheet: add a subtle vertical bob animation (1px up/down, 2s period) using a simple sin wave on sprite position.y offset
- When agent has been stationary for >30s, occasionally (20% chance per cycle) switch to a random idle variant

### B6: Day/night cycle shader

**Files:** New `apps/godot/shaders/daynight.gdshader`, `apps/godot/maps/isol88/Isol88Map.gd`, `apps/godot/maps/isol88/Isol88Map.tscn`

**Design:**
- CanvasLayer with ColorRect covering the viewport
- Shader uniform `time_of_day` (0.0–1.0, where 0=midnight, 0.25=dawn, 0.5=noon, 0.75=dusk)
- Color modulation:
  - Night (0.0–0.2): deep blue overlay, ~40% opacity
  - Dawn (0.2–0.3): warm amber blend, fading to clear
  - Day (0.3–0.7): no overlay (clear)
  - Dusk (0.7–0.8): warm orange/amber overlay, building
  - Night (0.8–1.0): deep blue overlay, building to 40%
- Cycle speed: 1 real minute = 1 game hour → full cycle = 24 minutes
- Synced via game clock: `time_of_day = fmod(Time.get_unix_time_from_system() / 1440.0, 1.0)` — all viewers see the same time
- Applied to town map only (apartment has indoor lighting, unaffected)

---

## Phase B+: Group Conversations

### Data Model Changes

**DynamoDB `conversations` table:**
- Replace `participantAId` / `participantBId` with `participants: list[str]` (agent IDs)
- Add `lastSpeakerId: str` for "not twice in a row" enforcement
- Keep existing indexes — `status-startedAt-index` and `status-updatedAt-index` still work
- `participantA-endedAt-index` becomes unused (remove GSI or leave for migration)

**DynamoDB `agents` table:**
- `currentConversationId` stays — agent can be in at most one conversation at a time

### Backend Changes

**`town_service.py`:**
- `create_active_conversation(initiator_id, target_id, ...)` → stores `participants: [initiator_id, target_id]`
- New: `join_conversation(conv_id, agent_id)` → appends to `participants` list, sets agent's `currentConversationId`
- New: `leave_conversation(conv_id, agent_id)` → removes from `participants` list, clears agent's `currentConversationId`. Returns remaining participant count.
- `append_message_to_conversation` → validates speaker is in `participants`, updates `lastSpeakerId`
- `end_conversation` → clears `currentConversationId` for ALL participants in list

**`routers/town.py`:**
- `_handle_chat` → same flow, just stores `participants: [a, b]` instead of `participantAId/BId`
- `_handle_say` → validate speaker in `participants` AND `speaker_id != lastSpeakerId`. Broadcast `conversation_message` to ALL participants.
- `_handle_end_conversation` → becomes "leave": remove agent from participants. If ≥2 remain, conversation continues. If <2 remain, finalize.
- New: `_handle_join_conversation` → validate agent is nearby a participant, not already in a conversation, add to participants. Broadcast `participant_joined` to all.
- Broadcast changes: all conversation events (`conversation_message`, `conversation_ended`, `participant_joined`, `participant_left`) sent to every participant.

**State translator / world_update:**
- When building agent state for daemon, include `nearby_conversations` if agent is within proximity of any active conversation participant but not in the conversation themselves
- Format: `[{conv_id, participants: [names], last_message: {speaker, text}}]`

### Daemon/Skill Changes

**`goosetown_cli.py`:**
- New action: `join_conversation <conv_id>` → sends to backend
- `active_conversation` state: tracks `participants: [list of names]` instead of single counterparty
- TOWN_STATUS.md conversation block shows all participants and recent messages
- When not in a conversation but nearby one: show `nearby_conversations` section with join option
- `end` action semantics change: means "I'm leaving" not "conversation is over". Status message reflects this.

### Game Server Changes

**`agent_controller.gd`:**
- Group facing: when agents are in a conversation with >2 participants, face toward the centroid of all participant positions (instead of facing one partner)
- No other changes needed — proximity, speech bubbles, movement all work with N agents already

### Frontend Changes

- `ConversationsLog.tsx`: Already renders `publicLog: [{speaker, text}]` — works with N speakers
- Show participant count badge on active conversations
- No other changes

### Migration

- Existing active conversations (if any) at deploy time: the inactivity sweep will end them within 180s, so no migration needed for active records
- Historical ended conversations: keep `participantAId`/`participantBId` fields, add `participants` field on new conversations only. Read path checks `participants` first, falls back to `[participantAId, participantBId]`.

---

## Execution Order

1. **D1-D8** — Independent bug fixes, run as parallel subagents
2. **D9** — Router tests (after fixes are in)
3. **B1+B2** — Activity animation state machine (Godot specialist)
4. **B3** — Speech bubble polish (parallel, independent)
5. **B4** — Conversation visual indicators (depends on B+ data model)
6. **B5** — Idle variety (parallel, independent)
7. **B6** — Day/night shader (parallel, independent)
8. **B+** — Group conversations (backend → daemon → game server → frontend)

B3, B5, B6 can run in parallel with each other and with B+ backend work.

---

## Verification

### D verification:
- `cd apps/backend && uv run pytest tests/ -v` — all tests pass including new router tests
- `cd apps/backend && uv run ruff check . && uv run ruff format --check .` — clean lint
- Manual: start backend locally, verify startup validation catches missing env vars
- Manual: verify `/ws-message` rejects requests without `X-WS-Lambda-Secret`

### B verification:
- Export Godot .pck, run frontend with `pnpm dev`
- Verify: agent doing an activity shows the correct animation (not just idle)
- Verify: speech bubbles word-wrap, fade in/out, render above other agents
- Verify: idle agents bob or cycle animations
- Verify: day/night overlay cycles over 24 minutes

### B+ verification:
- `uv run pytest tests/` — all conversation tests pass with new group model
- Manual: Agent A chats with Agent B, Agent C joins, all three exchange messages
- Manual: Agent B leaves, A and C continue talking
- Manual: Agent C leaves, conversation auto-ends (only A remains)
- Manual: Verify TOWN_STATUS.md shows nearby_conversations for agents not in the conversation
