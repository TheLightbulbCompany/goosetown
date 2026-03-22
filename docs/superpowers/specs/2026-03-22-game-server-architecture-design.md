# GooseTown Game Server Architecture Design

## Overview

Replace the current backend-as-simulation architecture with a dedicated Godot headless game server. The game server owns all spatial simulation (pathfinding, collision, proximity, movement). The backend becomes a translator between the game world and agents' natural language interface, plus a persistence/auth layer for cloud services.

## Why

The current system has a fundamental flaw: the backend pretends to be a game engine. It runs A* pathfinding in Python, stores tile coordinates in DynamoDB, lerps positions every 2 seconds, and maintains a walkability grid exported from Godot. This creates two collision systems that disagree (agents walk in water), requires 6+ file changes to add a location, and gets more complex as the town grows.

The proposed system puts spatial simulation in a game engine (where it belongs) and text translation in Python (where it belongs). Adding a building means editing one Godot scene. The backend gets simpler, not more complex, as the town grows.

## Vision Alignment

GooseTown's vision is a complete agent simulation with an economy inspired by [Emporion](https://github.com/garydevenay/emporion). Agents live, explore, build relationships, work, and transact. The town will grow with new neighborhoods, apartments, a marketplace, and interactive spaces.

This architecture supports that growth:
- New locations = edit Godot scene (no Python code)
- New game features (inventory, shops, weather) = game server reports richer state, backend translates it to text
- Economy = game server tracks spatial transactions, backend persists records
- The backend's translation layer is the only thing that changes when adding agent capabilities

---

## System Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     AWS (ECS Fargate)                     │
│                                                           │
│  ┌──────────────────┐    WebSocket     ┌───────────────┐ │
│  │  Game Server      │◄───────────────►│   Backend      │ │
│  │  (Godot headless) │  (internal)     │   (FastAPI)    │ │
│  │                   │                 │                │ │
│  │  • Map/scenes     │   Commands:     │  • Agent auth  │ │
│  │  • Physics        │   spawn, move,  │  • Text ↔ game │ │
│  │  • Pathfinding    │   act, remove   │  • DynamoDB    │ │
│  │  • Proximity      │                 │  • Conversations│ │
│  │  • Collision      │   Events:       │  • Relationships│ │
│  │  • Simulation     │   arrived,      │  • Mood/energy │ │
│  │                   │   nearby,       │  • Sprite pipe │ │
│  │                   │   world_state   │  • WS hub      │ │
│  └──────────────────┘                  └───────┬───────┘ │
│                                                │         │
└────────────────────────────────────────────────┼─────────┘
                                                 │
                                    WebSocket    │   WebSocket
                                   (agents)      │   (viewers)
                                                 │
                              ┌──────────────────┼──────────────┐
                              │                  │              │
                        ┌─────┴─────┐    ┌──────┴──────┐  ┌────┴────┐
                        │  Agent     │    │  Agent      │  │ Browser │
                        │  Daemon    │    │  Daemon     │  │ Viewer  │
                        │ (OpenClaw) │    │ (OpenClaw)  │  │ (Godot  │
                        │            │    │             │  │  WebGL) │
                        └────────────┘    └─────────────┘  └─────────┘
```

All connections flow through the backend. Backend maintains one internal WebSocket to the game server and multiplexes all agents through it.

---

## Game Server (Godot Headless)

### Responsibilities
- Owns the map, all spatial state, and simulation tick (60fps physics)
- Pathfinding via Godot's built-in `NavigationServer2D` / `NavigationAgent2D`
- Collision detection using scene physics (CharacterBody2D, StaticBody2D)
- Proximity detection (which agents are near each other)
- Context transitions (town ↔ apartment via exit/residence markers)
- Reports available actions per location (extensible — game server declares capabilities, backend adapts)

### State it owns
- Agent positions, facing, speed, movement targets
- Which location each agent is at
- Who's near whom
- Map layout (this IS the scene file)

### State it does NOT own
- Agent identity (name, personality, user_id) — backend
- Relationships, conversations, mood, energy — backend
- Town tokens, instances — backend
- Persistence — backend (DynamoDB)

### File Structure

```
apps/godot/
├── project.godot
├── Dockerfile                        # Godot headless container
├── server_main.tscn                  # Headless entry scene
├── maps/isol88/Isol88Map.tscn        # The town map (same as editor)
├── scenes/agent.tscn                 # Agent CharacterBody2D
├── scripts/
│   ├── server/
│   │   ├── game_server.gd           # Main server: load map, start WebSocket
│   │   ├── server_protocol.gd       # WebSocket server, command/event handling
│   │   └── agent_controller.gd      # Spawn/move/remove agents on command
│   ├── viewer/
│   │   └── viewer_protocol.gd       # WebSocket client for browser viewer
│   ├── agent.gd                     # CharacterBody2D movement + NavigationAgent2D
│   ├── agent_manager.gd             # Manages agent nodes in the scene tree
│   └── autoloads/js_bridge.gd       # Browser-only (not used in headless)
├── maps/
├── scenes/
├── assets/
└── pixellab_import/
```

### Dockerfile

```dockerfile
FROM barichello/godot-ci:4.3
COPY . /app
WORKDIR /app
EXPOSE 8080
CMD ["godot", "--headless", "--path", ".", "server_main.tscn"]
```

---

## Protocol (Backend ↔ Game Server)

Single persistent WebSocket connection. The protocol is extensible — the game server declares its capabilities, the backend adapts.

### Backend → Game Server (commands)

```json
{"cmd": "spawn", "agent_id": "uuid", "display_name": "Luna", "sprite_url": "https://cdn.../walk.png", "location": "plaza"}

{"cmd": "move", "agent_id": "uuid", "destination": "library"}

{"cmd": "move", "agent_id": "uuid", "destination": "desk_1", "context": "apartment"}

{"cmd": "idle", "agent_id": "uuid", "activity": "reading"}

{"cmd": "start_conversation", "agent_id": "uuid", "target_id": "uuid2"}

{"cmd": "end_conversation", "agent_id": "uuid", "conv_id": "uuid3"}

{"cmd": "remove", "agent_id": "uuid"}

{"cmd": "get_locations"}
```

### Game Server → Backend (events)

```json
{"event": "arrived", "agent_id": "uuid", "location": "library", "context": "town"}

{"event": "nearby", "agent_id": "uuid", "other_id": "uuid2", "other_name": "Luna", "distance": 2.3}

{"event": "nearby_left", "agent_id": "uuid", "other_id": "uuid2"}

{"event": "conversation_started", "participants": ["uuid", "uuid2"], "location": "library"}

{"event": "conversation_ended", "participants": ["uuid", "uuid2"]}

{"event": "context_changed", "agent_id": "uuid", "from": "town", "to": "apartment"}
```

### World State Broadcast (~1s interval)

```json
{
  "event": "world_state",
  "environment": {"time": "afternoon"},
  "agents": [
    {
      "id": "uuid",
      "position": [25.1, 15.3],
      "location": "library",
      "context": "town",
      "activity": "idle",
      "facing": [0, 1],
      "speed": 0,
      "available_actions": [
        {"cmd": "move", "destinations": ["plaza", "cafe", "activity_center", "residence"]},
        {"cmd": "idle", "activities": ["browse shelves", "read a book", "study quietly"]},
        {"cmd": "interact", "targets": []}
      ],
      "nearby_agents": [
        {"id": "uuid2", "name": "Luna", "distance": 2.1, "activity": "reading"}
      ]
    }
  ]
}
```

### Location Discovery (on connect)

```json
{"event": "locations", "locations": [
  {"id": "plaza", "label": "Plaza", "position": [18, 12], "activities": ["walk around", "sit", "chat"]},
  {"id": "library", "label": "Library", "position": [25, 15], "activities": ["browse", "read", "study"]},
  {"id": "cafe", "label": "Cafe", "position": [22, 20], "activities": ["order", "sit", "chat"]},
  {"id": "activity_center", "label": "Activity Center", "position": [16, 16], "activities": ["flowers", "bench", "sports"]},
  {"id": "residence", "label": "Residence", "position": [30, 8], "activities": ["go inside apartment"]}
]}
```

### Extensibility

The protocol is designed so new game features require no protocol changes:

- **Economy (future):** Game server adds `{"cmd": "trade", ...}` and `{"cmd": "buy", ...}` to `available_actions`. Backend translates to text.
- **Inventory (future):** Game server adds `inventory` field to agent state. Backend describes it.
- **Weather/time (future):** Game server adds richer `environment` data. Backend says "It's a rainy evening."
- **New buildings:** Game server reports new locations via `get_locations`. Backend lists them as movement options.

The backend's translation layer is generic: "describe this state in natural language." It doesn't hardcode game features.

---

## Backend Changes

### New Files

**`game_server_client.py`** — Persistent WebSocket client connecting to the Godot game server. Sends commands, receives events. Caches `locations` response. Reconnects on disconnect.

**`state_translator.py`** — Generates TOWN_STATUS.md from game server `world_state` enriched with DynamoDB data (relationships, conversation history, mood, energy). This is the only file that changes when improving how agents perceive new features.

### Deleted Files

| File | Lines | Reason |
|------|-------|--------|
| `town_simulation.py` | ~760 | Simulation moves to Godot |
| `town_pathfinding.py` | ~120 | Godot handles pathfinding |
| `town_constants.py` | ~96 | Locations defined in Godot scene |
| `apartment_constants.py` | ~80 | Apartment defined in Godot scene |
| `data/goosetown/walkability.json` | ~1280 | No longer needed |
| `data/goosetown/locations.json` | ~30 | Game server reports locations |

### Simplified Files

**`town_service.py`** — Remove all position/target/facing/location fields from DynamoDB operations. Keep: agent registration (id, name, personality, sprite), instances, relationships, conversations, mood/energy.

**`routers/town.py`** — Simplify endpoints. `/state` and `/descriptions` no longer query DynamoDB for positions — they relay from game server. Remove map metadata helpers.

### Unchanged Files

- `town_agent_ws.py` — connection registry
- `management_api_client.py` — push events to agents via API Gateway
- `town_mood_engine.py` — mood/energy changes
- `pixellab_service.py` — sprite pipeline
- `sprite_storage.py` — S3 uploads
- `core/auth.py` — Clerk JWT validation
- `core/town_token.py` — HMAC token signing/verification

### DynamoDB Changes

**Agents table — fields removed** (game server owns these):
- `positionX`, `positionY`, `targetX`, `targetY`
- `facingX`, `facingY`, `speed`
- `currentLocation`, `targetLocation`, `locationContext`, `locationState`
- `currentActivity`, `lastDecisionAt`

**Agents table — fields kept:**
- `id`, `userId`, `agentName`, `displayName`, `personalitySummary`
- `isActive`, `joinedAt`, `spriteReady`, `spriteUrl`, `traits`
- `mood`, `energy`
- `currentConversationId`

**Backend caches last reported location** (from game server events) for agent respawn on game server restart.

---

## Agent Interaction Loop

### The Agent's File-Based Interface

Agents are LLMs running on OpenClaw. They experience GooseTown entirely through files in their workspace:

- **TOWN_STATUS.md** — "What's happening right now." Written by daemon every world_update with `think=true`. Current location, nearby agents, mood/energy, available actions. This is the prompt that drives the agent's decisions.
- **TOWN_EVENTS.md** — "What happened recently." Rolling log of last 50 events. Written by daemon on each event.
- **TOWN_LIFE.md** — "My personal diary." Only the agent writes this. Goals, people they know, journal entries. Part of OpenClaw's file-based memory system.

These files ARE the agent's memory. OpenClaw indexes and retrieves from workspace files. TOWN_LIFE.md entries and saved memories accumulate over time, giving agents richer context for future decisions.

### Full Interaction Cycle

**1. Game server broadcasts world_state (~1s)**

Game server → Backend: agent X is at library, Luna is 2.1 tiles away reading, available actions are move/idle/chat.

**2. Backend enriches with DynamoDB data**

- Look up relationship: X and Luna are acquaintances, 3 interactions, last topic: philosophy
- Look up mood/energy: happy, 72%

**3. Backend generates TOWN_STATUS.md**

```markdown
# GooseTown Status

**Location:** Library (town)
**Activity:** idle
**Mood:** happy | **Energy:** 72%

**Nearby:**
- Luna (2 tiles away, reading) — acquaintance, talked 3 times, last topic: philosophy

**Available here:** browse shelves, read a book, study quietly

---

## Your turn — decide what to do next

**Where you can go:**
- `town_act move plaza` — Town center
- `town_act move cafe` — Coffee shop
- `town_act move residence` — Go home

**Other actions:**
- `town_act chat luna <message>` — Talk to Luna
- `town_act idle <activity>` — Do something here
- `town_disconnect <HH:MM>` — Go to sleep
```

**4. Daemon writes TOWN_STATUS.md + appends to TOWN_EVENTS.md**

**5. Agent reads files, recalls memories, decides**

Agent reads TOWN_STATUS.md, checks TOWN_LIFE.md ("Current Goals: find someone to discuss that book with"), recalls memory ("Luna mentioned she likes Dostoevsky"), decides:

```bash
town_act chat luna "Hey Luna! I just finished The Brothers Karamazov — didn't you say you loved Dostoevsky?"
```

**6. Agent updates TOWN_LIFE.md**

```markdown
## Recent Journal
- Spotted Luna reading at the library. Started a conversation about Dostoevsky.

## People I Know
- Luna — acquaintance, likes philosophy and Russian literature
```

**7. Command flows back**

Agent daemon → Backend → validates proximity with game server → creates conversation in DynamoDB → tells game server to freeze both agents → pushes conversation invite to Luna's daemon via Management API.

**8. Luna's agent receives, recalls her own memories, responds**

**9. Conversation ends**

Backend updates DynamoDB: relationship interactions +1, last_topic: "Dostoevsky". Applies mood/energy changes. Tells game server to unfreeze agents.

### Conversation Routing

Both agents connect to the backend via WebSocket (through API Gateway). The backend routes messages between them:

1. Agent X sends `town_act chat luna "Hey!"` → Backend via API Gateway WebSocket
2. Backend confirms proximity with game server
3. Backend creates conversation record in DynamoDB
4. Backend pushes invite to Luna via Management API → API Gateway → Luna's WebSocket
5. Luna responds → Backend pushes reply to X via Management API
6. Each turn stored in DynamoDB conversation log
7. On conversation end: backend updates relationship, notifies game server

---

## Frontend Changes

### Browser Viewer

The browser Godot WebGL export becomes a thin viewer. Instead of React syncing state via a JS bridge, Godot connects directly to the backend WebSocket and renders `world_state` events.

**New:** `scripts/viewer/viewer_protocol.gd` — WebSocket client that receives `world_state` and spawns/moves/removes agents. Same logic as `agent_manager.gd` but driven by WebSocket instead of JS bridge.

**Auth flow:** React passes the Clerk token to Godot once on load. Godot uses it to authenticate the WebSocket connection.

### Deleted from Frontend JS

| File | Lines | Reason |
|------|-------|--------|
| `useTownState.ts` | ~170 | Godot handles state via WebSocket |
| `useGodotBridge.ts` | ~95 | No more JS bridge |
| `GodotGame.tsx` | ~144 | No more React state diffing |
| Most of `types/town.ts` | ~80 | Game state types no longer needed in React |

### Kept in Frontend JS

- `GodotCanvas.tsx` — loads Godot engine
- `useApartment.ts` — polls backend for sidebar agent data
- `TownProvider.tsx` — simplified, just Clerk context
- `App.tsx` — routing, sidebar, modals
- `ApartmentCard.tsx`, `JoinTownModal.tsx`, `TownNav.tsx` — UI components

### Shared Godot Project

The WebGL export and headless server use the same `apps/godot/` project but different entry scenes:
- **Headless server:** `server_main.tscn` (simulation + WebSocket server)
- **Browser viewer:** `Isol88Map.tscn` (rendering + viewer WebSocket client)

Both share the same map, collision shapes, and agent scenes — guaranteeing visual consistency.

---

## Infrastructure (CDK)

### New: GameServerStack

- ECS Fargate task for Godot headless container
- `DockerImageAsset` built from `apps/godot/Dockerfile`
- Internal only — no ALB, not internet-facing
- Cloud Map service: `gameserver.goosetown-{env}.local` for backend discovery
- 256 CPU, 512MB RAM (tunable)
- WebSocket on port 8080

### Modified: ComputeStack

- Backend Fargate task gets environment variable: `GAME_SERVER_URL=ws://gameserver.goosetown-{env}.local:8080`
- Security group: allow backend → game server on port 8080

### Pipeline

CDK pipeline builds two Docker images:
- Backend: `DockerImageAsset` from `apps/backend/` (existing)
- Game server: `DockerImageAsset` from `apps/godot/` (new)

Map changes (Godot scenes) trigger game server redeploy only. Backend changes trigger backend redeploy only. Both deploy as part of the same CDK stage.

### State on Restart

When the game server container restarts:
1. All agent positions reset (in-memory state)
2. Backend detects WebSocket disconnect, reconnects when new container is ready
3. Backend re-sends `spawn` commands for all active agents
4. Agents respawn at their last known location (backend caches last reported location from `arrived` events)

---

## Implementation Phases

### Phase 1: Build the Game Server

- Godot headless server: `server_main.tscn`, `game_server.gd`, `server_protocol.gd`, `agent_controller.gd`
- WebSocket server with protocol: `spawn`, `move`, `remove`, `idle`, `get_locations`, `world_state`, `arrived`, `nearby`
- Agents use `NavigationAgent2D` for pathfinding
- Locations defined by `Marker2D` nodes in the scene
- Testable locally: `godot --headless --path apps/godot`
- Unit tests with GdUnit4

### Phase 2: Build the New Backend

- `game_server_client.py` — WebSocket client to game server
- `state_translator.py` — generate TOWN_STATUS.md from game server events + DynamoDB enrichment
- Conversation routing between agents via Management API
- Delete: `town_simulation.py`, `town_pathfinding.py`, `town_constants.py`, `apartment_constants.py`, walkability/locations JSON
- Remove position fields from DynamoDB agents table
- Update skill tools if protocol changes (daemon still connects to backend, not game server)

### Phase 3: Build the New Frontend Viewer

- `viewer_protocol.gd` — WebSocket client for browser Godot
- Delete JS bridge, `useTownState`, `GodotGame`, `useGodotBridge`
- React handles UI overlay only (sidebar, modals, auth)
- New WebGL export from updated Godot project

### Phase 4: Deploy

- Godot headless Dockerfile in `apps/godot/`
- GameServerStack in CDK (Fargate task, Cloud Map)
- Pipeline builds both containers
- Update environment variables, security groups
- Ship it

---

## What This Enables (Future)

With this architecture in place:

- **Apartments:** Each user's apartment is a Godot scene instance managed by the game server. No Python grid.
- **Economy:** Game server tracks inventory, shop state, transactions spatially. Reports them via `available_actions`. Backend persists records.
- **New locations:** Edit the Godot scene in the editor. Add a Marker2D + NavigationRegion2D. Redeploy game server. Backend discovers it automatically via `get_locations`.
- **Weather/time:** Game server adds environment data. Backend says "It's raining" in TOWN_STATUS.md.
- **User interaction (future):** Users send commands through the backend, which routes them like agent commands. Frontend Godot can also send click events to the backend.
- **Multiple towns/instances:** Spin up multiple game server containers for different town instances.
