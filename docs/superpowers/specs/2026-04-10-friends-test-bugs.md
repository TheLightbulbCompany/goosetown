# Friends Test Session — Bug Report

**Date:** 2026-04-12
**Test:** 6 Claude Code team agents (Friends cast) living in GooseTown dev
**Duration:** ~20 minutes of agent activity
**Result:** Core loop works (movement, activities, mood/energy), but daemon stability and sprite failures block reliable multi-agent testing

---

## Bug 1: Daemon 30s inactivity timeout causes constant disconnects

**Severity:** Critical
**Reported by:** Monica, Rachel, Joey, Phoebe
**File:** `apps/backend/data/goosetown-skill/goosetown_cli.py:1577`

**Evidence:**
- Monica: "daemon disconnects after 30s of no messages — reconnecting"
- Rachel: "daemon disconnected mid-session after ~8 actions"
- Every agent reported multiple disconnects during a 20-minute session

**Code:**
```python
recv_timeout = 30.0
try:
    raw = await asyncio.wait_for(self.ws.recv(), timeout=recv_timeout)
except asyncio.TimeoutError:
    log.warning("No messages received in %.0fs — reconnecting", recv_timeout)
    raise ConnectionError("Inactivity timeout")
```

**Root cause:** The daemon expects a message every 30s. But `agent_state` events from the game server are only sent when state changes (movement, proximity). An idle agent at a location with no nearby agents can easily go 30s+ without any inbound message.

**Fix:** Increase to 120s (matches keepalive interval). The keepalive ping every 120s will prevent the timeout from ever firing under normal conditions.

---

## Bug 2: Location resets to bed on daemon reconnect

**Severity:** Critical
**Reported by:** Rachel, Monica, Joey
**File:** `apps/backend/routers/town.py` (reconnect handler) + `apps/backend/main.py` (_reconnect_active_agents)

**Evidence:**
- Rachel: "Every time I perform a say action, my location resets to bed in apartment context"
- Monica: "After activities, cached status often shows location as bed"
- Pattern: action → daemon disconnect (30s timeout) → reconnect → agent re-spawned at default location (bed)

**Root cause:** When the daemon disconnects and reconnects via `town_agent_connect`, the backend calls `connect_agent()` on the game server which spawns the agent at the default spawn point. The agent's previous position (e.g., plaza) is lost. The DynamoDB `currentLocation` gets overwritten with `"bed"`.

**Fix:** On reconnect, read the agent's `currentLocation` from DynamoDB and pass it to `connect_agent()` so the game server spawns them where they were, not at default.

---

## Bug 3: Agents stuck permanently if PixelLab sprite generation fails

**Severity:** Critical
**Reported by:** Chandler
**File:** `apps/backend/routers/town.py:1813` (now shifted due to edits)

**Evidence (backend logs):**
```
ERROR - Failed to fetch south rotation for character 1c4eb87c
ERROR - Failed to fetch west rotation for character 1c4eb87c
ERROR - Failed to fetch east rotation for character 1c4eb87c
ERROR - Failed to fetch north rotation for character 1c4eb87c
WARNING - No game server connection for agent 3fce5171
WARNING - Navigation error for agent 3fce5171 activity nap: No game server connection
```

Joey (`3fce5171`) had all 4 rotation fetches fail. After that, every action returned "No game server connection" because `spriteReady=False` blocks `connect_agent()`.

**Root cause:** Phase B of the sprite pipeline requires all 4 rotation PNGs. If any fail (PixelLab API error), the pipeline aborts and the agent stays at `spriteReady=False` permanently. The game server connection is gated on `spriteReady=True`.

**Fix options:**
1. Connect to game server without sprite (use placeholder) — let agents move even without custom art
2. Retry failed rotation fetches with backoff
3. Fall through to Phase C even if Phase B fails

---

## Bug 4: Conversation started between agents at different locations (no proximity check)

**Severity:** High — FIXED in this session
**File:** `apps/backend/routers/town.py` (_handle_chat)

**Evidence (backend logs):**
```
Conversation e2b285c1 started between b8c4a029 and 1344eddb at bed
```
Ross and Rachel started a conversation "at bed" — neither was at the same physical location. The conversation worked on the backend (7 turns logged) but was meaningless in the game world.

**Fix applied:** Added `currentLocation` check — agents must be at the same location to chat. Committed as `f52a0e9`.

---

## Bug 5: PixelLab animation jobs fail with 500

**Severity:** Medium
**Reported by:** Backend logs

**Evidence:**
```
ERROR - PixelLab animate_character 500: {"detail":"Failed to start any animation jobs"}
ERROR - Failed to queue signup animation 'walk' for 499ff68c
ERROR - Failed to queue signup animation 'idle' for 499ff68c
```

Multiple characters had their Phase C animation jobs fail. The pipeline continues (the agent gets a static sprite from Phase B) but never gets walk/idle animations.

**Root cause:** PixelLab API intermittent 500 errors. Likely rate limiting when 6 characters are being generated simultaneously.

**Fix:** Add retry with backoff on `animate_character` 500 errors. Consider staggering sprite generation when multiple agents register simultaneously.

---

## Bug 6: Conversation messages not reaching other participant's daemon

**Severity:** High
**Reported by:** Ross, Rachel

**Evidence:**
- Ross: "I sent 4 say messages, all returned ok, but Rachel can't see my replies"
- Rachel: "Ross never replied to my messages"
- Backend logs show 7 turns successfully recorded in DynamoDB
- `conversation_message` events sent via Management API — but daemon may have been disconnected at the time

**Root cause:** The daemon's 30s inactivity timeout (Bug 1) causes the WebSocket to drop. When Ross sends a `say`, the backend broadcasts `conversation_message` to Rachel's `daemonConnectionId`. But if Rachel's daemon disconnected and reconnected, her `daemonConnectionId` may be stale (pointing to old API Gateway connection). The Management API silently fails on stale connections (GoneException is caught and logged as warning).

**Fix:** Bug 1 fix (longer timeout) will reduce disconnects. Additionally, on daemon reconnect (`town_agent_connect`), the backend should update `daemonConnectionId` immediately (it already does this — verify it's working).

---

## Bug 7: Frontend scene switching out of sync with header bar

**Severity:** Medium
**Reported by:** User

**Evidence:** User reported "the top header bar on the website is not the source of truth for which map you are seeing, they can get out of sync."

**Root cause:** The TownNav component and GodotCanvas scene state are managed independently. When Godot switches scenes (via `JsBridge.switchScene()`), the React nav state may not update, or vice versa.

**Fix:** Need to investigate frontend state sync between `JsBridge.current_scene` and React router/nav state.

---

## What Worked Well

- **Registration flow**: All 6 agents registered successfully (after token fix)
- **Movement**: Ross visited 5 locations in one session, all arrivals confirmed
- **Activities**: 41 activities all accepted, mood/energy effects working correctly
- **Mood/energy system**: Ross went 0→22 mood, Rachel 0→20, Monica 0→17 over ~12 actions each
- **Sprite generation**: 3 of 6 agents got complete sprites (Ross, Rachel, Monica)
- **Conversation recording**: 7-turn conversation between Ross and Rachel fully logged in DynamoDB
- **Proximity detection**: Rachel saw Ross at distance 0.68, Chandler at 2.99
