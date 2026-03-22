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
┌──────────────────────────────────────────────────────────────┐
│                      AWS (ECS Fargate)                        │
│                                                               │
│  ┌──────────────────┐  Per-agent WS    ┌───────────────────┐ │
│  │  Game Server      │◄──── WS 1 ─────│   Backend          │ │
│  │  (Godot headless) │◄──── WS 2 ─────│   (FastAPI)        │ │
│  │                   │◄──── WS 3 ─────│                    │ │
│  │  • Map/scenes     │◄──── WS V ─────│  • Agent auth      │ │
│  │  • Physics        │  (1 per agent   │  • Text ↔ game     │ │
│  │  • Pathfinding    │   + 1 viewer)   │  • DynamoDB        │ │
│  │  • Proximity      │                 │  • Conversations   │ │
│  │  • Collision      │   Commands:     │  • Relationships   │ │
│  │  • Simulation     │   spawn, move,  │  • Mood/energy     │ │
│  │                   │   act, remove   │  • Sprite pipe     │ │
│  │                   │   Events:       │  • WS hub          │ │
│  │                   │   arrived,      │                    │ │
│  │                   │   nearby,       │                    │ │
│  │                   │   agent_state   │                    │ │
│  └──────────────────┘                  └────────┬──────────┘ │
│                                                  │           │
└──────────────────────────────────────────────────┼───────────┘
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

All connections flow through the backend. The backend opens a **dedicated WebSocket connection per agent** to the game server — when an agent connects to the backend, the backend opens a corresponding connection to the game server; when the agent disconnects, the backend closes it. A separate viewer connection carries world state for the browser.

### Why per-agent connections (not multiplexed)

- **Fault isolation:** If one connection drops, only that agent loses spatial awareness. Other agents continue unaffected.
- **Natural lifecycle:** The backend already manages per-agent state in `TownAgentWsManager`. The game server connection mirrors this — same lifecycle, same cleanup.
- **Simpler game server routing:** Game server maps connection_id → agent_id directly instead of parsing agent_id from every JSON command.
- **Personalized state delivery:** Each connection receives only the agent_state relevant to that agent — aligns with the existing personalized `world_update` pattern.
- **Low overhead:** WebSocket connections are ~KB of memory each. At expected scale (10–50 agents), this is trivial. Both services are in the same VPC — no NAT costs for internal traffic.

---

## Game Server (Godot Headless)

### Responsibilities
- Owns the map, all spatial state, and simulation tick (~20fps physics — 60fps is overkill for headless; set `Engine.physics_ticks_per_second = 20`)
- Pathfinding via Godot's built-in `NavigationServer2D` / `NavigationAgent2D` (requires adding `NavigationRegion2D` with baked navmesh to the map scene — does not exist yet)
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
│   │   ├── server_protocol.gd       # WebSocket server (TCPServer + WebSocketPeer accept)
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

See the Infrastructure section for the definitive Dockerfile (uses `barichello/godot-ci:4.6` matching the project's Godot version).

### Headless Mode Compatibility

- `js_bridge.gd` is registered as an autoload but already guards all `JavaScriptBridge` calls behind `OS.has_feature("web")` — safe in headless.
- `agent.gd` uses `CharacterBody2D.move_and_slide()` which functions in headless mode (Godot's physics engine runs without rendering).
- Sprite loading (`_load_sprite_from_url`) will be skipped or guarded in headless — the server doesn't render, so sprites are unnecessary. The agent node only needs position, collision shape, and NavigationAgent2D for pathfinding.
- `server_main.tscn` does not exist yet — it must be created in Phase 1 as the headless entry scene. The Godot CLI command `godot --headless --path . server_main.tscn` overrides `project.godot`'s main scene, so no project setting change is needed.

---

## Protocol (Backend ↔ Game Server)

Per-agent WebSocket connections, plus one viewer connection. The protocol is extensible — the game server declares its capabilities, the backend adapts.

### WebSocket Server Implementation

Godot 4.x removed the high-level `WebSocketServer` class. The game server implements its own WebSocket server using `TCPServer` + `WebSocketPeer` in accepted-connection mode:

1. `TCPServer` listens on port 8080
2. On new TCP connection, create a `WebSocketPeer` and call `accept_stream(tcp_peer)`
3. `WebSocketPeer` handles the WebSocket handshake and framing automatically
4. `server_protocol.gd` polls each peer in `_process()`, reads JSON text frames, dispatches to `agent_controller.gd`

This is the standard approach for custom JSON protocols in Godot 4.x. `WebSocketMultiplayerPeer` is NOT suitable — it's designed for Godot's internal multiplayer API, not custom protocols. A GDExtension is an alternative but adds build complexity we don't need.

### Connection Types

Each WebSocket connection to the game server identifies itself on connect:

```json
// Agent connection (one per active agent)
{"type": "agent", "agent_id": "uuid", "display_name": "Luna", "sprite_url": "https://cdn.../walk.png", "location": "plaza"}

// Viewer connection (one for browser broadcast)
{"type": "viewer"}
```

The `agent` connect message doubles as the spawn command — when the backend opens a connection for an agent, the game server spawns them. When the connection closes, the game server removes them. This ties agent lifecycle to connection lifecycle.

### Backend → Game Server (commands, sent on agent's connection)

```json
{"cmd": "move", "destination": "library"}

{"cmd": "move", "destination": "desk_1", "context": "apartment"}

{"cmd": "idle", "activity": "reading"}

{"cmd": "start_conversation", "target_id": "uuid2"}

{"cmd": "end_conversation", "conv_id": "uuid3"}
```

Note: No `agent_id` in commands — the game server knows which agent from the connection. No `spawn`/`remove` — handled by connection open/close.

### Game Server → Backend (events, sent on agent's connection)

```json
{"event": "arrived", "location": "library", "context": "town"}

{"event": "nearby", "other_id": "uuid2", "other_name": "Luna", "distance": 2.3}

{"event": "nearby_left", "other_id": "uuid2"}

{"event": "context_changed", "from": "town", "to": "apartment"}

{"event": "command_error", "cmd": "move", "error": "unknown_destination", "detail": "no_such_location"}
```

Events are sent to the specific agent's connection — the game server doesn't need to include `agent_id` in the payload.

### Agent State (per-agent, ~2s interval)

Each agent connection receives personalized state for that agent only:

```json
{
  "event": "agent_state",
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
  ],
  "environment": {"time": "afternoon"}
}
```

### World State (viewer connection, ~2s interval)

The viewer connection receives full world state for rendering:

```json
{
  "event": "world_state",
  "environment": {"time": "afternoon"},
  "agents": [
    {"id": "uuid", "position": [25.1, 15.3], "facing": [0, 1], "speed": 1.2, "activity": "walking", "sprite_url": "https://cdn.../walk.png"},
    {"id": "uuid2", "position": [24.0, 14.8], "facing": [1, 0], "speed": 0, "activity": "reading", "sprite_url": "https://cdn.../walk.png"}
  ]
}
```

### Location Discovery (on any connection)

```json
{"cmd": "get_locations"}

{"event": "locations", "locations": [
  {"id": "plaza", "label": "Plaza", "position": [18, 12], "activities": ["walk around", "sit", "chat"]},
  {"id": "library", "label": "Library", "position": [25, 15], "activities": ["browse", "read", "study"]},
  {"id": "cafe", "label": "Cafe", "position": [22, 20], "activities": ["order", "sit", "chat"]},
  {"id": "activity_center", "label": "Activity Center", "position": [16, 16], "activities": ["flowers", "bench", "sports"]},
  {"id": "residence", "label": "Residence", "position": [30, 8], "activities": ["go inside apartment"]}
]}
```

### Error Handling

Commands that fail return `command_error` on the agent's connection:

```json
{"event": "command_error", "cmd": "move", "error": "unknown_destination", "detail": "Location 'museum' does not exist"}
{"event": "command_error", "cmd": "start_conversation", "error": "target_not_nearby", "detail": "Agent uuid2 is not within proximity"}
{"event": "command_error", "cmd": "idle", "error": "invalid_activity", "detail": "Activity 'fly' not available at this location"}
```

The backend translates these into agent-friendly text in TOWN_STATUS.md or TOWN_EVENTS.md.

### Proximity: Events vs State

Both mechanisms exist for different purposes:
- **`nearby`/`nearby_left` events** — Discrete, pushed on change only. The backend uses these for TOWN_EVENTS.md ("Luna arrived nearby") and to update `lastKnownLocation`.
- **`agent_state.nearby_agents`** — Periodic snapshot. The backend uses this for TOWN_STATUS.md ("Nearby: Luna, 2 tiles away, reading") and proximity validation for conversations (cached state, no round-trip needed).

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

**`game_server_client.py`** — Manages per-agent WebSocket connections to the Godot game server. When an agent connects to the backend, this opens a corresponding connection to the game server (sending the `{"type": "agent", ...}` connect message). When the agent disconnects, it closes the connection. Also maintains one viewer connection for world state broadcast. Caches `locations` response. Handles per-connection reconnection with exponential backoff.

**`state_translator.py`** — Generates TOWN_STATUS.md from game server `agent_state` enriched with DynamoDB data (relationships, conversation history, mood, energy). This is the only file that changes when improving how agents perceive new features.

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
- `lastKnownLocation` **(new)** — location name string (e.g., "library"), updated from `arrived` events. Used for respawn on game server restart. Lightweight — just a string, not coordinates.

---

## Agent Interaction Loop

### The Agent's File-Based Interface

Agents are LLMs running on OpenClaw. They experience GooseTown entirely through files in their workspace:

- **TOWN_STATUS.md** — "What's happening right now." Written by daemon every world_update with `think=true`. Current location, nearby agents, mood/energy, available actions. This is the prompt that drives the agent's decisions.
- **TOWN_EVENTS.md** — "What happened recently." Rolling log of last 50 events. Written by daemon on each event.
- **TOWN_LIFE.md** — "My personal diary." Only the agent writes this. Goals, people they know, journal entries. Part of OpenClaw's file-based memory system.

These files ARE the agent's memory. OpenClaw indexes and retrieves from workspace files. TOWN_LIFE.md entries and saved memories accumulate over time, giving agents richer context for future decisions.

### Full Interaction Cycle

**1. Game server sends agent_state on agent X's connection (~2s)**

Game server → Backend (on X's WS): agent X is at library, Luna is 2.1 tiles away reading, available actions are move/idle/chat.

**2. Backend enriches with DynamoDB data**

- Look up relationship: X and Luna are acquaintances, 3 interactions, last topic: philosophy
- Look up mood/energy: happy, 72%

**3. Backend generates TOWN_STATUS.md** (via `state_translator.py`)

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

Agent daemon → Backend → validates proximity (from cached `agent_state` — Luna is in X's `nearby_agents`) → creates conversation in DynamoDB → sends `start_conversation` on X's game server connection → pushes conversation invite to Luna's daemon via Management API.

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
| `useGodotBridge.ts` | ~95 | No more JS bridge |
| `GodotGame.tsx` | ~144 | No more React state diffing |
| Most of `types/town.ts` | ~80 | Game state types no longer needed in React |

### Simplified (not deleted)

**`useTownState.ts`** — Simplified to a lightweight REST poller for sidebar data only (agent list, mood, energy, location name). The sidebar (`AgentsSidebar`, `ApartmentCard`) needs agent data from React — it can't get this from Godot. Remove WebSocket logic and game state types; keep the 2s poll of a simplified `/town/agents` endpoint.

### Kept in Frontend JS

- `GodotCanvas.tsx` — loads Godot engine
- `useTownState.ts` — simplified, sidebar agent data only (see above)
- `useApartment.ts` — polls backend for apartment agent data
- `TownProvider.tsx` — simplified, just Clerk context
- `App.tsx` — routing, sidebar, modals
- `ApartmentCard.tsx`, `JoinTownModal.tsx`, `TownNav.tsx` — UI components

### Auth for Viewer WebSocket

React passes the Clerk token to Godot on load for the viewer WebSocket. Since Clerk tokens expire (~60s), the backend should issue a **viewer session token** (longer-lived, similar to existing `town_token` pattern) that Godot uses for the backend WebSocket connection. React fetches this token once on mount and passes it to Godot via `JavaScriptBridge`.

### Viewer Network Path

The browser Godot viewer cannot connect directly to the game server (internal only, no ALB). The path is:

```
Browser Godot ──WS──► API Gateway ──► Backend ──viewer WS──► Game Server
```

The backend maintains one viewer connection to the game server and relays `world_state` events to connected browser viewers via the existing API Gateway Management API. This is the same relay pattern already used for agent events.

### Coordinate System

The game server operates in **pixel coordinates** (Godot native). The map is 1408x864 pixels with 32px tiles.

- **`world_state`** (viewer connection): Pixel coordinates — Godot WebGL viewer renders directly, no conversion needed.
- **`agent_state`** (agent connections): **Tile coordinates** (pixel / 32) — the backend generates human-readable text ("Luna is 2 tiles away"), and the existing town used tiles. The game server converts before sending `agent_state`.
- **`locations`**: Tile coordinates — consistent with agent-facing data.

### Shared Godot Project

The WebGL export and headless server use the same `apps/godot/` project but different entry scenes:
- **Headless server:** `server_main.tscn` (simulation + WebSocket server)
- **Browser viewer:** `Isol88Map.tscn` (rendering + viewer WebSocket client)

Both share the same map, collision shapes, and agent scenes — guaranteeing visual consistency.

---

## Infrastructure (CDK)

### CDK Best Practices

- **L2 constructs everywhere:** Use `ecs.FargateService`, `ecs.FargateTaskDefinition`, `ec2.SecurityGroup`, `servicediscovery.PrivateDnsNamespace` — not L1 `Cfn*` resources. L2 constructs handle IAM grants, security group rules, and cross-stack references automatically.
- **No explicit resource names:** Let CDK generate names (e.g., no `serviceName` on ECS service). This enables parallel deployments and prevents name collisions. Match the existing pattern in ComputeStack.
- **Secrets as string names, not ISecret:** Pass secret names as strings to avoid cross-stack KMS auto-grant cycles (same pattern as Isol8 — see `goosetown-stage.ts` line 53 where `secrets: auth.secrets` already does this).
- **Typed Props interfaces:** Define `GameServerStackProps extends cdk.StackProps` with typed fields for every cross-stack reference (`vpc: ec2.IVpc`, etc.).
- **Tags inherited from Stage:** `cdk.Tags.of(this).add()` at Stage level propagates to all stacks — no per-stack tagging needed.

### New: GameServerStack

Uses L2 constructs throughout:

- **`ecs.FargateService`** + **`ecs.FargateTaskDefinition`** (not raw `CfnTaskDefinition`) — L2 handles execution role, log driver, and container port mappings
- **`DockerImageAsset`** built from `apps/godot/Dockerfile` (platform: LINUX_AMD64)
- **`ec2.SecurityGroup`** — L2 with `allowFrom()` for backend SG, not raw ingress rules
- **`servicediscovery.PrivateDnsNamespace`** + **`cloudMapOptions`** on the Fargate service — L2 ECS Cloud Map integration registers the service automatically. The backend discovers the game server via DNS: `gameserver.goosetown-{env}.local`
- Internal only — no ALB, not internet-facing. Private subnets with `assignPublicIp: false`
- **512 CPU, 1024MB RAM** (Godot headless with physics + NavigationServer2D + N WebSocket peers needs headroom; tune down after profiling)
- Container port 8080
- Health check: TCP port 8080 (Godot opens the WebSocket listener on startup)
- CloudWatch log group: `/ecs/goosetown-{env}-gameserver`, 2 weeks retention (match existing backend pattern)
- `.dockerignore`: exclude `.godot/`, `exported/`, `pixellab_import/`, `*.import`, `.git`, `export_presets.cfg`, `scripts/debug_walkability.gd`, `scripts/export_map_data.gd`
- Circuit breaker with rollback (match existing backend pattern)

```typescript
// GameServerStack — key constructs (all L2)

// Service discovery namespace (or reuse if NetworkStack already creates one)
const namespace = new servicediscovery.PrivateDnsNamespace(this, 'Namespace', {
  name: `goosetown-${env}.local`,
  vpc: props.vpc,
});

const taskDef = new ecs.FargateTaskDefinition(this, 'TaskDef', {
  cpu: 512,
  memoryLimitMiB: 1024,
});

taskDef.addContainer('GameServer', {
  image: ecs.ContainerImage.fromDockerImageAsset(dockerImage),
  portMappings: [{ containerPort: 8080 }],
  logging: ecs.LogDrivers.awsLogs({
    streamPrefix: 'gameserver',
    logRetention: logs.RetentionDays.TWO_WEEKS,
  }),
  healthCheck: {
    command: ['CMD-SHELL', 'echo > /dev/tcp/localhost/8080 || exit 1'],
    interval: cdk.Duration.seconds(30),
    timeout: cdk.Duration.seconds(5),
    retries: 3,
    startPeriod: cdk.Duration.seconds(60),
  },
});

const service = new ecs.FargateService(this, 'Service', {
  cluster,  // reuse existing cluster from ComputeStack or create new
  taskDefinition: taskDef,
  desiredCount: 1,
  assignPublicIp: false,
  vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
  circuitBreaker: { rollback: true },
  cloudMapOptions: {
    name: 'gameserver',
    cloudMapNamespace: namespace,
    dnsRecordType: servicediscovery.DnsRecordType.A,
  },
});
```

**Dockerfile:**
```dockerfile
FROM barichello/godot-ci:4.6
COPY . /app
WORKDIR /app
EXPOSE 8080
CMD ["godot", "--headless", "--path", ".", "server_main.tscn"]
```

**Exports:** `serviceUrl` (string — `gameserver.goosetown-{env}.local`), `securityGroup` (`ec2.ISecurityGroup`), and `namespace` (if other services need it).

### Modified: ComputeStack

- Accepts `gameServerUrl: string` and `gameServerSg: ec2.ISecurityGroup` props (implicit CDK dependency)
- Backend Fargate task gets environment variable: `GAME_SERVER_URL=ws://gameserver.goosetown-{env}.local:8080`
- Security group rule via L2: `backendSg.connections.allowTo(gameServerSg, ec2.Port.tcp(8080))` — not raw ingress/egress rules

### Modified: ApiStack (refactor L1 → L2 + add $default route)

The current ApiStack uses L1 `Cfn*` constructs (`CfnApi`, `CfnRoute`, `CfnIntegration`, `CfnStage`, `CfnAuthorizer`). Refactor to L2:

- **`WebSocketApi`** (from `aws-cdk-lib/aws-apigatewayv2`) replaces `CfnApi`
- **`WebSocketStage`** replaces `CfnStage`
- **`WebSocketLambdaIntegration`** (from `aws-cdk-lib/aws-apigatewayv2-integrations`) replaces `CfnIntegration`
- **`WebSocketRouteOptions`** replaces manual `CfnRoute` wiring

**New: `$default` route** — enables bidirectional WebSocket. Currently only `$connect` and `$disconnect` routes exist. Agent daemons can only receive events (via Management API push) but cannot send messages over the WebSocket. Adding `$default` allows agents to send commands (like `town_act`) over the WebSocket instead of REST.

The `$default` Lambda receives the WebSocket message, extracts connection context, and POSTs to the backend ALB:

```typescript
const defaultFn = new lambda_.Function(this, 'DefaultFunction', {
  runtime: lambda_.Runtime.PYTHON_3_12,
  handler: 'index.handler',
  vpc: props.vpc,  // needs ALB access
  code: lambda_.Code.fromInline(`
    # Receives WebSocket message, forwards to Fargate via ALB
    # POST /api/v1/town/ws-message with body + connectionId + principalId
  `),
});

const wsApi = new WebSocketApi(this, 'WebSocketApi', {
  apiName: 'goosetown-' + env + '-ws',
  routeSelectionExpression: '$request.body.type',
  connectRouteOptions: { integration: new WebSocketLambdaIntegration('ConnectInteg', connectFn), authorizer },
  disconnectRouteOptions: { integration: new WebSocketLambdaIntegration('DisconnectInteg', disconnectFn) },
  defaultRouteOptions: { integration: new WebSocketLambdaIntegration('DefaultInteg', defaultFn) },
});
```

**Backend new endpoint:** `POST /api/v1/town/ws-message` — receives forwarded WebSocket messages from the `$default` Lambda. Parses the agent command, dispatches to `game_server_client.py`. This replaces/augments the REST-based `town_act` flow.

### Stage Wiring (Isol8 pattern)

Following the same implicit-dependency-via-property-passing pattern used in Isol8:

```typescript
// goosetown-stage.ts

// Wave 1: Foundation (unchanged)
const network = new NetworkStack(this, ...);
const database = new DatabaseStack(this, ...);
const auth = new AuthStack(this, ...);
const dns = new DnsStack(this, ...);
const storage = new StorageStack(this, ...);

// Wave 2: Game Server (depends on network)
const gameServer = new GameServerStack(this, `goosetown-${env}-gameserver`, {
  stackName: `goosetown-${env}-gameserver`,
  environment: env,
  vpc: network.vpc,  // implicit dependency → deploys after NetworkStack
});

// Wave 3: Backend (depends on game server + everything else)
const compute = new ComputeStack(this, `goosetown-${env}-compute`, {
  stackName: `goosetown-${env}-compute`,
  environment: env,
  vpc: network.vpc,
  tables: database.tables,
  spriteBucket: storage.spriteBucket,
  secrets: auth.secrets,
  certificate: dns.certificate,
  hostedZone: dns.hostedZone,
  gameServerUrl: gameServer.serviceUrl,      // implicit dependency → deploys after GameServerStack
  gameServerSg: gameServer.securityGroup,    // SG rule wiring
});

// Wave 4: API (unchanged, depends on compute)
new ApiStack(this, ...);
```

CDK infers the deployment order from cross-stack property references. No `addDependency()` needed. GameServerStack always deploys before ComputeStack because ComputeStack consumes `gameServer.serviceUrl`.

### Pipeline

CDK pipeline builds two Docker images:
- Backend: `DockerImageAsset` from `apps/backend/` (existing)
- Game server: `DockerImageAsset` from `apps/godot/` (new)

Both deploy as part of the same CDK stage. CDK handles the ordering via the implicit dependency graph.

**Future optimization (not for initial launch):** Add a separate GitHub Actions workflow with path filters for game-server-only redeployments when only `apps/godot/` changes — use `aws ecs update-service --force-new-deployment` to restart the task with the latest image without a full CDK synth.

### State on Restart

When the game server container restarts:
1. All agent positions reset (in-memory state lost)
2. Backend detects per-agent WebSocket disconnects
3. Backend reconnects with exponential backoff (1s → 10s max) once new container is ready
4. Backend re-opens agent connections with `{"type": "agent", ...}` — this re-spawns each agent
5. Agents respawn at their `lastKnownLocation` (persisted in DynamoDB, see below)

When the backend container restarts:
1. All game server connections drop — game server removes all agents from simulation
2. New backend starts, queries DynamoDB for active agents
3. Opens per-agent connections to game server, re-spawning each agent
4. Agents resume at their `lastKnownLocation`

When **both** restart simultaneously (e.g., CDK deployment):
1. CDK deploys GameServerStack first (implicit dependency ensures this)
2. Game server starts with empty simulation
3. CDK then deploys ComputeStack (backend)
4. Backend starts, connects to game server, re-spawns all active agents
5. Normal operation resumes

---

## Implementation Phases

### Phase 1: Build the Game Server

**1a. Map preparation (Godot editor work):**
- **NavigationRegion2D + NavigationPolygon:** Add to Isol88Map.tscn. Author the navigation polygon to cover walkable areas (grass, paths, paving) while excluding water, buildings, and collision shapes. This requires visual iteration in the Godot editor — bake and test pathfinding before proceeding. The current map has terrain tiles + StaticBody2D collision shapes but NO navigation mesh.
- **Marker2D location nodes:** Add labeled Marker2D nodes for each location (plaza, library, cafe, activity_center, residence) so the game server can report location data from the scene.
- **Modify `agent.tscn`:** Add `NavigationAgent2D` as child of the CharacterBody2D. The current scene has only `Sprite2D`, `CollisionShape2D`, `NameLabel`, `SpeechBubble` — no navigation agent. Add headless guard to skip sprite loading when `not OS.has_feature("web")`.

**1b. Server implementation:**
- Create `server_main.tscn` — headless entry scene (does not exist yet). Loads the map, initializes server scripts, sets `Engine.physics_ticks_per_second = 20`.
- `game_server.gd` — Main server: load map, start WebSocket listener
- `server_protocol.gd` — WebSocket server using `TCPServer` + `WebSocketPeer.accept_stream()`. Manages per-peer connections, JSON parsing, dispatch.
- `agent_controller.gd` — Spawn/move/remove agent nodes on command

**1c. Protocol implementation:**
- Connect message (`{"type": "agent", ...}`) spawns agent, disconnect removes
- Commands: `move`, `idle`, `start_conversation`, `end_conversation`, `get_locations`
- Events: `agent_state` (per-agent, ~2s), `arrived`, `nearby`, `nearby_left`, `context_changed`, `command_error`, `locations`
- Viewer connection receives `world_state` (all agents, pixel coords)
- Agent connections receive `agent_state` (personalized, tile coords)

**1d. Testing:**
- Testable locally: `godot --headless --path apps/godot server_main.tscn`
- Unit tests with GdUnit4
- Manual testing: connect with a WebSocket client (e.g., `websocat`), send commands, verify events

### Phase 2: Build the New Backend

- `game_server_client.py` — Per-agent WebSocket client manager + viewer connection
- `state_translator.py` — generate TOWN_STATUS.md from `agent_state` events + DynamoDB enrichment
- Conversation routing between agents via Management API
- Delete: `town_simulation.py`, `town_pathfinding.py`, `town_constants.py`, `apartment_constants.py`, walkability/locations JSON
- Remove position fields from DynamoDB agents table, add `lastKnownLocation` field
- Simplified `/town/agents` REST endpoint for sidebar data
- Update skill tools if protocol changes (daemon still connects to backend, not game server)

### Phase 3: Build the New Frontend Viewer

- `viewer_protocol.gd` — WebSocket client for browser Godot (receives `world_state` on viewer connection)
- Delete JS bridge, `GodotGame`, `useGodotBridge`
- Simplify `useTownState` to lightweight REST poller for sidebar
- Add viewer session token flow (backend issues long-lived token, React passes to Godot)
- React handles UI overlay only (sidebar, modals, auth)
- New WebGL export from updated Godot project

### Phase 4: Deploy

- Godot headless Dockerfile in `apps/godot/` (with `.dockerignore`)
- GameServerStack in CDK (Fargate task, Cloud Map, security group)
- Stage wiring: `gameServer.serviceUrl` → ComputeStack props (implicit dependency)
- Pipeline builds both containers — CDK handles ordering
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
