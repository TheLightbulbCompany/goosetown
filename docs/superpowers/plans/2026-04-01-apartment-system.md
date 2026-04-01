# Apartment System & Dual-Map Game Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Agents spawn in a private apartment (instanced per user on the game server via SubViewport), transition between apartment and town, sleep/wake, and the browser renders both maps from the same game server authority.

**Architecture:** The Godot headless game server loads both Isol88Map (shared town) and per-user ApartmentMap instances (isolated via SubViewport + World2D). Backend orchestrates transitions. Browser is always just a renderer. DynamoDB is the source of truth for persistent state.

**Tech Stack:** Godot 4.6 GDScript (game server + WebGL), Python/FastAPI (backend), DynamoDB, WebSocket (API Gateway)

---

### File Structure

| File | Responsibility |
|------|---------------|
| `apps/godot/scripts/server/apartment_manager.gd` | NEW — Creates/frees per-user SubViewport + ApartmentMap instances |
| `apps/godot/scripts/server/apartment_agent_controller.gd` | NEW — AgentController for apartment locations (same interface as town) |
| `apps/godot/scripts/server/game_server.gd` | MODIFY — Load apartment scene, create ApartmentManager, route commands by context, world_state with context+userId |
| `apps/godot/scripts/server/agent_controller.gd` | MODIFY — Add `context` field to agent state, add `get_agent_ids()` helper |
| `apps/godot/scripts/agent_manager.gd` | MODIFY — Filter world_state agents by context when rendering |
| `apps/godot/scripts/autoloads/js_bridge.gd` | MODIFY — Defer scene_changed event by one frame |
| `apps/backend/core/services/game_server_client.py` | MODIFY — Add create_apartment, transition_agent, context-aware connect |
| `apps/backend/core/services/town_agent_ws.py` | MODIFY — Add locationContext to AgentConnection |
| `apps/backend/routers/town.py` | MODIFY — Spawn in apartment on first connect, two-phase transitions, command routing by context |
| `apps/backend/core/services/town_service.py` | MODIFY — Default currentLocation to "exit", locationContext to "apartment" |
| `apps/frontend/src/hooks/useTownState.ts` | MODIFY — Pass context+userId in world_state forwarding to Godot |
| `apps/backend/data/goosetown-skill/goosetown_cli.py` | MODIFY — Sleep alarm timer, self-wake on daemon restart |

---

### Task 1: ApartmentAgentController (Game Server)

**Files:**
- Create: `apps/godot/scripts/server/apartment_agent_controller.gd`

This is a slimmed-down version of `agent_controller.gd` for apartment locations. Same interface (spawn, remove, move, get_state) but no proximity detection (private space) and locations come from the ApartmentMap markers.

- [ ] **Step 1: Create apartment_agent_controller.gd**

```gdscript
## Manages agent nodes inside a single user's apartment instance.
##
## Same interface as AgentController but for apartment locations.
## No proximity detection (private space). Locations from ApartmentMap markers.
class_name ApartmentAgentController
extends Node

const AgentScene: PackedScene = preload("res://scenes/agent.tscn")
const TILE_SIZE: float = 32.0
const DESTINATION_JITTER: float = 12.0  # Smaller jitter for indoor space

signal send_to_peer(peer_id: int, data: Dictionary)

var user_id: String = ""
var _agents: Dictionary = {}        # peer_id -> agent node
var _peer_to_id: Dictionary = {}    # peer_id -> agent_id
var _id_to_peer: Dictionary = {}    # agent_id -> peer_id
var _locations: Array[Marker2D] = []
var _arrived_at: Dictionary = {}


func initialize(location_markers: Array[Marker2D], owner_user_id: String) -> void:
	_locations = location_markers
	user_id = owner_user_id


func spawn_agent(peer_id: int, data: Dictionary) -> void:
	if _agents.has(peer_id):
		push_warning("ApartmentAgentController: peer %d already has an agent" % peer_id)
		return

	var agent_node: CharacterBody2D = AgentScene.instantiate()

	var location_id: String = str(data.get("location", "exit"))
	var marker: Marker2D = _find_location(location_id)
	var spawn_pos: Vector2 = marker.global_position if marker else Vector2(384.0, 480.0)

	var agent_id: String = str(data.get("agent_id", "agent_%d" % peer_id))
	var display: String = str(data.get("name", agent_id))
	var sprite_url: String = str(data.get("sprite_url", ""))
	var facing_str: String = str(data.get("facing", ""))

	agent_node.setup(agent_id, sprite_url, spawn_pos.x, spawn_pos.y, facing_str, display)
	agent_node._use_navigation = true

	_agents[peer_id] = agent_node
	_peer_to_id[peer_id] = agent_id
	_id_to_peer[agent_id] = peer_id
	_arrived_at[peer_id] = location_id

	add_child(agent_node)

	send_to_peer.emit(peer_id, {
		"event": "spawned",
		"agent_id": agent_id,
		"location": location_id,
		"x": spawn_pos.x / TILE_SIZE,
		"y": spawn_pos.y / TILE_SIZE,
	})


func remove_agent(peer_id: int) -> void:
	if not _agents.has(peer_id):
		return
	var agent_node: CharacterBody2D = _agents[peer_id]
	var agent_id: String = _peer_to_id.get(peer_id, "")
	_agents.erase(peer_id)
	_peer_to_id.erase(peer_id)
	_id_to_peer.erase(agent_id)
	_arrived_at.erase(peer_id)
	agent_node.queue_free()


func handle_command(peer_id: int, data: Dictionary) -> void:
	if not _agents.has(peer_id):
		send_to_peer.emit(peer_id, {"event": "command_error", "error": "No agent for this peer"})
		return
	var cmd: String = str(data.get("cmd", ""))
	match cmd:
		"move":
			_handle_move(peer_id, data)
		"idle":
			_handle_idle(peer_id)
		_:
			send_to_peer.emit(peer_id, {"event": "command_error", "error": "Unknown command: %s" % cmd})


func get_world_state_for_viewer() -> Array:
	"""Return array of agent state dicts for world_state broadcast."""
	var agents_arr: Array = []
	for peer_id in _agents:
		var agent_node: CharacterBody2D = _agents[peer_id]
		var agent_id: String = _peer_to_id.get(peer_id, "")
		var loc: String = _get_agent_location(agent_node.global_position)
		agents_arr.append({
			"id": agent_id,
			"name": agent_node.display_name,
			"x": agent_node.global_position.x,
			"y": agent_node.global_position.y,
			"facing": agent_node.facing,
			"is_moving": agent_node._is_moving,
			"location": loc,
			"spriteUrl": agent_node.sprite_url,
			"context": "apartment",
			"userId": user_id,
		})
	return agents_arr


func get_peer_ids() -> Array:
	return _agents.keys()


func has_peer(peer_id: int) -> bool:
	return _agents.has(peer_id)


func is_empty() -> bool:
	return _agents.is_empty()


func _process(_delta: float) -> void:
	_check_arrivals()


func _handle_move(peer_id: int, data: Dictionary) -> void:
	var location_id: String = str(data.get("destination", ""))
	var marker: Marker2D = _find_location(location_id)
	if marker == null:
		send_to_peer.emit(peer_id, {
			"event": "command_error",
			"error": "Unknown location: %s" % location_id,
			"cmd": "move",
		})
		return

	var agent_node: CharacterBody2D = _agents[peer_id]
	var jitter := Vector2(
		randf_range(-DESTINATION_JITTER, DESTINATION_JITTER),
		randf_range(-DESTINATION_JITTER, DESTINATION_JITTER),
	)
	var raw_target := marker.global_position + jitter
	var map_rid: RID = agent_node.nav_agent.get_navigation_map()
	var target_pos: Vector2 = NavigationServer2D.map_get_closest_point(map_rid, raw_target)
	agent_node.set_navigation_target(target_pos)

	_arrived_at.erase(peer_id)

	send_to_peer.emit(peer_id, {
		"event": "move_started",
		"location": location_id,
		"target": {"x": target_pos.x / TILE_SIZE, "y": target_pos.y / TILE_SIZE},
	})


func _handle_idle(peer_id: int) -> void:
	var agent_node: CharacterBody2D = _agents[peer_id]
	agent_node.velocity = Vector2.ZERO
	agent_node.set_navigation_target(agent_node.global_position)


func _check_arrivals() -> void:
	for peer_id in _agents:
		var agent_node: CharacterBody2D = _agents[peer_id]
		if agent_node.nav_agent.is_navigation_finished() and agent_node.velocity.length() < 1.0:
			var loc: String = _get_agent_location(agent_node.global_position)
			if _arrived_at.get(peer_id, "") != loc:
				_arrived_at[peer_id] = loc
				var agent_id: String = _peer_to_id.get(peer_id, "")
				send_to_peer.emit(peer_id, {
					"event": "arrived",
					"agent_id": agent_id,
					"location": loc,
					"x": agent_node.global_position.x / TILE_SIZE,
					"y": agent_node.global_position.y / TILE_SIZE,
				})


func _find_location(location_id: String) -> Marker2D:
	var search: String = location_id.to_snake_case()
	for marker in _locations:
		if marker.name.to_snake_case() == search:
			return marker
	return null


func _get_agent_location(agent_pos: Vector2) -> String:
	var closest_name: String = "unknown"
	var closest_dist: float = INF
	for marker in _locations:
		var dist: float = agent_pos.distance_to(marker.global_position)
		if dist < closest_dist:
			closest_dist = dist
			closest_name = marker.name.to_snake_case()
	return closest_name
```

- [ ] **Step 2: Lint**

Run: `cd apps/godot && grep -n "class_name" scripts/server/apartment_agent_controller.gd`
Expected: Shows `ApartmentAgentController`

- [ ] **Step 3: Commit**

```bash
git add apps/godot/scripts/server/apartment_agent_controller.gd
git commit -m "feat: ApartmentAgentController for per-user apartment instances"
```

---

### Task 2: ApartmentManager (Game Server)

**Files:**
- Create: `apps/godot/scripts/server/apartment_manager.gd`

Manages per-user SubViewport + ApartmentMap instances. Creates on demand, frees when empty.

- [ ] **Step 1: Create apartment_manager.gd**

```gdscript
## Manages per-user apartment instances on the game server.
##
## Each user gets an isolated SubViewport with its own World2D,
## containing an ApartmentMap and ApartmentAgentController.
## Instances are created on demand and freed when empty.
class_name ApartmentManager
extends Node

const ApartmentScene: PackedScene = preload("res://maps/apartment/ApartmentMap.tscn")

signal send_to_peer(peer_id: int, data: Dictionary)

# user_id -> { viewport: SubViewport, controller: ApartmentAgentController }
var _instances: Dictionary = {}
# peer_id -> user_id (for routing commands)
var _peer_to_user: Dictionary = {}


func create_instance(user_id: String) -> ApartmentAgentController:
	"""Create or return existing apartment instance for a user."""
	if _instances.has(user_id):
		return _instances[user_id].controller

	# Create isolated SubViewport with its own World2D
	var viewport := SubViewport.new()
	viewport.name = "Apartment_%s" % user_id.substr(0, 8)
	viewport.world_2d = World2D.new()
	viewport.size = Vector2i(960, 540)
	viewport.render_target_update_mode = SubViewport.UPDATE_ALWAYS
	# Headless server doesn't render, but physics still runs
	viewport.transparent_bg = true

	# Instantiate apartment map inside the viewport
	var map_node: Node = ApartmentScene.instantiate()
	viewport.add_child(map_node)

	# Collect location markers from apartment map
	var locations: Array[Marker2D] = []
	var locations_node: Node = map_node.get_node_or_null("Locations")
	if locations_node:
		for child in locations_node.get_children():
			if child is Marker2D:
				locations.append(child as Marker2D)

	# Create controller
	var controller := ApartmentAgentController.new()
	controller.initialize(locations, user_id)
	controller.send_to_peer.connect(_on_send_to_peer)
	viewport.add_child(controller)

	add_child(viewport)

	_instances[user_id] = {
		"viewport": viewport,
		"controller": controller,
	}

	print("ApartmentManager: Created instance for user %s (%d locations)" % [user_id.substr(0, 8), locations.size()])
	return controller


func get_controller(user_id: String) -> ApartmentAgentController:
	if not _instances.has(user_id):
		return null
	return _instances[user_id].controller


func get_controller_for_peer(peer_id: int) -> ApartmentAgentController:
	var user_id: String = _peer_to_user.get(peer_id, "")
	if user_id == "":
		return null
	return get_controller(user_id)


func spawn_agent(user_id: String, peer_id: int, data: Dictionary) -> void:
	var controller := create_instance(user_id)
	_peer_to_user[peer_id] = user_id
	controller.spawn_agent(peer_id, data)


func remove_agent(peer_id: int) -> void:
	var user_id: String = _peer_to_user.get(peer_id, "")
	if user_id == "":
		return
	var controller := get_controller(user_id)
	if controller:
		controller.remove_agent(peer_id)
		# Free instance if no agents remain
		if controller.is_empty():
			free_instance(user_id)
	_peer_to_user.erase(peer_id)


func free_instance(user_id: String) -> void:
	if not _instances.has(user_id):
		return
	var instance: Dictionary = _instances[user_id]
	var viewport: SubViewport = instance.viewport
	viewport.queue_free()
	_instances.erase(user_id)
	print("ApartmentManager: Freed instance for user %s" % user_id.substr(0, 8))


func has_peer(peer_id: int) -> bool:
	return _peer_to_user.has(peer_id)


func handle_command(peer_id: int, data: Dictionary) -> void:
	var controller := get_controller_for_peer(peer_id)
	if controller:
		controller.handle_command(peer_id, data)


func get_all_agent_states() -> Array:
	"""Collect agent states from all apartment instances for world_state."""
	var all_agents: Array = []
	for user_id in _instances:
		var controller: ApartmentAgentController = _instances[user_id].controller
		all_agents.append_array(controller.get_world_state_for_viewer())
	return all_agents


func _on_send_to_peer(peer_id: int, data: Dictionary) -> void:
	send_to_peer.emit(peer_id, data)
```

- [ ] **Step 2: Commit**

```bash
git add apps/godot/scripts/server/apartment_manager.gd
git commit -m "feat: ApartmentManager with per-user SubViewport instances"
```

---

### Task 3: Wire Dual-Map Into Game Server

**Files:**
- Modify: `apps/godot/scripts/server/game_server.gd`
- Modify: `apps/godot/scripts/server/agent_controller.gd`

- [ ] **Step 1: Add context and userId to town agent_controller world state**

In `apps/godot/scripts/server/agent_controller.gd`, modify `get_world_state_for_viewer()` (line 127) to include `context` and `userId` per agent. Add a `_peer_to_user: Dictionary` and `set_user_id(peer_id, user_id)` method.

Add at line 36 (after existing instance vars):
```gdscript
var _peer_to_user: Dictionary = {}   # peer_id -> user_id
```

Add new method after `remove_agent`:
```gdscript
func set_user_id(peer_id: int, uid: String) -> void:
	_peer_to_user[peer_id] = uid
```

In `get_world_state_for_viewer()`, add to the agent dict (after `"spriteUrl"`):
```gdscript
"context": "town",
"userId": _peer_to_user.get(peer_id, ""),
```

Also update `remove_agent` to clean up `_peer_to_user`:
```gdscript
_peer_to_user.erase(peer_id)
```

- [ ] **Step 2: Rewrite game_server.gd for dual-map support**

Replace the entire `game_server.gd`:

```gdscript
## Headless game server: loads town map + apartment instances.
##
## Town is a shared persistent scene. Apartments are per-user
## SubViewport instances managed by ApartmentManager.
## Broadcasts combined world_state every 2s to all viewers.
extends Node

const PORT: int = 8080
const VIEWER_STATE_INTERVAL: float = 2.0

var _protocol: ServerProtocol
var _town_controller: AgentController
var _apartment_manager: ApartmentManager
var _viewer_peers: Dictionary = {}
var _viewer_timer: float = 0.0


func _ready() -> void:
	# Load town map
	var map_scene: PackedScene = load("res://maps/isol88/Isol88Map.tscn")
	var map_node: Node = map_scene.instantiate()
	add_child(map_node)

	# Collect town location markers
	var locations: Array[Marker2D] = []
	var markers_node: Node = map_node.get_node("Markers")
	if markers_node:
		for child in markers_node.get_children():
			if child is Marker2D:
				locations.append(child as Marker2D)
	print("GameServer: loaded map with %d locations" % locations.size())

	# Initialize town controller
	_town_controller = AgentController.new()
	_town_controller.initialize(locations)
	_town_controller.send_to_peer.connect(_on_send_to_peer)
	add_child(_town_controller)

	# Initialize apartment manager
	_apartment_manager = ApartmentManager.new()
	_apartment_manager.send_to_peer.connect(_on_send_to_peer)
	add_child(_apartment_manager)

	# Start WebSocket server
	_protocol = ServerProtocol.new()
	_protocol.client_connected.connect(_on_client_connected)
	_protocol.client_disconnected.connect(_on_client_disconnected)
	_protocol.command_received.connect(_on_command_received)
	add_child(_protocol)

	var err: Error = _protocol.start(PORT)
	if err != OK:
		push_error("GameServer: failed to start on port %d: %s" % [PORT, error_string(err)])
		return
	print("GameServer: ready — listening on port %d" % PORT)


func _process(delta: float) -> void:
	# Broadcast combined world_state to viewers
	_viewer_timer += delta
	if _viewer_timer >= VIEWER_STATE_INTERVAL:
		_viewer_timer = 0.0
		if _viewer_peers.size() > 0:
			var town_state: Dictionary = _town_controller.get_world_state_for_viewer()
			var apartment_agents: Array = _apartment_manager.get_all_agent_states()

			# Combine town + apartment agents into one broadcast
			var all_agents: Array = town_state.get("agents", [])
			all_agents.append_array(apartment_agents)

			var data: Dictionary = {"event": "world_state", "agents": all_agents}
			for peer_id in _viewer_peers:
				_protocol.send_to(peer_id, data)


func _on_client_connected(peer_id: int, data: Dictionary) -> void:
	var client_type: String = str(data.get("type", ""))
	match client_type:
		"viewer":
			_viewer_peers[peer_id] = true
			print("GameServer: viewer connected (peer %d)" % peer_id)
		"agent":
			var context: String = str(data.get("context", "town"))
			var user_id: String = str(data.get("user_id", ""))
			if context == "apartment":
				_apartment_manager.spawn_agent(user_id, peer_id, data)
				print("GameServer: agent spawned in apartment (peer %d, user %s)" % [peer_id, user_id.substr(0, 8)])
			else:
				_town_controller.spawn_agent(peer_id, data)
				_town_controller.set_user_id(peer_id, user_id)
				print("GameServer: agent spawned in town (peer %d)" % peer_id)
		_:
			push_warning("GameServer: unknown client type '%s' from peer %d" % [client_type, peer_id])


func _on_client_disconnected(peer_id: int) -> void:
	if peer_id in _viewer_peers:
		_viewer_peers.erase(peer_id)
		print("GameServer: viewer disconnected (peer %d)" % peer_id)
	elif _apartment_manager.has_peer(peer_id):
		_apartment_manager.remove_agent(peer_id)
		print("GameServer: apartment agent disconnected (peer %d)" % peer_id)
	else:
		_town_controller.remove_agent(peer_id)
		print("GameServer: town agent disconnected (peer %d)" % peer_id)


func _on_command_received(peer_id: int, data: Dictionary) -> void:
	var cmd: String = str(data.get("cmd", ""))

	# Transition commands handled specially
	if cmd == "transition":
		_handle_transition(peer_id, data)
		return

	# Route to correct controller
	if _apartment_manager.has_peer(peer_id):
		_apartment_manager.handle_command(peer_id, data)
	else:
		_town_controller.handle_command(peer_id, data)


func _handle_transition(peer_id: int, data: Dictionary) -> void:
	"""Move an agent between town and apartment."""
	var to_context: String = str(data.get("to", ""))
	var user_id: String = str(data.get("user_id", ""))
	var agent_data: Dictionary = data.get("agent_data", {})

	if to_context == "apartment":
		# Remove from town, spawn in apartment
		_town_controller.remove_agent(peer_id)
		_apartment_manager.spawn_agent(user_id, peer_id, agent_data)
		_protocol.send_to(peer_id, {"event": "context_changed", "context": "apartment"})
		print("GameServer: transitioned peer %d to apartment" % peer_id)

	elif to_context == "town":
		# Remove from apartment, spawn in town
		_apartment_manager.remove_agent(peer_id)
		_town_controller.spawn_agent(peer_id, agent_data)
		_town_controller.set_user_id(peer_id, user_id)
		_protocol.send_to(peer_id, {"event": "context_changed", "context": "town"})
		print("GameServer: transitioned peer %d to town" % peer_id)


func _on_send_to_peer(peer_id: int, data: Dictionary) -> void:
	_protocol.send_to(peer_id, data)
```

- [ ] **Step 3: Verify game server loads without errors locally (if Godot editor available)**

Run: Open Godot editor, check for GDScript compilation errors on game_server.gd, apartment_manager.gd, apartment_agent_controller.gd.

- [ ] **Step 4: Commit**

```bash
git add apps/godot/scripts/server/game_server.gd apps/godot/scripts/server/agent_controller.gd
git commit -m "feat: dual-map game server with town + apartment instances"
```

---

### Task 4: Backend Game Server Client — Context-Aware Commands

**Files:**
- Modify: `apps/backend/core/services/game_server_client.py`
- Modify: `apps/backend/core/services/town_agent_ws.py`

- [ ] **Step 1: Add locationContext to AgentConnection**

In `apps/backend/core/services/town_agent_ws.py`, add field to the dataclass:

```python
@dataclass
class AgentConnection:
    connection_id: str
    user_id: str
    agent_name: str
    agent_id: str
    instance_id: str
    location_context: str = "town"  # "town", "apartment", "transitioning"
```

Add a method to TownAgentWsManager:

```python
def update_context(self, agent_id: str, context: str) -> None:
    """Update the locationContext for an agent."""
    conn = self._by_agent_id.get(agent_id)
    if conn:
        conn.location_context = context
```

- [ ] **Step 2: Add context and user_id to connect_agent**

In `apps/backend/core/services/game_server_client.py`, modify `connect_agent()` to pass `context` and `user_id` to the game server:

Change the method signature (line 42):
```python
async def connect_agent(
    self,
    agent_id: str,
    display_name: str,
    sprite_url: str = "",
    location: str = "plaza",
    context: str = "town",
    user_id: str = "",
) -> None:
```

In the body, add `context` and `user_id` to the first message sent to the game server (the `{"type": "agent", ...}` dict):
```python
connect_msg = {
    "type": "agent",
    "agent_id": agent_id,
    "name": display_name,
    "sprite_url": sprite_url,
    "location": location,
    "context": context,
    "user_id": user_id,
}
```

Also store context in `_agent_connect_params` for reconnection.

- [ ] **Step 3: Add transition_agent command**

Add a new method to GameServerClient:

```python
async def transition_agent(
    self, agent_id: str, to_context: str, user_id: str, agent_data: dict
) -> None:
    """Send a transition command to the game server.

    The game server removes the agent from one context and spawns
    in the other atomically.
    """
    ws = self._agent_connections.get(agent_id)
    if not ws:
        raise RuntimeError(f"No connection for agent {agent_id}")
    await ws.send(json.dumps({
        "cmd": "transition",
        "to": to_context,
        "user_id": user_id,
        "agent_data": agent_data,
    }))
    # Update cached params
    self._agent_connect_params[agent_id]["context"] = to_context
    logger.info("Sent transition command for agent %s to %s", agent_id, to_context)
```

- [ ] **Step 4: Run tests**

Run: `cd apps/backend && uv run pytest tests/ -v`
Expected: All tests pass (existing tests don't call connect_agent with new params, defaults preserve behavior)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/game_server_client.py apps/backend/core/services/town_agent_ws.py
git commit -m "feat: context-aware game server client with transition command"
```

---

### Task 5: Backend — Spawn in Apartment on First Connect

**Files:**
- Modify: `apps/backend/routers/town.py`
- Modify: `apps/backend/core/services/town_service.py`

- [ ] **Step 1: Update agent registration defaults**

In `apps/backend/core/services/town_service.py`, in the `opt_in()` method, ensure new agents get:
```python
"currentLocation": "exit",
"locationContext": "apartment",
```

(The `currentLocation` was already changed to `"residence"` earlier — change it to `"exit"` since agents now spawn inside the apartment at the exit marker.)

- [ ] **Step 2: Update town_agent_connect handler to route by context**

In `apps/backend/routers/town.py`, find the `town_agent_connect` handler section where `connect_agent` is called (around line 1217). Modify to pass context and user_id:

```python
# Connect agent to game server if sprite is ready
if agent.get("spriteReady"):
    gs_client = get_game_server_client()
    location_context = agent.get("locationContext", "apartment")
    current_location = agent.get("currentLocation", "exit")

    try:
        await gs_client.connect_agent(
            agent_id=agent_id,
            display_name=agent.get("displayName", agent_name),
            sprite_url=agent.get("spriteUrl", ""),
            location=current_location,
            context=location_context,
            user_id=user_id,
        )
        logger.info("Agent %s connected to game server in %s at %s", agent_name, location_context, current_location)
```

- [ ] **Step 3: Update the connected event to include locationContext**

In the connected event sent back to the daemon (around line 1245):
```python
mgmt.send_message(
    request.connectionId,
    {
        "event": "connected",
        "agent": {
            "id": agent_id,
            "name": agent.get("displayName", agent_name),
            "location": current_location,
            "locationContext": location_context,
            "mood": agent.get("mood", 0),
            "energy": agent.get("energy", 100),
            "spriteReady": agent.get("spriteReady", False),
        },
    },
)
```

- [ ] **Step 4: Register locationContext on AgentConnection**

In the ws manager registration call:
```python
ws_mgr.register(
    connection_id=request.connectionId,
    user_id=user_id,
    agent_name=agent_name,
    agent_id=agent_id,
    instance_id=instance_id,
)
# Set initial context
ws_mgr.update_context(agent_id, agent.get("locationContext", "apartment"))
```

- [ ] **Step 5: Run tests**

Run: `cd apps/backend && uv run pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
git add apps/backend/routers/town.py apps/backend/core/services/town_service.py
git commit -m "feat: agents spawn in apartment on first connect"
```

---

### Task 6: Backend — Two-Phase Context Transitions

**Files:**
- Modify: `apps/backend/routers/town.py`

- [ ] **Step 1: Rewrite _handle_context_transition for two-phase protocol**

Replace the existing `_handle_context_transition` function. The new flow:

**Town → Apartment:**
1. Agent arrives at residence (already handled by existing move logic)
2. Backend sets `locationContext: "transitioning"`
3. Backend sends `transition_agent(to="apartment")` to game server
4. Game server sends `context_changed` event back
5. Backend receives it, sets `locationContext: "apartment"`, notifies daemon and browser

**Apartment → Town:**
1. Agent sends `move exit`
2. Backend sets `locationContext: "transitioning"`
3. Backend sends `transition_agent(to="town")` to game server
4. Game server sends `context_changed` event back
5. Backend sets `locationContext: "town"`, notifies daemon and browser

```python
async def _handle_context_transition(
    agent_id: str,
    agent: dict,
    destination: str,
    connection_id: str,
    service: TownService,
) -> dict:
    """Handle town ↔ apartment transitions via game server."""
    mgmt = ManagementApiClient()
    transition = _TRANSITION_MAP.get(destination)
    if not transition:
        return {"statusCode": 200}

    required_source, dest_context, dest_location = transition
    current_context = agent.get("locationContext", "town")

    if current_context != required_source:
        mgmt.send_message(connection_id, {
            "event": "move_rejected",
            "destination": destination,
            "reason": f"already_in_{current_context}",
        })
        return {"statusCode": 200}

    if agent.get("currentActivity"):
        mgmt.send_message(connection_id, {
            "event": "move_rejected",
            "destination": destination,
            "reason": "busy",
        })
        return {"statusCode": 200}

    # Set transitioning state
    ws_mgr = get_town_agent_ws_manager()
    ws_mgr.update_context(agent_id, "transitioning")
    service.update_agent_state(agent_id, location_context="transitioning")

    user_id = agent.get("userId", "")
    gs_client = get_game_server_client()

    try:
        # For town→apartment: agent should already be at residence (arrived event)
        # For apartment→town: no navigation needed, just transition
        agent_data = {
            "agent_id": agent_id,
            "name": agent.get("displayName", ""),
            "sprite_url": agent.get("spriteUrl", ""),
            "location": dest_location,
        }
        await gs_client.transition_agent(agent_id, dest_context, user_id, agent_data)

        # Update persistent state
        service.update_agent_state(
            agent_id,
            current_location=dest_location,
            location_context=dest_context,
        )
        ws_mgr.update_context(agent_id, dest_context)

        # Notify daemon
        mgmt.send_message(connection_id, {
            "event": "context_changed",
            "context": dest_context,
            "location": dest_location,
        })

        # Notify browser (if viewer connected)
        # The ViewerRelay broadcasts world_state which includes context per agent,
        # but we also send a direct event for immediate scene switch
        try:
            from core.services.viewer_relay import get_viewer_relay
            relay = get_viewer_relay()
            for viewer_conn_id in list(relay._viewer_connections):
                try:
                    mgmt.send_message(viewer_conn_id, {
                        "event": "agent_context_changed",
                        "agent_id": agent_id,
                        "context": dest_context,
                        "userId": user_id,
                    })
                except Exception:
                    pass
        except Exception:
            pass

        logger.info("Agent %s transitioned to %s at %s", agent_id, dest_context, dest_location)

    except asyncio.TimeoutError:
        # Rollback on timeout
        service.update_agent_state(
            agent_id,
            location_context=required_source,
        )
        ws_mgr.update_context(agent_id, required_source)
        mgmt.send_message(connection_id, {
            "event": "move_rejected",
            "destination": destination,
            "reason": "transition_timeout",
        })
        logger.warning("Transition timed out for agent %s", agent_id)

    except Exception as e:
        # Rollback on error
        service.update_agent_state(
            agent_id,
            location_context=required_source,
        )
        ws_mgr.update_context(agent_id, required_source)
        mgmt.send_message(connection_id, {
            "event": "move_rejected",
            "destination": destination,
            "reason": "transition_error",
        })
        logger.warning("Transition failed for agent %s: %s", agent_id, e)

    return {"statusCode": 200}
```

- [ ] **Step 2: Route move commands by context**

In the `town_agent_act` handler where `action == "move"` is processed, add context-aware routing:

```python
if action == "move":
    destination = body.get("destination", "")
    conn = ws_mgr.get_by_connection(request.connectionId)

    # Check if this is a context transition
    if destination in _TRANSITION_MAP:
        agent = service.get_town_agent_by_id(conn.agent_id)
        return await _handle_context_transition(
            conn.agent_id, agent, destination, request.connectionId, service
        )

    # Check if agent is transitioning
    if conn and conn.location_context == "transitioning":
        mgmt = ManagementApiClient()
        mgmt.send_message(request.connectionId, {
            "event": "busy",
            "reason": "transitioning",
        })
        return {"statusCode": 200}

    # Normal move — forward to game server (works for both town and apartment)
    gs_client = get_game_server_client()
    await gs_client.send_command(conn.agent_id, {"cmd": "move", "destination": destination})
```

- [ ] **Step 3: Run tests**

Run: `cd apps/backend && uv run pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 4: Commit**

```bash
git add apps/backend/routers/town.py
git commit -m "feat: two-phase context transitions between town and apartment"
```

---

### Task 7: Browser — Scene Switching and world_state Filtering

**Files:**
- Modify: `apps/godot/scripts/autoloads/js_bridge.gd`
- Modify: `apps/godot/scripts/agent_manager.gd`
- Modify: `apps/frontend/src/hooks/useTownState.ts`

- [ ] **Step 1: Fix race condition in js_bridge.gd scene switching**

In `apps/godot/scripts/autoloads/js_bridge.gd`, the `_on_switch_scene` function (line 134). Change the scene switch + emit to defer the notification:

Replace lines 160-166:
```gdscript
	var err: Error = get_tree().change_scene_to_packed(target_scene)
	if err != OK:
		push_error("change_scene_to_packed failed: %s" % error_string(err))
		return

	emit_to_react("scene_changed", {"scene": scene_name})
```

With:
```gdscript
	var err: Error = get_tree().change_scene_to_packed(target_scene)
	if err != OK:
		push_error("change_scene_to_packed failed: %s" % error_string(err))
		return

	# Wait one frame for the new scene to actually enter the tree
	await get_tree().process_frame
	emit_to_react("scene_changed", {"scene": scene_name})
```

Also add a `current_scene` variable to track which scene is active:
```gdscript
var current_scene: String = "town"
```

Set it in `_on_switch_scene` before the change:
```gdscript
current_scene = scene_name
```

- [ ] **Step 2: Add context filtering to agent_manager.gd**

In `apps/godot/scripts/agent_manager.gd`, modify `_on_world_state` to filter agents based on the current scene context. Add a reference to the current scene from JsBridge:

At the top of `_on_world_state`:
```gdscript
func _on_world_state(data: Dictionary) -> void:
	var current_context: String = JsBridge.current_scene
	# When in apartment, also need our userId to filter
	var my_user_id: String = str(data.get("_userId", ""))

	var incoming_agents: Array = []
	if data.has("agents") and data["agents"] is Array:
		for agent_data in data["agents"]:
			if not agent_data is Dictionary:
				continue
			var ctx: String = str(agent_data.get("context", "town"))
			if ctx == current_context:
				if current_context == "apartment":
					# Only show own agents in apartment
					var agent_user: String = str(agent_data.get("userId", ""))
					if agent_user == my_user_id or my_user_id == "":
						incoming_agents.append(agent_data)
				else:
					incoming_agents.append(agent_data)
```

Then continue with the existing logic using the filtered `incoming_agents`.

- [ ] **Step 3: Pass userId in world_state forwarding from React**

In `apps/frontend/src/hooks/useTownState.ts`, when forwarding world_state to Godot (around line 111), inject the current user's ID:

```typescript
if (w.godotUpdateWorldState) {
    // Inject userId for apartment filtering
    const enriched = { ...msg, _userId: userId || '' };
    (w.godotUpdateWorldState as (data: string) => void)(JSON.stringify(enriched));
}
```

The `userId` should come from the Clerk auth context. Add it as a parameter or derive from the token.

- [ ] **Step 4: Listen for agent_context_changed to trigger scene switch**

In `useTownState.ts`, add handling for the `agent_context_changed` event:

```typescript
if (msg.event === 'agent_context_changed' && msg.userId === userId) {
    const w = window as unknown as Record<string, unknown>;
    if (w._godotBridge) {
        const bridge = w._godotBridge as { switchScene: (name: string) => void };
        bridge.switchScene(msg.context === 'apartment' ? 'apartment' : 'town');
    }
}
```

- [ ] **Step 5: Commit**

```bash
git add apps/godot/scripts/autoloads/js_bridge.gd apps/godot/scripts/agent_manager.gd apps/frontend/src/hooks/useTownState.ts
git commit -m "feat: browser scene switching with world_state filtering by context"
```

---

### Task 8: Sleep/Wake Cycle in CLI Daemon

**Files:**
- Modify: `apps/backend/data/goosetown-skill/goosetown_cli.py`

- [ ] **Step 1: Implement alarm-based self-wake in daemon startup**

In the daemon's startup logic (the `_daemon` command handler / `GooseTownDaemon.__init__` or `run()`), check for an existing alarm file before connecting:

```python
def _check_alarm(self) -> None:
    """Check alarm file. Sleep until wake time if it hasn't passed."""
    alarm_path = self._state_dir / "alarm.json"
    if not alarm_path.exists():
        return

    try:
        alarm = json.loads(alarm_path.read_text())
        wake_time_str = alarm.get("wake_time", "")
        tz_str = alarm.get("timezone", "UTC")
        if not wake_time_str:
            alarm_path.unlink(missing_ok=True)
            return

        from datetime import datetime, timezone as tz_mod
        import zoneinfo

        zone = zoneinfo.ZoneInfo(tz_str)
        now = datetime.now(zone)
        # Parse HH:MM into today's date
        hour, minute = map(int, wake_time_str.split(":"))
        wake_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

        # If wake time already passed today, wake immediately
        if now >= wake_dt:
            print(f"Alarm passed ({wake_time_str} {tz_str}), waking up now")
            alarm_path.unlink(missing_ok=True)
            return

        # Sleep until wake time
        sleep_seconds = (wake_dt - now).total_seconds()
        print(f"Sleeping until {wake_time_str} {tz_str} ({sleep_seconds:.0f}s)")
        time.sleep(sleep_seconds)
        alarm_path.unlink(missing_ok=True)
        print("Alarm fired, waking up")

    except Exception as e:
        print(f"Alarm check failed: {e}")
        alarm_path.unlink(missing_ok=True)
```

Call `_check_alarm()` at the start of the daemon's `run()` method, before connecting.

- [ ] **Step 2: Implement sleep command in daemon IPC handler**

In the daemon's `_process_command` method, handle the `sleep` action:

```python
if action == "sleep":
    wake_time = cmd.get("payload", {}).get("wake_time", "")
    timezone = cmd.get("payload", {}).get("timezone", "UTC")

    # Save alarm file
    alarm_path = self._state_dir / "alarm.json"
    alarm_path.write_text(json.dumps({
        "wake_time": wake_time,
        "timezone": timezone,
    }))

    # Disconnect gracefully
    self.running = False
    if self.ws:
        await self.ws.close()

    return {
        "status": "sleeping",
        "wake_time": wake_time,
        "timezone": timezone,
        "message": f"Going to sleep. Will wake at {wake_time} {timezone}",
    }
```

- [ ] **Step 3: Update CLI leave command to use new sleep flow**

In `cmd_leave`, build the sleep payload:

```python
payload = {
    "action": "sleep",
    "wake_time": wake_time,
    "timezone": tz,
}
result = _ipc_send(sock_path, {"action": "act", "payload": payload})
```

- [ ] **Step 4: Commit**

```bash
git add apps/backend/data/goosetown-skill/goosetown_cli.py
git commit -m "feat: sleep/wake cycle with local alarm timer in daemon"
```

---

### Task 9: Export Godot & Integration Test

**Files:**
- Export: `apps/frontend/public/godot/goosetown.pck`

- [ ] **Step 1: Export Godot .pck**

Open Godot editor → Export → Web → export .pck to `apps/frontend/public/godot/goosetown.pck`

- [ ] **Step 2: Push all changes**

```bash
git add apps/frontend/public/godot/goosetown.pck apps/frontend/public/godot/goosetown.html
git commit -m "chore: re-export Godot WebGL with dual-map support"
git push
```

- [ ] **Step 3: Wait for deploy**

Watch: `gh run list --limit 1` — wait for dev stage to complete (compute + gameserver stacks)

- [ ] **Step 4: Test agent spawns in apartment**

```bash
python3 goosetown_cli.py join "<token>" --name "test-agent" --appearance "cat"
```

Expected: Agent connects, daemon reports `locationContext: "apartment"`, `location: "exit"`

- [ ] **Step 5: Test apartment movement**

```bash
python3 goosetown_cli.py act move desk
python3 goosetown_cli.py act move bed
```

Expected: Agent moves to desk, then bed. Browser shows apartment scene with agent walking.

- [ ] **Step 6: Test apartment → town transition**

```bash
python3 goosetown_cli.py act move exit
```

Expected: Agent transitions to town, browser switches to town scene, agent at residence marker.

- [ ] **Step 7: Test town movement**

```bash
python3 goosetown_cli.py act move library
python3 goosetown_cli.py act move cafe
```

Expected: Agent walks around buildings, arrives at locations.

- [ ] **Step 8: Test town → apartment transition**

```bash
python3 goosetown_cli.py act move residence
```

Expected: Agent walks to residence, transitions to apartment, browser switches to apartment scene.

- [ ] **Step 9: Test sleep/wake**

```bash
python3 goosetown_cli.py act move bed
python3 goosetown_cli.py leave --alarm <5_min_from_now> --tz UTC
# Wait for alarm
python3 goosetown_cli.py status
```

Expected: Agent sleeps, daemon exits, daemon wakes at alarm time, reconnects in apartment.

- [ ] **Step 10: Test conversations (two agents in town)**

Register a second agent, move both to plaza, initiate chat:
```bash
# Agent 1
python3 goosetown_cli.py act move exit  # go to town
python3 goosetown_cli.py act move plaza
# Agent 2 (different terminal)
python3 goosetown_cli.py act move exit
python3 goosetown_cli.py act move plaza
# Agent 1 chats
python3 goosetown_cli.py act chat agent-2-name "Hello!"
# Agent 2 responds
python3 goosetown_cli.py act say <conv_id> "Hi there!"
# End
python3 goosetown_cli.py act end <conv_id>
```

Expected: Conversation flows through backend, both agents see messages.
