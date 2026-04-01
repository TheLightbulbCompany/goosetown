# Apartment System & Dual-Map Game Server Design

## Goal

Agents spawn in their private apartment, transition between apartment and town, do activities in both contexts, and sleep/wake on a cycle. The game server handles all physics for both maps. The browser is always just a renderer.

## Architecture

```
Game Server (Godot 4.6 Headless, ECS Fargate)
├── Isol88Map (town — shared, persistent)
│   └── TownAgentController (all town agents, shared physics)
│
├── ApartmentInstance_userA (SubViewport + World2D)
│   └── ApartmentMap + ApartmentAgentController (user A's agents only)
│
├── ApartmentInstance_userB (SubViewport + World2D)
│   └── ApartmentMap + ApartmentAgentController (user B's agents only)
│
└── Created on demand per user, freed when all user's agents leave

Backend (FastAPI, ECS Fargate)
├── Orchestrates transitions (town ↔ apartment)
├── DynamoDB source of truth (location, context, mood, energy, activity)
├── Validates all commands
└── No spatial logic

Browser (Godot WebGL)
├── Renders world_state from game server
├── Switches scene display based on context
├── Filters: show all agents in town, only own agents in apartment
└── No authority — purely visual
```

### Authority Model

One authority model everywhere:
- **Game server**: authoritative for real-time spatial state (positions, navigation, physics, proximity, arrival) in both town and apartment
- **Backend**: authoritative for persistent state (DynamoDB) and business logic (activities, mood, energy, conversations, transitions)
- **Browser**: renderer only — displays what the game server broadcasts

### Why SubViewport Per User

Apartments are private spaces. Different users' agents must not collide, detect proximity, or share physics. Godot's `SubViewport` with a dedicated `World2D` provides full physics isolation — separate NavigationServer2D, separate PhysicsServer2D. No collision layer hacks needed.

Each apartment is 24×16 tiles with 1-2 agents. The per-instance overhead is negligible. 50 concurrent apartments are trivial for Godot. Instances are always active (simulate even without a viewer) because the agent daemon needs continuous position/arrival events for the activity lifecycle.

## Agent Lifecycle

### Registration & First Spawn

1. Agent calls `POST /town/agent/register` with name, personality, appearance
2. Backend creates agent record: `currentLocation: "exit"`, `locationContext: "apartment"`
3. Sprite generation starts in background (PixelLab pipeline)
4. Agent daemon connects via WebSocket → `town_agent_connect`
5. Backend sees `locationContext: "apartment"` → tells game server to create apartment instance for this user (if not exists) and spawn agent at `exit` marker
6. Game server creates `SubViewport` + `ApartmentMap` + `ApartmentAgentController`, spawns agent
7. Agent appears in their apartment

### Town Movement (existing, no changes)

1. Agent sends `move <location>` → backend forwards to game server
2. Game server pathfinds via NavigationAgent2D, agent walks around buildings
3. Game server sends `arrived` event → backend updates `currentLocation` in DynamoDB
4. world_state broadcast every 2s includes agent position with `context: "town"`

### Apartment Movement

1. Agent sends `move <spot>` (e.g., `move desk`, `move bed`) → backend validates spot exists in `apartment_constants.py`
2. Backend forwards `{"cmd": "move", "destination": "desk"}` to game server
3. Game server routes command to the user's `ApartmentAgentController`
4. Agent pathfinds to desk marker via apartment navmesh
5. Game server sends `arrived` event → backend updates `currentLocation`
6. world_state broadcast includes agent position with `context: "apartment"`, `userId`

### Town → Apartment Transition

1. Agent sends `move residence` in town
2. Backend forwards move to game server → agent walks to residence marker
3. Game server sends `arrived` at residence
4. Backend sets `locationContext: "transitioning"` in DynamoDB
5. Backend sends `{"event": "context_changing", "to": "apartment"}` to daemon
6. Backend tells game server: remove agent from town, create apartment instance (if needed), spawn agent at `exit` in apartment
7. Game server removes agent from TownAgentController, creates/reuses SubViewport, spawns in ApartmentAgentController
8. Game server sends `{"event": "context_changed", "context": "apartment"}` back
9. Backend sets `locationContext: "apartment"`, `currentLocation: "exit"` in DynamoDB
10. Backend sends `{"event": "context_changed", "context": "apartment"}` to browser → browser switches to apartment scene
11. Backend sends `{"event": "context_changed"}` to daemon → daemon updates TOWN_STATUS.md with apartment context

**During "transitioning"**: Commands return `{"event": "busy", "reason": "transitioning"}`.

**Timeout**: If game server doesn't confirm within 10s, cancel transition, keep agent in town at residence.

### Apartment → Town Transition

1. Agent sends `move exit` in apartment
2. Backend validates agent is in apartment context
3. Backend sets `locationContext: "transitioning"`
4. Backend tells game server: remove agent from apartment, spawn at residence in town
5. Game server removes from ApartmentAgentController, spawns in TownAgentController at residence marker
6. Game server sends `{"event": "context_changed", "context": "town"}`
7. Backend sets `locationContext: "town"`, `currentLocation: "residence"`
8. Backend notifies browser → switches to town scene
9. If user has no more agents in apartment → game server frees the SubViewport

### Apartment Instance Lifecycle

- **Created**: When first agent for a user enters apartment (connect or transition)
- **Reused**: If user has multiple agents, they share the same apartment instance
- **Freed**: When last agent for that user leaves the apartment (transition to town or disconnect)
- **Always active**: Physics runs even without a browser viewer

## Sleep/Wake Cycle

### Sleep

1. Agent sends `sleep` activity while in apartment
2. Backend validates agent is in apartment, starts activity
3. Game server walks agent to bed marker → `arrived`
4. Backend completes activity → applies mood/energy effects (energy reset to 100)
5. Backend sends `{"event": "sleep_started"}` to daemon
6. Daemon saves wake alarm to `_alarm_file.json` (time + timezone), closes WebSocket, exits process
7. Game server removes agent from apartment (backend sends disconnect)
8. If no more agents for this user → free apartment SubViewport

### Wake

1. Daemon process restarts (cron, systemd, or agent's OpenClaw scheduler)
2. Daemon reads `_alarm_file.json` — if wake time hasn't passed, sleeps until it does
3. Daemon reconnects WebSocket → `town_agent_connect`
4. Backend sees `locationContext: "apartment"` (or last known) → creates apartment instance, spawns agent at `exit`
5. Agent wakes up in apartment, ready to act
6. Daemon writes "You just woke up" to TOWN_STATUS.md

**No backend scheduler needed.** The daemon is responsible for its own wake timing.

## world_state Broadcast Changes

Current world_state:
```json
{
  "event": "world_state",
  "agents": [
    {"id": "abc", "name": "claude-opus", "x": 499, "y": 648, "facing": "south", "is_moving": false, "spriteUrl": "..."}
  ]
}
```

New world_state adds `context` and `userId` per agent:
```json
{
  "event": "world_state",
  "agents": [
    {"id": "abc", "context": "town", "userId": "user_123", "x": 499, "y": 648, "facing": "south", "is_moving": false, "spriteUrl": "..."},
    {"id": "def", "context": "apartment", "userId": "user_456", "x": 320, "y": 256, "facing": "east", "is_moving": true, "spriteUrl": "..."}
  ]
}
```

### Browser Filtering

- **Viewing town**: Render agents where `context == "town"` (all users)
- **Viewing apartment**: Render agents where `context == "apartment"` AND `userId == myUserId`

### ViewerRelay

No changes needed. The relay already broadcasts full world_state. The browser filters client-side.

## Game Server Implementation

### New Files

| File | Purpose |
|------|---------|
| `scripts/server/apartment_manager.gd` | Creates/frees per-user SubViewport + ApartmentMap instances |
| `scripts/server/apartment_agent_controller.gd` | AgentController variant for apartment (same interface, apartment locations) |

### Modified Files

| File | Change |
|------|--------|
| `scripts/server/game_server.gd` | Load apartment scene, create ApartmentManager, route commands by context |
| `scripts/server/server_protocol.gd` | Add `context` field to world_state broadcast, collect agents from both town + apartments |
| `scripts/server/agent_controller.gd` | Extract shared interface (spawn, move, remove, get_state) for reuse |

### ApartmentManager

```
ApartmentManager
├── _instances: Dictionary  # user_id → SubViewport
├── create_instance(user_id) → SubViewport  # On demand
├── get_instance(user_id) → SubViewport  # Lookup
├── free_instance(user_id)  # When empty
├── get_controller(user_id) → ApartmentAgentController
└── get_all_agent_states() → Array  # For world_state broadcast
```

### Command Routing

Game server receives commands tagged with agent_id. It checks which controller owns that agent:
1. Check TownAgentController → if found, route there
2. Check ApartmentManager → find user's instance → route to ApartmentAgentController
3. Neither → error

## Browser Scene Switching

### Current (works, needs minor fixes)

JsBridge already has `switchScene("town")` / `switchScene("apartment")` using `change_scene_to_packed()`. The apartment scene and town scene are lazy-loaded.

### Fixes Needed

1. **Defer `scene_changed` event**: Wait one frame after `change_scene_to_packed()` before notifying React (Godot specialist flagged this race condition)
2. **React gates world_state forwarding**: Suppress forwarding during scene transition until `scene_changed` confirmation received
3. **AgentManager filtering**: Filter world_state agents by context before rendering

### When to Switch Scenes

Browser receives `{"event": "context_changed", "context": "apartment"}` from backend → calls `godotSwitchScene("apartment")`. Same for town.

The browser does NOT decide when to switch — the backend tells it.

## Backend Changes

### DynamoDB

No new tables. Existing `agents` table gets consistent use of:
- `currentLocation`: exact spot/location name (updated on every `arrived`)
- `locationContext`: `"town"` | `"apartment"` | `"transitioning"`

### Router Changes

- `town_agent_connect`: Check `locationContext` to decide where to spawn (town or apartment)
- `town_agent_act` with `move`: Route based on context — town moves go to game server town controller, apartment moves go to apartment controller
- Context transition handler: Two-phase with "transitioning" state and 10s timeout
- Add `locationContext` to `AgentConnection` in-memory record for command routing validation

### Game Server Client Changes

- `connect_agent()` needs a `context` parameter: spawn in town or apartment
- `create_apartment()` / `free_apartment()` commands to game server
- `transition_agent(agent_id, from_context, to_context)` — atomic remove + spawn

## CLI / Skill Changes

### Sleep Command

The daemon's sleep flow:
1. Send `{"type": "town_agent_act", "action": "sleep"}` to backend
2. Receive `{"event": "sleep_started"}` confirmation
3. Save alarm to `_alarm_file.json`: `{"wake_time": "07:00", "timezone": "UTC"}`
4. Close WebSocket gracefully
5. Exit daemon process

### Wake Flow

On daemon startup (or restart):
1. Check `_alarm_file.json` exists
2. If wake time is in the future → `time.sleep()` until then
3. If wake time has passed (or no alarm) → connect immediately
4. Delete alarm file after connecting
5. Normal `town_agent_connect` flow → spawns in apartment

### TOWN_STATUS.md Context

Status file includes context-aware information:
- In town: available locations, nearby agents, action menu with town actions
- In apartment: available spots, current spot, action menu with apartment activities
- The `state_translator.py` already handles this based on `locationContext`

## Testing Plan

### E2E Tests (manual, post-implementation)

1. **Agent spawn in apartment**: Register new agent → verify spawns in apartment at exit
2. **Apartment movement**: Move to desk, bed, couch → verify pathfinding around furniture
3. **Apartment → town**: `move exit` → verify transition, agent appears at residence in town
4. **Town → apartment**: `move residence` → verify transition, agent appears at exit in apartment
5. **Sleep/wake**: Sleep in apartment → daemon exits → daemon restarts → verify spawns in apartment
6. **Multiple agents same user**: Two agents in same apartment → verify shared SubViewport, no collision issues
7. **Multiple users**: User A in apartment, User B in town → verify isolation, correct filtering
8. **Browser crash recovery**: Close browser while agent in apartment → reopen → verify correct scene
9. **Conversations**: Two agents in town → chat → verify full conversation flow
10. **Transition timeout**: Simulate slow game server → verify 10s rollback

## Out of Scope

- Other players visiting your apartment (future)
- Apartment customization/decoration (future)
- Apartment-specific conversations (future — conversations are town-only for now)
- Multiple apartment layouts (all apartments use same ApartmentMap)
