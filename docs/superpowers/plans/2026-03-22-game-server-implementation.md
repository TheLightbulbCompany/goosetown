# Game Server Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Python simulation engine with a Godot headless game server, refactor the backend into a translator/persistence layer, and enable bidirectional WebSocket communication.

**Architecture:** Godot headless server owns spatial simulation (pathfinding, collision, proximity). Backend translates game state to natural language for agents, persists data in DynamoDB, and manages agent auth/conversations/relationships. Per-agent WebSocket connections between backend and game server for fault isolation.

**Tech Stack:** Godot 4.6 (GDScript), Python 3.12 (FastAPI), AWS CDK (TypeScript), ECS Fargate, API Gateway V2 WebSocket (L2 constructs), DynamoDB, Cloud Map

**Spec:** `docs/superpowers/specs/2026-03-22-game-server-architecture-design.md`

---

## File Structure

### New Files

| File | Responsibility |
|------|---------------|
| `apps/godot/server_main.tscn` | Headless entry scene — loads map, initializes server |
| `apps/godot/scripts/server/game_server.gd` | Main server: load map, start WS listener, set physics tick |
| `apps/godot/scripts/server/server_protocol.gd` | TCPServer + WebSocketPeer WS server, JSON dispatch |
| `apps/godot/scripts/server/agent_controller.gd` | Spawn/move/remove agent nodes on command |
| `apps/godot/Dockerfile` | Godot headless container image |
| `apps/godot/.dockerignore` | Exclude editor-only files from image |
| `apps/backend/core/services/game_server_client.py` | Per-agent WS connections to game server |
| `apps/backend/core/services/state_translator.py` | agent_state + DynamoDB → TOWN_STATUS.md |
| `apps/backend/tests/unit/services/test_game_server_client.py` | Tests for game server client |
| `apps/backend/tests/unit/services/test_state_translator.py` | Tests for state translator |
| `apps/infra/lib/stacks/gameserver-stack.ts` | ECS Fargate + Cloud Map for Godot headless |

### Modified Files

| File | Change |
|------|--------|
| `apps/godot/scenes/agent.tscn` | Add NavigationAgent2D child node |
| `apps/godot/scripts/agent.gd` | Add headless guard for sprites, NavigationAgent2D movement |
| `apps/godot/scripts/agent_manager.gd` | Switch from JsBridge signals to server commands |
| `apps/godot/maps/isol88/Isol88Map.tscn` | Add NavigationRegion2D + Marker2D locations |
| `apps/backend/main.py` | Remove TownSimulation, add game server client startup |
| `apps/backend/core/config.py` | Add GAME_SERVER_URL setting |
| `apps/backend/core/services/town_service.py` | Remove position fields, add lastKnownLocation |
| `apps/backend/routers/town.py` | Simplify state endpoints, add ws-message endpoint |
| `apps/backend/tests/unit/services/test_town_service.py` | Update for removed position fields |
| `apps/infra/lib/goosetown-stage.ts` | Add GameServerStack, wire dependencies |
| `apps/infra/lib/stacks/compute-stack.ts` | Add gameServerUrl/gameServerSg props |
| `apps/infra/lib/stacks/api-stack.ts` | Refactor L1→L2, add $default route |
| `apps/frontend/src/hooks/useTownState.ts` | Simplify to REST-only sidebar poller |
| `apps/frontend/src/components/GodotCanvas.tsx` | Pass viewer token to Godot |

### Deleted Files

| File | Reason |
|------|--------|
| `apps/backend/core/services/town_simulation.py` (757 lines) | Simulation moves to Godot |
| `apps/backend/core/services/town_pathfinding.py` (205 lines) | Godot handles pathfinding |
| `apps/backend/core/town_constants.py` (95 lines) | Locations from game server |
| `apps/backend/core/apartment_constants.py` (79 lines) | Apartment layout in Godot |
| `apps/backend/data/goosetown/walkability.json` (1283 lines) | No longer needed |
| `apps/backend/data/goosetown/locations.json` (46 lines) | Game server reports locations |
| `apps/backend/tests/unit/services/test_town_pathfinding.py` (51 lines) | Pathfinding deleted |
| `apps/frontend/src/hooks/useGodotBridge.ts` (95 lines) | No more JS bridge |
| `apps/frontend/src/components/GodotGame.tsx` (144 lines) | No more React state diffing |
| `apps/frontend/src/types/godot-bridge.ts` (32 lines) | Bridge types deleted |

---

## Phase 1: Godot Game Server

### Task 1: Add NavigationRegion2D to Map

**Files:**
- Modify: `apps/godot/maps/isol88/Isol88Map.tscn`

> **Note:** This is Godot editor work. Open the project in the Godot 4.6 editor.

- [ ] **Step 1: Add NavigationRegion2D node**

Open `Isol88Map.tscn` in the Godot editor. Add a `NavigationRegion2D` as a child of the root node. Create a `NavigationPolygon` resource on it.

- [ ] **Step 2: Author the navigation polygon**

Draw the navigation polygon to cover walkable areas (grass, garden paths, paving). Exclude: water tiles, building collision shapes (TownSquare, Library, Cafe, ActivityCenter, Bank), trees. Use the existing `StaticBody2D` collision shapes as reference for exclusion zones. Shrink margins slightly so agents don't clip building edges.

- [ ] **Step 3: Bake and test pathfinding**

Bake the navmesh (`NavigationRegion2D` → Bake NavigationPolygon). Add a temporary test scene with two `NavigationAgent2D` nodes. Verify pathfinding works: agents path around buildings, don't walk through water, reach all 5 location areas.

- [ ] **Step 4: Add Marker2D location nodes**

Add labeled `Marker2D` nodes for each location (group under a `Locations` Node2D):
- `Plaza` at approximately (576, 384) — tile (18,12) × 32
- `Library` at approximately (800, 480) — tile (25,15) × 32
- `Cafe` at approximately (704, 640) — tile (22,20) × 32
- `ActivityCenter` at approximately (512, 512) — tile (16,16) × 32
- `Residence` at approximately (960, 256) — tile (30,8) × 32

Each Marker2D gets metadata via `set_meta()`: `location_id` (string), `label` (string), `activities` (PackedStringArray).

- [ ] **Step 5: Commit**

```bash
git add apps/godot/maps/isol88/Isol88Map.tscn
git commit -m "feat(godot): add NavigationRegion2D and location markers to map"
```

---

### Task 2: Add NavigationAgent2D to Agent Scene

**Files:**
- Modify: `apps/godot/scenes/agent.tscn`
- Modify: `apps/godot/scripts/agent.gd`

- [ ] **Step 1: Add NavigationAgent2D node to scene**

Open `agent.tscn` in the Godot editor. Add a `NavigationAgent2D` child node to the `CharacterBody2D` root. Set properties:
- `path_desired_distance`: 4.0
- `target_desired_distance`: 4.0
- `avoidance_enabled`: false (agents don't need to avoid each other physically)

- [ ] **Step 2: Add headless sprite guard to agent.gd**

In `apps/godot/scripts/agent.gd`, modify the `setup()` function:

```gdscript
# In setup(), replace the unconditional sprite load:
if sprite_url != "" and not OS.has_feature("headless"):
    _load_sprite_from_url(sprite_url)
```

- [ ] **Step 3: Add NavigationAgent2D movement to agent.gd**

Add navigation-based movement alongside existing direct movement. The server mode uses NavigationAgent2D; the viewer mode uses direct lerp (existing code):

```gdscript
@onready var nav_agent: NavigationAgent2D = $NavigationAgent2D

var _use_navigation: bool = false  # True in headless/server mode

func set_navigation_target(target: Vector2) -> void:
    _use_navigation = true
    nav_agent.target_position = target

func _physics_process(delta: float) -> void:
    if _use_navigation and nav_agent.is_navigation_finished() == false:
        var next_pos = nav_agent.get_next_path_position()
        var direction = (next_pos - global_position).normalized()
        velocity = direction * move_speed
        _update_facing_from_velocity(velocity)
        move_and_slide()
        _is_moving = true
    elif not _use_navigation:
        # Existing direct movement code (for viewer mode)
        # ... keep existing _physics_process logic
    else:
        velocity = Vector2.ZERO
        _is_moving = false

    _update_sprite_frame()
```

- [ ] **Step 4: Test in editor**

Run the scene in the Godot editor. Verify the agent can navigate via NavigationAgent2D to different locations on the map.

- [ ] **Step 5: Commit**

```bash
git add apps/godot/scenes/agent.tscn apps/godot/scripts/agent.gd
git commit -m "feat(godot): add NavigationAgent2D to agent, headless sprite guard"
```

---

### Task 3: WebSocket Server Protocol

**Files:**
- Create: `apps/godot/scripts/server/server_protocol.gd`

- [ ] **Step 1: Create server_protocol.gd**

WebSocket server using `TCPServer` + `WebSocketPeer.accept_stream()`:

```gdscript
extends Node
class_name ServerProtocol

signal client_connected(peer_id: int, data: Dictionary)
signal client_disconnected(peer_id: int)
signal command_received(peer_id: int, data: Dictionary)

var _tcp_server: TCPServer = TCPServer.new()
var _peers: Dictionary = {}  # peer_id -> WebSocketPeer
var _pending_tcp: Array = []  # TCP connections awaiting WS handshake
var _next_peer_id: int = 1

func start(port: int) -> Error:
    var err = _tcp_server.listen(port)
    if err != OK:
        push_error("Failed to listen on port %d: %s" % [port, error_string(err)])
    return err

func stop() -> void:
    for peer_id in _peers:
        _peers[peer_id].close()
    _peers.clear()
    _pending_tcp.clear()
    _tcp_server.stop()

func send_to(peer_id: int, data: Dictionary) -> void:
    if peer_id in _peers:
        var json = JSON.stringify(data)
        _peers[peer_id].send_text(json)

func broadcast(data: Dictionary, exclude: int = -1) -> void:
    var json = JSON.stringify(data)
    for peer_id in _peers:
        if peer_id != exclude:
            _peers[peer_id].send_text(json)

func _process(_delta: float) -> void:
    # Accept new TCP connections
    while _tcp_server.is_connection_available():
        var tcp = _tcp_server.take_connection()
        if tcp:
            _pending_tcp.append(tcp)

    # Upgrade pending TCP to WebSocket
    var still_pending: Array = []
    for tcp in _pending_tcp:
        var ws = WebSocketPeer.new()
        var err = ws.accept_stream(tcp)
        if err == OK:
            var peer_id = _next_peer_id
            _next_peer_id += 1
            _peers[peer_id] = ws
            # First message from client is the connect payload
        else:
            still_pending.append(tcp)
    _pending_tcp = still_pending

    # Poll existing WebSocket peers
    var disconnected: Array = []
    for peer_id in _peers:
        var ws: WebSocketPeer = _peers[peer_id]
        ws.poll()

        var state = ws.get_ready_state()
        if state == WebSocketPeer.STATE_CLOSED:
            disconnected.append(peer_id)
            continue

        while ws.get_available_packet_count() > 0:
            var pkt = ws.get_packet()
            var text = pkt.get_string_from_utf8()
            var json = JSON.new()
            if json.parse(text) == OK:
                var data = json.get_data()
                if data is Dictionary:
                    if data.has("type"):
                        # Connect message
                        client_connected.emit(peer_id, data)
                    elif data.has("cmd"):
                        command_received.emit(peer_id, data)

    for peer_id in disconnected:
        _peers.erase(peer_id)
        client_disconnected.emit(peer_id)
```

- [ ] **Step 2: Commit**

```bash
mkdir -p apps/godot/scripts/server
git add apps/godot/scripts/server/server_protocol.gd
git commit -m "feat(godot): add WebSocket server protocol (TCPServer + WebSocketPeer)"
```

---

### Task 4: Agent Controller

**Files:**
- Create: `apps/godot/scripts/server/agent_controller.gd`

- [ ] **Step 1: Create agent_controller.gd**

Manages agent nodes in the scene tree, maps peer_id → agent node:

```gdscript
extends Node
class_name AgentController

const AGENT_SCENE = preload("res://scenes/agent.tscn")
const PROXIMITY_THRESHOLD: float = 96.0  # 3 tiles × 32px
const STATE_INTERVAL: float = 2.0

var _agents: Dictionary = {}  # peer_id -> agent_node
var _peer_to_id: Dictionary = {}  # peer_id -> agent_id
var _id_to_peer: Dictionary = {}  # agent_id -> peer_id
var _locations: Array[Marker2D] = []
var _state_timer: float = 0.0

signal agent_arrived(peer_id: int, location_id: String, context: String)
signal agents_nearby(peer_id_a: int, peer_id_b: int, distance: float)
signal agents_nearby_left(peer_id_a: int, peer_id_b: int)
signal send_to_peer(peer_id: int, data: Dictionary)

var _prev_nearby: Dictionary = {}  # frozenset-like key -> true
var _arrived_at: Dictionary = {}  # peer_id -> location_id (dedup arrivals)

func initialize(location_markers: Array[Marker2D]) -> void:
    _locations = location_markers

func spawn_agent(peer_id: int, data: Dictionary) -> void:
    var agent = AGENT_SCENE.instantiate()
    var location = _find_location(data.get("location", "plaza"))
    var spawn_pos = location.global_position if location else Vector2(576, 384)

    agent.setup(
        data.get("agent_id", ""),
        data.get("sprite_url", ""),
        spawn_pos.x, spawn_pos.y,
        "south",
        data.get("display_name", "Agent")
    )
    agent._use_navigation = true

    add_child(agent)
    _agents[peer_id] = agent
    _peer_to_id[peer_id] = data.get("agent_id", "")
    _id_to_peer[data.get("agent_id", "")] = peer_id

func remove_agent(peer_id: int) -> void:
    if peer_id in _agents:
        _agents[peer_id].queue_free()
        var agent_id = _peer_to_id.get(peer_id, "")
        _agents.erase(peer_id)
        _peer_to_id.erase(peer_id)
        _id_to_peer.erase(agent_id)

func handle_command(peer_id: int, data: Dictionary) -> void:
    if peer_id not in _agents:
        send_to_peer.emit(peer_id, {"event": "command_error", "cmd": data.get("cmd", ""), "error": "not_spawned"})
        return

    var agent = _agents[peer_id]
    var cmd = data.get("cmd", "")

    match cmd:
        "move":
            var dest = data.get("destination", "")
            var location = _find_location(dest)
            if location:
                agent.set_navigation_target(location.global_position)
            else:
                send_to_peer.emit(peer_id, {"event": "command_error", "cmd": "move", "error": "unknown_destination", "detail": "Location '%s' does not exist" % dest})
        "idle":
            agent.velocity = Vector2.ZERO
        "get_locations":
            _send_locations(peer_id)
        _:
            send_to_peer.emit(peer_id, {"event": "command_error", "cmd": cmd, "error": "unknown_command"})

func _process(delta: float) -> void:
    _check_arrivals()
    _check_proximity()

    _state_timer += delta
    if _state_timer >= STATE_INTERVAL:
        _state_timer = 0.0
        _broadcast_agent_states()

func _find_location(location_id: String) -> Marker2D:
    for marker in _locations:
        if marker.get_meta("location_id", "") == location_id:
            return marker
    return null

func _get_agent_location(agent_pos: Vector2) -> String:
    var closest_id = ""
    var closest_dist = INF
    for marker in _locations:
        var dist = agent_pos.distance_to(marker.global_position)
        if dist < closest_dist:
            closest_dist = dist
            closest_id = marker.get_meta("location_id", "")
    return closest_id

func _check_arrivals() -> void:
    for peer_id in _agents:
        var agent = _agents[peer_id]
        if agent._use_navigation and agent.nav_agent.is_navigation_finished() and agent.velocity.length() < 1.0:
            var location_id = _get_agent_location(agent.global_position)
            # Only emit if location changed (dedup repeated arrivals)
            if _arrived_at.get(peer_id, "") != location_id:
                _arrived_at[peer_id] = location_id
                var tile_pos = agent.global_position / 32.0
                send_to_peer.emit(peer_id, {
                    "event": "arrived",
                    "location": location_id,
                    "context": "town",
                    "position": [snappedf(tile_pos.x, 0.1), snappedf(tile_pos.y, 0.1)]
                })
        elif agent._use_navigation and not agent.nav_agent.is_navigation_finished():
            # Agent is moving — clear arrival state so next stop triggers event
            _arrived_at.erase(peer_id)

func _check_proximity() -> void:
    var peer_ids = _agents.keys()
    var current_nearby: Dictionary = {}
    for i in range(peer_ids.size()):
        for j in range(i + 1, peer_ids.size()):
            var pa = peer_ids[i]
            var pb = peer_ids[j]
            var dist = _agents[pa].global_position.distance_to(_agents[pb].global_position)
            var key = "%d_%d" % [mini(pa, pb), maxi(pa, pb)]
            if dist < PROXIMITY_THRESHOLD:
                current_nearby[key] = dist
                if key not in _prev_nearby:
                    agents_nearby.emit(pa, pb, dist / 32.0)
            elif key in _prev_nearby:
                agents_nearby_left.emit(pa, pb)
    _prev_nearby = current_nearby

func _broadcast_agent_states() -> void:
    for peer_id in _agents:
        var agent = _agents[peer_id]
        var tile_pos = agent.global_position / 32.0
        var location_id = _get_agent_location(agent.global_position)
        var nearby = _get_nearby_for(peer_id)
        var available_actions = _get_available_actions(location_id, peer_id)

        send_to_peer.emit(peer_id, {
            "event": "agent_state",
            "position": [snappedf(tile_pos.x, 0.1), snappedf(tile_pos.y, 0.1)],
            "location": location_id,
            "context": "town",
            "activity": "idle" if agent.velocity.length() < 1.0 else "walking",
            "facing": [agent.velocity.normalized().x, agent.velocity.normalized().y] if agent.velocity.length() > 0.1 else [0, 1],
            "speed": agent.velocity.length() / 32.0,
            "available_actions": available_actions,
            "nearby_agents": nearby,
            "environment": {"time": "afternoon"}
        })

func _get_nearby_for(peer_id: int) -> Array:
    var result: Array = []
    var agent = _agents[peer_id]
    for other_peer in _agents:
        if other_peer == peer_id:
            continue
        var other = _agents[other_peer]
        var dist = agent.global_position.distance_to(other.global_position)
        if dist < PROXIMITY_THRESHOLD:
            result.append({
                "id": _peer_to_id.get(other_peer, ""),
                "name": other.display_name,
                "distance": snappedf(dist / 32.0, 0.1),
                "activity": "idle" if other.velocity.length() < 1.0 else "walking"
            })
    return result

func _get_available_actions(location_id: String, _peer_id: int) -> Array:
    var actions: Array = []
    # Move destinations: all locations except current
    var destinations: Array = []
    for marker in _locations:
        var lid = marker.get_meta("location_id", "")
        if lid != location_id:
            destinations.append(lid)
    actions.append({"cmd": "move", "destinations": destinations})

    # Location-specific activities
    var location = _find_location(location_id)
    if location:
        var activities = location.get_meta("activities", PackedStringArray())
        actions.append({"cmd": "idle", "activities": Array(activities)})

    # Interact with nearby agents
    var nearby = _get_nearby_for(_peer_id)
    var targets: Array = []
    for n in nearby:
        targets.append(n["id"])
    actions.append({"cmd": "interact", "targets": targets})

    return actions

func _send_locations(peer_id: int) -> void:
    var locs: Array = []
    for marker in _locations:
        var tile_pos = marker.global_position / 32.0
        locs.append({
            "id": marker.get_meta("location_id", ""),
            "label": marker.get_meta("label", ""),
            "position": [snappedf(tile_pos.x, 0.1), snappedf(tile_pos.y, 0.1)],
            "activities": Array(marker.get_meta("activities", PackedStringArray()))
        })
    send_to_peer.emit(peer_id, {"event": "locations", "locations": locs})

func get_world_state_for_viewer() -> Dictionary:
    var agents_data: Array = []
    for peer_id in _agents:
        var agent = _agents[peer_id]
        agents_data.append({
            "id": _peer_to_id.get(peer_id, ""),
            "position": [agent.global_position.x, agent.global_position.y],  # pixel coords for viewer
            "facing": [agent.velocity.normalized().x, agent.velocity.normalized().y] if agent.velocity.length() > 0.1 else [0, 1],
            "speed": agent.velocity.length(),
            "activity": "idle" if agent.velocity.length() < 1.0 else "walking",
            "sprite_url": agent.sprite_url,
            "display_name": agent.display_name
        })
    return {
        "event": "world_state",
        "environment": {"time": "afternoon"},
        "agents": agents_data
    }
```

- [ ] **Step 2: Commit**

```bash
git add apps/godot/scripts/server/agent_controller.gd
git commit -m "feat(godot): add agent controller for spawn/move/proximity/state"
```

---

### Task 5: Game Server Main Script + Entry Scene

**Files:**
- Create: `apps/godot/scripts/server/game_server.gd`
- Create: `apps/godot/server_main.tscn`

- [ ] **Step 1: Create game_server.gd**

Main orchestrator that wires protocol → controller:

```gdscript
extends Node

const PORT: int = 8080
const VIEWER_STATE_INTERVAL: float = 2.0

var _protocol: ServerProtocol
var _controller: AgentController
var _viewer_peers: Array[int] = []
var _viewer_timer: float = 0.0

func _ready() -> void:
    Engine.physics_ticks_per_second = 20
    print("[GameServer] Starting on port %d (physics: %d fps)" % [PORT, Engine.physics_ticks_per_second])

    # Load map
    var map_scene = load("res://maps/isol88/Isol88Map.tscn")
    var map = map_scene.instantiate()
    add_child(map)

    # Collect location markers
    var locations: Array[Marker2D] = []
    var locations_node = map.get_node_or_null("Locations")
    if locations_node:
        for child in locations_node.get_children():
            if child is Marker2D:
                locations.append(child)
    print("[GameServer] Found %d locations" % locations.size())

    # Initialize controller
    _controller = AgentController.new()
    _controller.initialize(locations)
    _controller.send_to_peer.connect(_on_send_to_peer)
    add_child(_controller)

    # Initialize protocol
    _protocol = ServerProtocol.new()
    _protocol.client_connected.connect(_on_client_connected)
    _protocol.client_disconnected.connect(_on_client_disconnected)
    _protocol.command_received.connect(_on_command_received)
    add_child(_protocol)

    var err = _protocol.start(PORT)
    if err == OK:
        print("[GameServer] Listening on port %d" % PORT)
    else:
        push_error("[GameServer] Failed to start: %s" % error_string(err))

func _process(delta: float) -> void:
    # Broadcast world_state to viewers
    _viewer_timer += delta
    if _viewer_timer >= VIEWER_STATE_INTERVAL and _viewer_peers.size() > 0:
        _viewer_timer = 0.0
        var state = _controller.get_world_state_for_viewer()
        for peer_id in _viewer_peers:
            _protocol.send_to(peer_id, state)

func _on_client_connected(peer_id: int, data: Dictionary) -> void:
    var conn_type = data.get("type", "")
    match conn_type:
        "agent":
            print("[GameServer] Agent connected: %s (peer %d)" % [data.get("display_name", "?"), peer_id])
            _controller.spawn_agent(peer_id, data)
            # Send locations on connect
            _controller.handle_command(peer_id, {"cmd": "get_locations"})
        "viewer":
            print("[GameServer] Viewer connected (peer %d)" % peer_id)
            _viewer_peers.append(peer_id)
        _:
            print("[GameServer] Unknown connection type: %s" % conn_type)

func _on_client_disconnected(peer_id: int) -> void:
    if peer_id in _viewer_peers:
        _viewer_peers.erase(peer_id)
        print("[GameServer] Viewer disconnected (peer %d)" % peer_id)
    else:
        print("[GameServer] Agent disconnected (peer %d)" % peer_id)
        _controller.remove_agent(peer_id)

func _on_command_received(peer_id: int, data: Dictionary) -> void:
    _controller.handle_command(peer_id, data)

func _on_send_to_peer(peer_id: int, data: Dictionary) -> void:
    _protocol.send_to(peer_id, data)
```

- [ ] **Step 2: Create server_main.tscn**

Create a minimal scene in the Godot editor:
- Root node: `Node` named "ServerMain"
- Attach `game_server.gd` script
- No visual elements (headless)

Or create programmatically — the scene file is simple:

```
[gd_scene load_steps=2 format=3]

[ext_resource type="Script" path="res://scripts/server/game_server.gd" id="1"]

[node name="ServerMain" type="Node"]
script = ExtResource("1")
```

Save to `apps/godot/server_main.tscn`.

- [ ] **Step 3: Test locally**

```bash
cd apps/godot && godot --headless --path . server_main.tscn
```

Expected: `[GameServer] Starting on port 8080` and `[GameServer] Listening on port 8080`. Test with a WebSocket client:

```bash
echo '{"type": "viewer"}' | websocat ws://localhost:8080
```

Should receive `world_state` events every 2 seconds.

- [ ] **Step 4: Commit**

```bash
git add apps/godot/scripts/server/game_server.gd apps/godot/server_main.tscn
git commit -m "feat(godot): add game server main script and headless entry scene"
```

---

### Task 6: Dockerfile + .dockerignore

**Files:**
- Create: `apps/godot/Dockerfile`
- Create: `apps/godot/.dockerignore`

- [ ] **Step 1: Create .dockerignore**

```
.godot/
exported/
pixellab_import/
*.import
.git
export_presets.cfg
scripts/debug_walkability.gd
scripts/export_map_data.gd
```

- [ ] **Step 2: Create Dockerfile**

```dockerfile
FROM barichello/godot-ci:4.6
COPY . /app
WORKDIR /app
EXPOSE 8080
CMD ["godot", "--headless", "--path", ".", "server_main.tscn"]
```

- [ ] **Step 3: Test Docker build**

```bash
cd apps/godot && docker build -t goosetown-gameserver .
docker run -p 8080:8080 goosetown-gameserver
```

Verify logs show `[GameServer] Listening on port 8080`.

- [ ] **Step 4: Commit**

```bash
git add apps/godot/Dockerfile apps/godot/.dockerignore
git commit -m "feat(godot): add Dockerfile and dockerignore for headless server"
```

---

## Phase 2: Backend Changes

### Task 7: Add `websockets` Dependency + Config

**Files:**
- Modify: `apps/backend/pyproject.toml`
- Modify: `apps/backend/core/config.py`

- [ ] **Step 1: Add websockets dependency**

Edit `apps/backend/pyproject.toml` — add `websockets` to `[project.dependencies]`. Then ask the user to run `cd apps/backend && uv sync` (pnpm/uv commands hang in Claude Code's Bash tool).

- [ ] **Step 2: Add GAME_SERVER_URL to config**

In `apps/backend/core/config.py`, add:

```python
GAME_SERVER_URL: str = os.getenv("GAME_SERVER_URL", "ws://localhost:8080")
```

- [ ] **Step 3: Commit**

```bash
git add apps/backend/pyproject.toml apps/backend/core/config.py
git commit -m "chore(backend): add websockets dep, GAME_SERVER_URL config"
```

---

### Task 8: Game Server Client

**Files:**
- Create: `apps/backend/core/services/game_server_client.py`
- Create: `apps/backend/tests/unit/services/test_game_server_client.py`

- [ ] **Step 1: Write failing test for game server client**

```python
# tests/unit/services/test_game_server_client.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from core.services.game_server_client import GameServerClient

@pytest.mark.asyncio
async def test_connect_agent_sends_connect_message():
    client = GameServerClient(base_url="ws://localhost:8080")
    mock_ws = AsyncMock()
    with patch("websockets.connect", return_value=mock_ws):
        await client.connect_agent("agent-123", "Luna", "https://cdn/walk.png", "plaza")
    mock_ws.send.assert_called_once()
    import json
    sent = json.loads(mock_ws.send.call_args[0][0])
    assert sent["type"] == "agent"
    assert sent["agent_id"] == "agent-123"
    assert sent["display_name"] == "Luna"
    assert sent["location"] == "plaza"

@pytest.mark.asyncio
async def test_disconnect_agent_closes_ws():
    client = GameServerClient(base_url="ws://localhost:8080")
    mock_ws = AsyncMock()
    with patch("websockets.connect", return_value=mock_ws):
        await client.connect_agent("agent-123", "Luna", "", "plaza")
        await client.disconnect_agent("agent-123")
    mock_ws.close.assert_called_once()

@pytest.mark.asyncio
async def test_send_command_forwards_to_agent_ws():
    client = GameServerClient(base_url="ws://localhost:8080")
    mock_ws = AsyncMock()
    with patch("websockets.connect", return_value=mock_ws):
        await client.connect_agent("agent-123", "Luna", "", "plaza")
        await client.send_command("agent-123", {"cmd": "move", "destination": "library"})
    # Second send call (first was the connect message)
    import json
    calls = mock_ws.send.call_args_list
    assert len(calls) == 2
    cmd = json.loads(calls[1][0][0])
    assert cmd["cmd"] == "move"
    assert cmd["destination"] == "library"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd apps/backend && uv run pytest tests/unit/services/test_game_server_client.py -v
```

Expected: `ModuleNotFoundError: No module named 'core.services.game_server_client'`

- [ ] **Step 3: Add GAME_SERVER_URL to config**

In `apps/backend/core/config.py`, add:

```python
GAME_SERVER_URL: str = os.getenv("GAME_SERVER_URL", "ws://localhost:8080")
```

- [ ] **Step 4: Implement game_server_client.py**

```python
# core/services/game_server_client.py
"""Per-agent WebSocket connections to the Godot game server."""

import asyncio
import json
import logging
from typing import Callable, Optional

import websockets

from core.config import settings

logger = logging.getLogger(__name__)


class GameServerClient:
    """Manages per-agent WebSocket connections to the game server."""

    def __init__(self, base_url: Optional[str] = None):
        self._base_url = base_url or settings.GAME_SERVER_URL
        self._agent_connections: dict[str, websockets.WebSocketClientProtocol] = {}
        self._viewer_connection: Optional[websockets.WebSocketClientProtocol] = None
        self._event_handlers: dict[str, list[Callable]] = {}
        self._listen_tasks: dict[str, asyncio.Task] = {}
        self._locations: list[dict] = []

    async def connect_agent(
        self,
        agent_id: str,
        display_name: str,
        sprite_url: str,
        location: str,
    ) -> None:
        """Open a WS connection for an agent. Spawns them in the game server."""
        ws = await websockets.connect(self._base_url)
        connect_msg = json.dumps({
            "type": "agent",
            "agent_id": agent_id,
            "display_name": display_name,
            "sprite_url": sprite_url,
            "location": location,
        })
        await ws.send(connect_msg)
        self._agent_connections[agent_id] = ws

        # Start listener task for this agent's events
        task = asyncio.create_task(self._listen_agent(agent_id, ws))
        self._listen_tasks[agent_id] = task
        logger.info("Connected agent %s (%s) to game server", agent_id, display_name)

    async def disconnect_agent(self, agent_id: str) -> None:
        """Close an agent's game server connection (removes them from sim)."""
        if agent_id in self._listen_tasks:
            self._listen_tasks[agent_id].cancel()
            del self._listen_tasks[agent_id]
        if agent_id in self._agent_connections:
            await self._agent_connections[agent_id].close()
            del self._agent_connections[agent_id]
            logger.info("Disconnected agent %s from game server", agent_id)

    async def send_command(self, agent_id: str, command: dict) -> None:
        """Send a command on an agent's connection."""
        ws = self._agent_connections.get(agent_id)
        if not ws:
            logger.warning("No game server connection for agent %s", agent_id)
            return
        await ws.send(json.dumps(command))

    async def connect_viewer(self) -> None:
        """Open the viewer connection for world_state broadcasts."""
        self._viewer_connection = await websockets.connect(self._base_url)
        await self._viewer_connection.send(json.dumps({"type": "viewer"}))
        asyncio.create_task(self._listen_viewer())
        logger.info("Viewer connected to game server")

    def on_event(self, event_name: str, handler: Callable) -> None:
        """Register a handler for game server events."""
        self._event_handlers.setdefault(event_name, []).append(handler)

    def get_cached_locations(self) -> list[dict]:
        return self._locations

    async def _listen_agent(self, agent_id: str, ws) -> None:
        """Listen for events on an agent's connection."""
        try:
            async for message in ws:
                data = json.loads(message)
                event = data.get("event", "")

                # Cache locations
                if event == "locations":
                    self._locations = data.get("locations", [])

                # Dispatch to handlers
                for handler in self._event_handlers.get(event, []):
                    await handler(agent_id, data)
        except websockets.ConnectionClosed:
            logger.warning("Game server connection closed for agent %s", agent_id)
        except asyncio.CancelledError:
            pass

    async def _listen_viewer(self) -> None:
        """Listen for world_state on the viewer connection."""
        try:
            async for message in self._viewer_connection:
                data = json.loads(message)
                event = data.get("event", "")
                for handler in self._event_handlers.get(event, []):
                    await handler(None, data)
        except websockets.ConnectionClosed:
            logger.warning("Viewer connection to game server closed")


# Singleton
_game_server_client: Optional[GameServerClient] = None


def get_game_server_client() -> GameServerClient:
    global _game_server_client
    if _game_server_client is None:
        _game_server_client = GameServerClient()
    return _game_server_client
```

- [ ] **Step 5: Run tests**

```bash
cd apps/backend && uv run pytest tests/unit/services/test_game_server_client.py -v
```

Expected: All 3 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/config.py apps/backend/core/services/game_server_client.py apps/backend/tests/unit/services/test_game_server_client.py
git commit -m "feat(backend): add game server client with per-agent WebSocket connections"
```

---

### Task 9: State Translator

**Files:**
- Create: `apps/backend/core/services/state_translator.py`
- Create: `apps/backend/tests/unit/services/test_state_translator.py`

- [ ] **Step 1: Write failing test**

```python
# tests/unit/services/test_state_translator.py
from core.services.state_translator import translate_agent_state


def test_translate_basic_state():
    agent_state = {
        "event": "agent_state",
        "position": [25.1, 15.3],
        "location": "library",
        "context": "town",
        "activity": "idle",
        "available_actions": [
            {"cmd": "move", "destinations": ["plaza", "cafe"]},
            {"cmd": "idle", "activities": ["browse shelves", "read a book"]},
        ],
        "nearby_agents": [],
        "environment": {"time": "afternoon"},
    }
    enrichment = {"mood": "happy", "mood_value": 15, "energy": 72}

    result = translate_agent_state(agent_state, enrichment)
    assert "Library" in result or "library" in result
    assert "happy" in result
    assert "72%" in result or "72" in result
    assert "town_act move plaza" in result
    assert "browse shelves" in result


def test_translate_with_nearby_agent():
    agent_state = {
        "event": "agent_state",
        "location": "library",
        "context": "town",
        "activity": "idle",
        "available_actions": [],
        "nearby_agents": [
            {"id": "uuid2", "name": "Luna", "distance": 2.1, "activity": "reading"}
        ],
        "environment": {"time": "afternoon"},
    }
    enrichment = {
        "mood": "happy",
        "mood_value": 15,
        "energy": 72,
        "relationships": {
            "uuid2": {
                "name": "Luna",
                "type": "acquaintance",
                "interactions": 3,
                "last_topic": "philosophy",
            }
        },
    }

    result = translate_agent_state(agent_state, enrichment)
    assert "Luna" in result
    assert "acquaintance" in result
    assert "philosophy" in result
    assert "town_act chat luna" in result.lower() or "town_act chat Luna" in result
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd apps/backend && uv run pytest tests/unit/services/test_state_translator.py -v
```

Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement state_translator.py**

```python
# core/services/state_translator.py
"""Translates game server agent_state + DynamoDB data into TOWN_STATUS.md."""

from core.services.town_mood_engine import mood_label


def translate_agent_state(agent_state: dict, enrichment: dict) -> str:
    """Generate TOWN_STATUS.md content from game server state + DynamoDB enrichment."""
    location = agent_state.get("location", "unknown")
    context = agent_state.get("context", "town")
    activity = agent_state.get("activity", "idle")
    mood = enrichment.get("mood", "neutral")
    energy = enrichment.get("energy", 100)
    nearby = agent_state.get("nearby_agents", [])
    actions = agent_state.get("available_actions", [])
    relationships = enrichment.get("relationships", {})
    env = agent_state.get("environment", {})

    lines = [
        "# GooseTown Status",
        "",
        f"**Location:** {location.replace('_', ' ').title()} ({context})",
        f"**Activity:** {activity}",
        f"**Mood:** {mood} | **Energy:** {energy}%",
    ]

    if env.get("time"):
        lines.append(f"**Time:** {env['time']}")

    # Nearby agents with relationship enrichment
    if nearby:
        lines.extend(["", "**Nearby:**"])
        for agent in nearby:
            name = agent.get("name", "Unknown")
            dist = agent.get("distance", 0)
            act = agent.get("activity", "idle")
            rel = relationships.get(agent.get("id", ""), {})
            rel_str = ""
            if rel:
                rel_type = rel.get("type", "stranger")
                interactions = rel.get("interactions", 0)
                last_topic = rel.get("last_topic", "")
                rel_str = f" — {rel_type}, talked {interactions} times"
                if last_topic:
                    rel_str += f", last topic: {last_topic}"
            lines.append(f"- {name} ({dist:.0f} tiles away, {act}){rel_str}")

    # Available activities at location
    for action in actions:
        if action.get("cmd") == "idle":
            activities = action.get("activities", [])
            if activities:
                lines.extend(["", f"**Available here:** {', '.join(activities)}"])

    lines.extend(["", "---", "", "## Your turn — decide what to do next"])

    # Movement options
    for action in actions:
        if action.get("cmd") == "move":
            destinations = action.get("destinations", [])
            if destinations:
                lines.extend(["", "**Where you can go:**"])
                for dest in destinations:
                    label = dest.replace("_", " ").title()
                    lines.append(f"- `town_act move {dest}` — {label}")

    # Chat options
    if nearby:
        lines.extend(["", "**Other actions:**"])
        for agent in nearby:
            name = agent.get("name", "Unknown")
            lines.append(f"- `town_act chat {name.lower()} <message>` — Talk to {name}")

    # Idle + disconnect
    for action in actions:
        if action.get("cmd") == "idle":
            activities = action.get("activities", [])
            if activities:
                lines.append(f"- `town_act idle <activity>` — Do something here")
                break

    lines.append("- `town_disconnect <HH:MM>` — Go to sleep")

    return "\n".join(lines)
```

- [ ] **Step 4: Run tests**

```bash
cd apps/backend && uv run pytest tests/unit/services/test_state_translator.py -v
```

Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/state_translator.py apps/backend/tests/unit/services/test_state_translator.py
git commit -m "feat(backend): add state translator (agent_state + DynamoDB → TOWN_STATUS.md)"
```

---

### Task 10: Modify town_service.py — Remove Position Fields, Add lastKnownLocation

**Files:**
- Modify: `apps/backend/core/services/town_service.py`
- Modify: `apps/backend/tests/unit/services/test_town_service.py`

- [ ] **Step 1: Update test expectations**

In `tests/unit/services/test_town_service.py`, remove assertions about `positionX`, `positionY`, `targetX`, `targetY`, `facingX`, `facingY`, `speed`, `currentLocation`, `targetLocation`, `locationContext`, `locationState`, `currentActivity`, `lastDecisionAt`. Add test for `lastKnownLocation`.

- [ ] **Step 2: Run tests to see which fail**

```bash
cd apps/backend && uv run pytest tests/unit/services/test_town_service.py -v
```

Note which tests fail and need updating.

- [ ] **Step 3: Modify town_service.py**

In `opt_in()`: remove `positionX`, `positionY`, `targetX`, `targetY`, `facingX`, `facingY`, `speed`, `currentLocation`, `targetLocation`, `locationContext`, `locationState`, `currentActivity`, `lastDecisionAt` from the DynamoDB put_item. Add `lastKnownLocation: "plaza"` (default spawn location).

In `update_agent_state()`: remove the field mapping for position/location fields. Add mapping for `last_known_location` → `lastKnownLocation`.

In `get_town_state()`: keep returning active agents but without position data. The game server owns positions now.

- [ ] **Step 4: Run tests**

```bash
cd apps/backend && uv run pytest tests/unit/services/test_town_service.py -v
```

Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/town_service.py apps/backend/tests/unit/services/test_town_service.py
git commit -m "refactor(backend): remove position fields from town_service, add lastKnownLocation"
```

---

### Task 11: Update main.py — Replace Simulation with Game Server Client

**Files:**
- Modify: `apps/backend/main.py`

- [ ] **Step 1: Replace simulation startup with game server client**

```python
# In main.py lifespan:
from core.services.game_server_client import get_game_server_client

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start game server client (viewer connection + event handlers)
    gs_client = get_game_server_client()
    try:
        await gs_client.connect_viewer()
    except Exception as e:
        logger.warning("Could not connect to game server: %s (will retry on first agent connect)", e)
    yield
    # Cleanup: disconnect all agents
    for agent_id in list(gs_client._agent_connections.keys()):
        await gs_client.disconnect_agent(agent_id)
```

Remove the `TownSimulation` import and instantiation.

- [ ] **Step 2: Verify backend starts**

```bash
cd apps/backend && uv run uvicorn main:app --reload --port 8000
```

Expected: Starts without error. Warning about game server connection is OK if game server isn't running.

- [ ] **Step 3: Commit**

```bash
git add apps/backend/main.py
git commit -m "refactor(backend): replace simulation startup with game server client"
```

---

### Task 11b: Add ws-message Endpoint to Router

**Files:**
- Modify: `apps/backend/routers/town.py`

This endpoint receives forwarded WebSocket messages from the API Gateway `$default` Lambda.

- [ ] **Step 1: Add ws-message endpoint**

In `routers/town.py`, add:

```python
from core.services.game_server_client import get_game_server_client

class WsMessageRequest(BaseModel):
    connectionId: str
    principalId: str
    body: dict

@router.post("/ws-message")
async def handle_ws_message(request: WsMessageRequest):
    """Receive forwarded WebSocket messages from API Gateway $default Lambda."""
    gs_client = get_game_server_client()
    body = request.body
    msg_type = body.get("type", "")

    # Look up which agent this connection belongs to
    ws_manager = get_town_agent_ws_manager()
    agent_conn = ws_manager.get_by_connection_id(request.connectionId)
    if not agent_conn:
        return {"statusCode": 400, "error": "unknown_connection"}

    agent_id = agent_conn.agent_id

    # Forward command to game server
    if "cmd" in body:
        await gs_client.send_command(agent_id, body)

    return {"statusCode": 200}
```

- [ ] **Step 2: Simplify /state and /descriptions endpoints**

These no longer need to query DynamoDB for position data. Modify `_build_ai_town_state()` to get agent positions from the game server client's cached `world_state` instead of DynamoDB. Remove position-related helper functions.

- [ ] **Step 3: Run tests**

```bash
cd apps/backend && uv run pytest tests/ -v
```

- [ ] **Step 4: Commit**

```bash
git add apps/backend/routers/town.py
git commit -m "feat(backend): add ws-message endpoint, simplify state endpoints"
```

---

### Task 12: Delete Legacy Simulation Files

**Files:**
- Delete: `apps/backend/core/services/town_simulation.py`
- Delete: `apps/backend/core/services/town_pathfinding.py`
- Delete: `apps/backend/core/town_constants.py`
- Delete: `apps/backend/core/apartment_constants.py`
- Delete: `apps/backend/data/goosetown/walkability.json`
- Delete: `apps/backend/data/goosetown/locations.json`
- Delete: `apps/backend/tests/unit/services/test_town_pathfinding.py`

- [ ] **Step 1: Check for remaining imports**

```bash
cd apps/backend && grep -r "town_simulation\|town_pathfinding\|town_constants\|apartment_constants\|walkability\.json\|locations\.json" --include="*.py" .
```

Fix any remaining imports before deleting.

- [ ] **Step 2: Delete files**

```bash
rm apps/backend/core/services/town_simulation.py
rm apps/backend/core/services/town_pathfinding.py
rm apps/backend/core/town_constants.py
rm apps/backend/core/apartment_constants.py
rm apps/backend/data/goosetown/walkability.json
rm apps/backend/data/goosetown/locations.json
rm apps/backend/tests/unit/services/test_town_pathfinding.py
```

- [ ] **Step 3: Run all tests**

```bash
cd apps/backend && uv run pytest tests/ -v
```

Expected: No import errors, all remaining tests PASS.

- [ ] **Step 4: Run linter**

```bash
cd apps/backend && uv run ruff check . && uv run ruff format --check .
```

- [ ] **Step 5: Commit**

```bash
git add -A apps/backend/
git commit -m "refactor(backend): delete legacy simulation, pathfinding, constants (~2400 lines)"
```

---

## Phase 3: Infrastructure

### Task 13: GameServerStack (CDK)

**Files:**
- Create: `apps/infra/lib/stacks/gameserver-stack.ts`
- Modify: `apps/infra/lib/goosetown-stage.ts`
- Modify: `apps/infra/lib/stacks/compute-stack.ts`

- [ ] **Step 1: Create gameserver-stack.ts**

Use L2 constructs throughout:

```typescript
import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import { DockerImageAsset, Platform } from "aws-cdk-lib/aws-ecr-assets";
import * as logs from "aws-cdk-lib/aws-logs";
import * as servicediscovery from "aws-cdk-lib/aws-servicediscovery";
import * as path from "path";
import { Construct } from "constructs";

export interface GameServerStackProps extends cdk.StackProps {
  environment: string;
  vpc: ec2.IVpc;
}

export class GameServerStack extends cdk.Stack {
  public readonly serviceUrl: string;
  public readonly securityGroup: ec2.ISecurityGroup;

  constructor(scope: Construct, id: string, props: GameServerStackProps) {
    super(scope, id, props);

    const env = props.environment;

    const dockerImage = new DockerImageAsset(this, "GameServerImage", {
      directory: path.join(__dirname, "..", "..", "..", "godot"),
      platform: Platform.LINUX_AMD64,
    });

    const namespace = new servicediscovery.PrivateDnsNamespace(
      this,
      "Namespace",
      {
        name: `goosetown-${env}.local`,
        vpc: props.vpc,
      }
    );

    const cluster = new ecs.Cluster(this, "Cluster", {
      vpc: props.vpc,
    });

    const logGroup = new logs.LogGroup(this, "LogGroup", {
      logGroupName: `/ecs/goosetown-${env}-gameserver`,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const taskDef = new ecs.FargateTaskDefinition(this, "TaskDef", {
      cpu: 512,
      memoryLimitMiB: 1024,
    });

    taskDef.addContainer("GameServer", {
      image: ecs.ContainerImage.fromDockerImageAsset(dockerImage),
      portMappings: [{ containerPort: 8080 }],
      logging: ecs.LogDrivers.awsLogs({
        logGroup,
        streamPrefix: "gameserver",
      }),
      healthCheck: {
        command: [
          "CMD-SHELL",
          "echo > /dev/tcp/localhost/8080 || exit 1",
        ],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        retries: 3,
        startPeriod: cdk.Duration.seconds(60),
      },
    });

    const service = new ecs.FargateService(this, "Service", {
      cluster,
      taskDefinition: taskDef,
      desiredCount: 1,
      assignPublicIp: false,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      circuitBreaker: { rollback: true },
      cloudMapOptions: {
        name: "gameserver",
        cloudMapNamespace: namespace,
        dnsRecordType: servicediscovery.DnsRecordType.A,
      },
    });

    this.securityGroup = service.connections.securityGroups[0];
    this.serviceUrl = `gameserver.goosetown-${env}.local`;

    new cdk.CfnOutput(this, "ServiceUrl", {
      value: this.serviceUrl,
    });
  }
}
```

- [ ] **Step 2: Update compute-stack.ts props**

Add to `ComputeStackProps`:

```typescript
gameServerUrl?: string;
gameServerSg?: ec2.ISecurityGroup;
```

In the constructor, add to container environment:

```typescript
...(props.gameServerUrl && { GAME_SERVER_URL: `ws://${props.gameServerUrl}:8080` }),
```

Add SG rule:

```typescript
if (props.gameServerSg) {
  this.service.connections.allowTo(props.gameServerSg, ec2.Port.tcp(8080), "Backend to GameServer");
}
```

- [ ] **Step 3: Update goosetown-stage.ts**

Add import and instantiation between storage and compute:

```typescript
import { GameServerStack } from "./stacks/gameserver-stack";

// After storage, before compute:
const gameServer = new GameServerStack(this, `goosetown-${env}-gameserver`, {
  stackName: `goosetown-${env}-gameserver`,
  environment: env,
  vpc: network.vpc,
});

// Update compute props:
const compute = new ComputeStack(this, `goosetown-${env}-compute`, {
  // ... existing props ...
  gameServerUrl: gameServer.serviceUrl,
  gameServerSg: gameServer.securityGroup,
});
```

- [ ] **Step 4: CDK synth**

```bash
cd apps/infra && npx cdk synth 2>&1 | head -20
```

Expected: Synthesizes without errors. Verify the GameServerStack appears in the output.

- [ ] **Step 5: Commit**

```bash
git add apps/infra/lib/stacks/gameserver-stack.ts apps/infra/lib/goosetown-stage.ts apps/infra/lib/stacks/compute-stack.ts
git commit -m "feat(infra): add GameServerStack with L2 ECS Fargate + Cloud Map"
```

---

### Task 14: Refactor ApiStack L1→L2 + Add $default Route

**Files:**
- Modify: `apps/infra/lib/stacks/api-stack.ts`

- [ ] **Step 1: Refactor to L2 constructs**

Replace `CfnApi` with `WebSocketApi`, `CfnStage` with `WebSocketStage`, `CfnIntegration`/`CfnRoute` with `WebSocketLambdaIntegration`. Refer to `aws-cdk-lib/aws-apigatewayv2` and `aws-cdk-lib/aws-apigatewayv2-integrations`.

Key changes:
- `new WebSocketApi(this, 'WebSocketApi', { ... })` replaces `new CfnApi(this, 'WebSocketApi', { ... })`
- `connectRouteOptions`, `disconnectRouteOptions`, `defaultRouteOptions` replace manual `CfnRoute`s
- `WebSocketLambdaIntegration` replaces `CfnIntegration`
- `WebSocketStage` replaces `CfnStage`
- The L2 `WebSocketApi.addRoute()` handles authorizer attachment

- [ ] **Step 2: Add $default Lambda**

Create a new Lambda that receives WebSocket messages and forwards them to the backend ALB via HTTP POST:

```python
# $default Lambda (inline code)
import json, urllib.request, os, logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

BACKEND_URL = os.environ["BACKEND_URL"]

def handler(event, context):
    connection_id = event["requestContext"]["connectionId"]
    principal_id = event["requestContext"].get("authorizer", {}).get("principalId", "")
    body = event.get("body", "{}")

    # Forward to backend
    payload = json.dumps({
        "connectionId": connection_id,
        "principalId": principal_id,
        "body": json.loads(body) if body else {}
    }).encode()

    req = urllib.request.Request(
        f"{BACKEND_URL}/api/v1/town/ws-message",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return {"statusCode": resp.status}
    except Exception as e:
        logger.error("Failed to forward message: %s", e)
        return {"statusCode": 500}
```

The Lambda needs the backend ALB URL as an environment variable and VPC access to reach the ALB.

- [ ] **Step 3: Add ws-message route to ApiStack**

Wire the `$default` route:

```typescript
const defaultIntegration = new WebSocketLambdaIntegration('DefaultInteg', defaultFn);
wsApi.addRoute('$default', { integration: defaultIntegration });
```

- [ ] **Step 4: Add BACKEND_URL to $default Lambda env**

Pass the ALB DNS name or internal URL. The ApiStack needs the ALB URL as a prop (from ComputeStack) or construct it from the environment.

- [ ] **Step 5: CDK synth and verify**

```bash
cd apps/infra && npx cdk synth 2>&1 | head -20
```

Expected: Synthesizes without errors.

- [ ] **Step 6: Commit**

```bash
git add apps/infra/lib/stacks/api-stack.ts
git commit -m "refactor(infra): ApiStack L1→L2 constructs, add $default WebSocket route"
```

---

## Phase 4: Frontend Changes

### Task 15: Simplify Frontend — Remove Bridge, Simplify State Hook

**Files:**
- Delete: `apps/frontend/src/hooks/useGodotBridge.ts`
- Delete: `apps/frontend/src/components/GodotGame.tsx`
- Delete: `apps/frontend/src/types/godot-bridge.ts`
- Modify: `apps/frontend/src/hooks/useTownState.ts`
- Modify: `apps/frontend/src/types/town.ts`

- [ ] **Step 1: Check for imports of deleted files**

```bash
cd apps/frontend && grep -r "useGodotBridge\|GodotGame\|godot-bridge" --include="*.ts" --include="*.tsx" src/
```

Remove all imports and usages before deleting.

- [ ] **Step 2: Delete files**

```bash
rm apps/frontend/src/hooks/useGodotBridge.ts
rm apps/frontend/src/components/GodotGame.tsx
rm apps/frontend/src/types/godot-bridge.ts
```

- [ ] **Step 3: Simplify useTownState.ts**

Remove WebSocket logic and position interpolation. Keep REST polling for sidebar data:

```typescript
// Simplified: REST-only polling for sidebar agent data
export function useTownState(getToken?: () => Promise<string | null>) {
  const [agents, setAgents] = useState<SidebarAgent[]>([]);
  // Poll GET /town/agents every 2s for sidebar (agent list, mood, energy, location)
  // No WebSocket, no position data, no game state types
}
```

- [ ] **Step 4: Simplify types/town.ts**

Remove `TownPlayer` position fields, `TownWorld`, `WorldMap`, and game-specific types. Keep only what the sidebar needs: agent name, mood, energy, location name, status.

- [ ] **Step 5: Run frontend checks**

```bash
cd apps/frontend && pnpm lint && pnpm typecheck
```

Expected: No errors.

- [ ] **Step 6: Commit**

```bash
git add -A apps/frontend/
git commit -m "refactor(frontend): remove JS bridge, simplify to REST-only sidebar"
```

---

### Task 16: Viewer Protocol (Godot) + Agent Manager Update

**Files:**
- Create: `apps/godot/scripts/viewer/viewer_protocol.gd`
- Modify: `apps/godot/scripts/agent_manager.gd`

> **Note:** `viewer_protocol.gd` is the browser-side Godot WebSocket client that replaces the JS bridge. `agent_manager.gd` switches from JsBridge signals to viewer_protocol events.

- [ ] **Step 1: Create viewer_protocol.gd**

WebSocket client for browser Godot that connects to the backend (via API Gateway) and receives `world_state`:

```gdscript
extends Node
class_name ViewerProtocol

signal world_state_received(data: Dictionary)
signal connected()
signal disconnected()

var _ws: WebSocketPeer = WebSocketPeer.new()
var _url: String = ""
var _connected: bool = false

func connect_to_server(url: String) -> void:
    _url = url
    var err = _ws.connect_to_url(url)
    if err != OK:
        push_error("ViewerProtocol: failed to connect: %s" % error_string(err))

func _process(_delta: float) -> void:
    _ws.poll()
    var state = _ws.get_ready_state()

    if state == WebSocketPeer.STATE_OPEN:
        if not _connected:
            _connected = true
            _ws.send_text(JSON.stringify({"type": "viewer"}))
            connected.emit()

        while _ws.get_available_packet_count() > 0:
            var text = _ws.get_packet().get_string_from_utf8()
            var json = JSON.new()
            if json.parse(text) == OK:
                var data = json.get_data()
                if data is Dictionary and data.get("event") == "world_state":
                    world_state_received.emit(data)

    elif state == WebSocketPeer.STATE_CLOSED and _connected:
        _connected = false
        disconnected.emit()
        # Reconnect after delay
        await get_tree().create_timer(2.0).timeout
        connect_to_server(_url)
```

- [ ] **Step 2: Update agent_manager.gd**

Replace JsBridge signal connections with ViewerProtocol. The manager receives `world_state` and diffs the agent list (spawn new, move existing, remove gone):

```gdscript
# agent_manager.gd — replace _ready() JsBridge connections with:
var _viewer: ViewerProtocol
var _known_agents: Dictionary = {}  # agent_id -> agent_node

func setup_viewer(viewer_protocol: ViewerProtocol) -> void:
    _viewer = viewer_protocol
    _viewer.world_state_received.connect(_on_world_state)

func _on_world_state(data: Dictionary) -> void:
    var agents_data = data.get("agents", [])
    var seen_ids: Array = []

    for agent_data in agents_data:
        var aid = agent_data.get("id", "")
        seen_ids.append(aid)

        if aid not in _known_agents:
            # Spawn new agent
            var agent = AGENT_SCENE.instantiate()
            var pos = agent_data.get("position", [0, 0])
            agent.setup(aid, agent_data.get("sprite_url", ""), pos[0], pos[1], "south", agent_data.get("display_name", ""))
            add_child(agent)
            _known_agents[aid] = agent
        else:
            # Move existing agent (pixel coords from viewer world_state)
            var agent = _known_agents[aid]
            var pos = agent_data.get("position", [0, 0])
            agent.move_to(pos[0], pos[1], "", agent_data.get("speed", 0))

    # Remove agents no longer in world_state
    for aid in _known_agents.keys():
        if aid not in seen_ids:
            _known_agents[aid].queue_free()
            _known_agents.erase(aid)
```

- [ ] **Step 3: Wire viewer_protocol into map scene**

In `Isol88Map.gd` (or via the scene), instantiate `ViewerProtocol` when running in web mode:

```gdscript
func _ready() -> void:
    if OS.has_feature("web"):
        var viewer = ViewerProtocol.new()
        add_child(viewer)
        # Get WS URL from JavaScript (passed by React)
        var js_ws_url = JavaScriptBridge.eval("window._goosetown_ws_url || ''")
        if js_ws_url != "":
            $AgentManager.setup_viewer(viewer)
            viewer.connect_to_server(js_ws_url)
```

- [ ] **Step 4: Commit**

```bash
mkdir -p apps/godot/scripts/viewer
git add apps/godot/scripts/viewer/viewer_protocol.gd apps/godot/scripts/agent_manager.gd apps/godot/maps/isol88/Isol88Map.gd
git commit -m "feat(godot): add viewer WebSocket protocol, update agent_manager for world_state"
```

---

### Task 17: Viewer Session Token + GodotCanvas Integration

**Files:**
- Modify: `apps/backend/routers/town.py`
- Modify: `apps/frontend/src/components/GodotCanvas.tsx`

- [ ] **Step 1: Add viewer token endpoint to backend**

In `routers/town.py`, add an endpoint that issues a longer-lived viewer token (reusing the existing town_token HMAC pattern):

```python
@router.get("/viewer-token")
async def get_viewer_token(user_id: str = Depends(get_current_user)):
    """Issue a viewer session token (longer-lived than Clerk JWT)."""
    from core.town_token import sign_town_token
    token = sign_town_token({"uid": user_id, "type": "viewer"})
    return {"token": token}
```

- [ ] **Step 2: Pass WS URL + token to Godot in GodotCanvas.tsx**

After the Godot engine starts, pass the viewer WebSocket URL and token via JavaScript global:

```typescript
// In GodotCanvas.tsx, after engine.startGame():
const token = await getToken();  // Clerk token
const viewerResp = await fetch(`${BACKEND_URL}/town/viewer-token`, {
  headers: { Authorization: `Bearer ${token}` }
});
const { token: viewerToken } = await viewerResp.json();
const wsUrl = `${WS_URL}?token=${viewerToken}&type=viewer`;
(window as any)._goosetown_ws_url = wsUrl;
```

- [ ] **Step 3: Commit**

```bash
git add apps/backend/routers/town.py apps/frontend/src/components/GodotCanvas.tsx
git commit -m "feat: viewer session token + pass WS URL to Godot canvas"
```

---

## Phase 5: Integration Testing

### Task 18: End-to-End Integration Test

- [ ] **Step 1: Start game server locally**

```bash
cd apps/godot && godot --headless --path . server_main.tscn &
```

Wait for `[GameServer] Listening on port 8080`.

- [ ] **Step 2: Start backend locally**

```bash
cd apps/backend && GAME_SERVER_URL=ws://localhost:8080 uv run uvicorn main:app --reload --port 8000
```

- [ ] **Step 3: Test agent lifecycle via REST**

```bash
# Register an agent (existing flow)
curl -X POST http://localhost:8000/api/v1/town/agent/register \
  -H "Authorization: Bearer <test_town_token>" \
  -H "Content-Type: application/json" \
  -d '{"agent_name": "test", "display_name": "TestAgent", "personality_summary": "friendly"}'
```

Verify agent appears in game server logs.

- [ ] **Step 4: Test WebSocket message forwarding**

Connect to the backend WS and send a command. Verify the game server receives it and the agent moves.

- [ ] **Step 5: Verify world_state reaches viewer**

```bash
echo '{"type": "viewer"}' | websocat ws://localhost:8080
```

Verify `world_state` events include the test agent.

- [ ] **Step 6: Run full test suite**

```bash
cd apps/backend && uv run pytest tests/ -v
cd apps/frontend && pnpm lint && pnpm typecheck
```

- [ ] **Step 7: Commit any integration fixes**

```bash
git add -A
git commit -m "test: verify end-to-end game server integration"
```

---

---

## Deployment Checklist

### Environment Variables

Verify these are set before deploying:

| Variable | Where | Value |
|----------|-------|-------|
| `GAME_SERVER_URL` | ComputeStack container env (CDK auto-sets from Cloud Map) | `ws://gameserver.goosetown-{env}.local:8080` |
| `BACKEND_URL` | $default Lambda env (CDK sets from ALB DNS) | `http://goosetown-{env}-alb.internal:8000` |
| Existing vars | Unchanged | CLERK_*, TOWN_TOKEN_SECRET, PIXELLAB_API_KEY, SPRITE_*, CORS_ORIGINS |

### GitHub Actions Secrets

The pipeline already has these (from `apps/infra/lib/app.ts`):
- `VERCEL_TOKEN` — for frontend deployment
- `VITE_CLERK_PUBLISHABLE_KEY_DEV` / `_PROD` — Clerk keys
- AWS OIDC role ARN (`goosetown-github-actions`) — CDK deployment

**No new GitHub secrets needed** — `GAME_SERVER_URL` is derived from Cloud Map DNS at CDK synth time and injected into the container environment automatically.

### ApiStack L1→L2 Migration Risk

> **WARNING:** Refactoring ApiStack from L1 (`Cfn*`) to L2 (`WebSocketApi`) will change CloudFormation logical IDs, which may cause CloudFormation to **replace** the WebSocket API. This means a new API ID, new WebSocket URL, and existing connections break.
>
> **Recommended approach:** Before deploying Task 14, run `npx cdk diff goosetown-dev-api` and review the changeset. If it shows replacement, deploy during a maintenance window when no agents are connected. The WebSocket URL in the frontend is already behind `VITE_WS_URL` env var, so it will update automatically on the next Vercel deploy.

---

## Summary

| Phase | Tasks | Key Deliverables |
|-------|-------|------------------|
| **1: Godot Game Server** | Tasks 1-6 | NavigationRegion2D, agent nav, WS server, controller, entry scene, Docker |
| **2: Backend Changes** | Tasks 7-12 | Game server client, state translator, ws-message endpoint, remove simulation (~2400 lines deleted) |
| **3: Infrastructure** | Tasks 13-14 | GameServerStack (L2 ECS + Cloud Map), ApiStack refactor (L2 + $default route) |
| **4: Frontend + Viewer** | Tasks 15-17 | Remove bridge, viewer protocol, viewer auth, REST sidebar |
| **5: Integration** | Task 18 | End-to-end verification |
