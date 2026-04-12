# Remaining Fixes + Features Design

**Date:** 2026-04-12
**Context:** After the Friends 6-agent test, we have unfixed bugs, unfinished implementation items from the ship-ready session, and two future features to build. This spec covers everything needed before the next test.

---

## 1. Shadow Sprite Fallback (Bug Fix)

**Problem:** Agents whose PixelLab sprite generation fails are permanently stuck — `spriteReady=false` blocks game server connection. They can't move, chat, or do anything.

**Fix:** Ship a built-in shadow silhouette sprite. Connect agents to the game server immediately on registration with the shadow sprite. Replace with custom sprite when PixelLab completes.

**Shadow sprite asset:**
- `apps/godot/assets/sprites/shadow_placeholder.png` — 576x384 PNG
- 6 frames x 4 directions at 96px per frame
- Dark silhouette shape (humanoid shadow) — clearly communicates "agent is here, sprite loading"
- Generate programmatically via Python PIL (solid dark semi-transparent humanoid shape on transparent background)

**Backend changes (`routers/town.py`):**
- In `_handle_agent_connect` (the `town_agent_connect` handler): remove the `spriteReady` gate on game server connection. Connect immediately with either the custom sprite URL (if ready) or a hardcoded shadow sprite path.
- The shadow sprite URL: `{SPRITE_CDN_URL}/sprites/shadow_placeholder.png` — upload once to S3 during deploy, or bundle in the Godot export at `res://assets/sprites/shadow_placeholder.png`.
- Simplest approach: bundle in Godot export. The agent.gd sprite loader fetches from URL, but if `sprite_url` is empty, Godot can use the built-in shadow texture directly without a URL fetch.

**Godot changes (`agent.gd`):**
- Add a preloaded shadow texture: `const SHADOW_TEXTURE: Texture2D = preload("res://assets/sprites/shadow_placeholder.png")`
- In `setup()`: if `sprite_url` is empty, use `SHADOW_TEXTURE` as the initial walk spritesheet instead of waiting for a URL fetch.
- When `set_spritesheet()` is called with a real sprite later, it replaces the shadow seamlessly (this already works).

**Flow:**
1. Agent registers → backend connects to game server immediately (no sprite gate)
2. Agent appears in town as shadow silhouette
3. PixelLab runs in background
4. Phase B completes → `update_agent_sprites` pushes real sprite → shadow replaced
5. If PixelLab fails → agent stays as shadow but can still move and chat

---

## 2. Frontend Header Bar Sync (Bug Fix)

**Problem:** The Town/Apartment nav bar can get out of sync with the actual Godot canvas scene. User sees "Town" selected but the apartment map is showing, or vice versa.

**Root cause:** `GodotCanvas.tsx` switches scenes via `JsBridge.switchScene()` in response to `agent_context_changed` WebSocket events, but the React router/nav state doesn't update to match. The nav bar reads from React Router location, not from Godot's actual scene state.

**Fix:**
- In `GodotCanvas.tsx`: when Godot fires `godot_scene_changed` custom event, update React Router to match (`/` for town, `/apartment` for apartment).
- Or: the nav bar should read scene state from a shared context (e.g., `TownProvider`) rather than URL path, and `GodotCanvas` updates that context when scenes switch.

---

## 3. "View in Town" Button (Bug Fix)

**Problem:** The "View in town" link on `ApartmentCard` doesn't navigate correctly.

**Fix:** Investigate current implementation in `ApartmentCard.tsx` — likely a React Router `Link` with wrong path or missing Godot scene switch trigger. Should navigate to `/` and optionally focus camera on the agent.

---

## 4. D10: Infra for WS_LAMBDA_SECRET (Implementation)

**Problem:** The `/ws-message` auth header check was added to the backend but the CDK infra doesn't provision or inject the secret yet.

**Changes:**
- `apps/infra/lib/stacks/auth-stack.ts`: Add `WS_LAMBDA_SECRET` to Secrets Manager (auto-generated 64-char string)
- `apps/infra/lib/stacks/compute-stack.ts`: Inject as ECS container secret
- `apps/infra/lib/stacks/api-stack.ts`: Inject ARN into `$default` Lambda env, grant read, Lambda reads secret at cold start and includes in `X-WS-Lambda-Secret` header when POSTing to `/ws-message`

---

## 5. BP6: nearby_conversations in World Update (Implementation)

**Problem:** Agents can't see nearby conversations to join. The daemon has `nearby_conversations` rendering in TOWN_STATUS.md but the backend never populates it.

**Changes (`routers/town.py`):**
- In the game server event callback (`_on_game_server_event`) for `agent_state` events, before sending to daemon:
  - Check if any nearby agents (from the `agent_state` event's nearby list) have `currentConversationId` set
  - If so, fetch those conversations and include `nearby_conversations` in the daemon payload
  - Format: `[{conv_id, participants: [display_names], last_message: {speaker, text}, location}]`
- Only include conversations the agent is NOT already in

---

## 6. BP8: Game Server Group Facing (Implementation)

**Problem:** In group conversations, agents don't face toward each other.

**Changes:**
- `apps/godot/scripts/server/agent_controller.gd`: Add `face_agents_toward_centroid(agent_ids)` method that computes the centroid of all participants and faces each agent toward it
- `apps/backend/routers/town.py`: In `_handle_join_conversation` and `_handle_chat`, after establishing/joining a conversation, send a `set_conversation_group` command to the game server with all participant IDs
- Game server dispatches to `face_agents_toward_centroid`

---

## 7. Event-Driven Think Triggers (Feature)

**Problem:** Agents only think on a fixed 12s+ throttle. If another agent walks into proximity at second 3, the first agent won't notice until second 12-15. Conversations feel sluggish.

**Changes (`routers/town.py` + `goosetown_cli.py`):**

**Backend side:**
- In the game server event callback, for specific event types, set `think=true` on the daemon payload regardless of throttle:
  - `nearby` (new agent entered proximity)
  - `nearby_left` (agent left proximity)
  - `arrived` (agent reached destination)
  - `conversation_invite` already triggers immediate write

**Daemon side (`goosetown_cli.py`):**
- In `_handle_agent_state`: currently throttles think to 12s+ of idle. Add override: if the event contains `nearby` data that differs from the previous nearby list (new agent appeared or one left), set `think=true` regardless of throttle.
- This means the agent's LLM fires immediately when someone walks up, but stays on the 12s+ throttle when nothing changes.

**Token cost impact:** Minimal for idle agents (same as today). Slightly more for agents in busy locations with people coming and going. Bounded by the game server's 2s broadcast interval — at worst, one extra think per new proximity event.

---

## 8. PixelLab Sprite Job Queue (Feature)

**Problem:** 6 simultaneous PixelLab jobs caused 2-3 failures (HTTP 500). Need bounded concurrency.

**Changes:**

**New file: `apps/backend/core/services/sprite_queue.py`**
- `SpriteQueue` class with an `asyncio.Semaphore(3)` for Phase A+B (character creation + static sprite)
- Phase C animations queued behind a separate `Semaphore(2)` (lower priority)
- Retry failed jobs up to 3 times with exponential backoff (5s, 10s, 20s)
- Report queue position to agent via keepalive: "You are #4 in the sprite queue"

**Changes to `routers/town.py`:**
- Replace direct `asyncio.create_task(_sprite_pipeline_task(...))` with `sprite_queue.enqueue(agent_id, display_name, appearance)`
- Queue manages task lifecycle, retries, and concurrency

**Singleton:** One `SpriteQueue` instance per backend process, initialized in `main.py` lifespan.

---

## 9. Deploy + Test

**Deploy:**
- Push all changes to main
- CI/CD deploys to dev (backend + game server + frontend)
- Verify game server picks up nav target fix

**Test plan:**
- Clean DynamoDB tables
- Get token from frontend
- Spin up 3-6 agents via team
- Verify:
  - [ ] Agents stay connected (no disconnect loop)
  - [ ] Shadow sprite shows for agents waiting on PixelLab
  - [ ] Custom sprite replaces shadow when ready
  - [ ] Agents move between locations
  - [ ] Agents chat when in proximity (pixel distance check)
  - [ ] Group conversation: A chats B, C joins, all exchange messages
  - [ ] Agents face toward conversation partners
  - [ ] nearby_conversations shows in TOWN_STATUS.md for bystanders
  - [ ] Event-driven think: agent reacts immediately when someone walks up
  - [ ] Day/night cycle visible on frontend
  - [ ] Activity animations play correctly
  - [ ] Speech bubbles fade in/out with word-wrap
  - [ ] Frontend header bar matches displayed map
  - [ ] "View in town" button works
