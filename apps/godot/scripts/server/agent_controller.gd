## Manages agent nodes in the scene tree for the headless game server.
##
## Handles spawning, removal, movement commands, arrival/proximity detection,
## and periodic state broadcasts. Emits events to peers via send_to_peer signal.
class_name AgentController
extends Node

const AgentScene: PackedScene = preload("res://scenes/agent.tscn")

## 3 tiles * 32px per tile
const PROXIMITY_THRESHOLD: float = 96.0
## Seconds between periodic agent_state broadcasts
const STATE_INTERVAL: float = 2.0
## Pixels per tile for coordinate conversion
const TILE_SIZE: float = 32.0
## Random offset range (pixels) around location markers so agents don't stack
const DESTINATION_JITTER: float = 24.0

## Emitted when the controller needs to send data to a specific peer.
## game_server.gd connects this to protocol.send_to.
signal send_to_peer(peer_id: int, data: Dictionary)

## peer_id → agent node (CharacterBody2D)
var _agents: Dictionary = {}
## peer_id → agent_id string
var _peer_to_id: Dictionary = {}
## agent_id string → peer_id
var _id_to_peer: Dictionary = {}
## Marker2D nodes for named locations
var _locations: Array[Marker2D] = []
## peer_id → location_id (dedup arrivals — only emit when changed)
var _arrived_at: Dictionary = {}
## "peerA:peerB" → distance (proximity change detection)
var _prev_nearby: Dictionary = {}
## Accumulator for periodic state broadcast
var _state_timer: float = 0.0
## peer_id → user_id (for tagging agents in world_state)
var _peer_to_user: Dictionary = {}
## peer_id -> activity name (empty string = no activity)
var _agent_activity: Dictionary = {}
## peer_id -> speech text (empty string = no speech)
var _agent_speech: Dictionary = {}
## peer_id -> seconds remaining before speech clears
var _speech_timers: Dictionary = {}
## How long speech bubbles stay visible
const SPEECH_DURATION: float = 5.0


func initialize(location_markers: Array[Marker2D]) -> void:
	_locations = location_markers


func set_user_id(peer_id: int, uid: String) -> void:
	_peer_to_user[peer_id] = uid


func spawn_agent(peer_id: int, data: Dictionary) -> void:
	if _agents.has(peer_id):
		push_warning("AgentController: peer %d already has an agent" % peer_id)
		return

	# Deduplicate by agent_id: if this agent exists on a stale peer, remove it first
	var agent_id_check: String = str(data.get("agent_id", ""))
	if agent_id_check != "" and _id_to_peer.has(agent_id_check):
		var old_peer: int = _id_to_peer[agent_id_check]
		if old_peer != peer_id:
			print("AgentController: removing stale peer %d for agent %s" % [old_peer, agent_id_check])
			remove_agent(old_peer)

	var agent_node: CharacterBody2D = AgentScene.instantiate()

	# Determine spawn position from requested location
	var location_id: String = str(data.get("location", "plaza"))
	var marker: Marker2D = _find_location(location_id)
	var spawn_pos: Vector2 = marker.global_position if marker else Vector2(576.0, 384.0)
	spawn_pos = _offset_if_occupied(spawn_pos)
	print("AgentController.spawn_agent: location=%s marker=%s pos=(%.0f,%.0f) locations_count=%d" % [
		location_id, "FOUND" if marker else "NOT_FOUND", spawn_pos.x, spawn_pos.y, _locations.size()
	])

	var agent_id: String = str(data.get("agent_id", "agent_%d" % peer_id))
	var display: String = str(data.get("name", agent_id))
	# JSON null becomes Godot null; str(null) == "<null>" which agent.setup
	# would treat as a real URL. Coerce to "" so the shadow fallback kicks in.
	var _sprite_url_raw: Variant = data.get("sprite_url")
	var sprite_url: String = "" if _sprite_url_raw == null else str(_sprite_url_raw)
	var facing_str: String = str(data.get("facing", ""))

	agent_node.setup(agent_id, sprite_url, spawn_pos.x, spawn_pos.y, facing_str, display)
	agent_node._use_navigation = true

	_agents[peer_id] = agent_node
	_peer_to_id[peer_id] = agent_id
	_id_to_peer[agent_id] = peer_id
	_arrived_at[peer_id] = location_id

	# Store sprite animation metadata for world_state broadcast
	var sprite_anims: Dictionary = data.get("sprite_animations", {})
	if not sprite_anims.is_empty():
		agent_node.sprite_animations = sprite_anims

	# add_child triggers _ready() which initializes @onready vars (nav_agent, etc).
	# We MUST add_child before touching agent_node.nav_agent — otherwise it's Nil
	# and silently corrupts the agent, causing _check_arrivals to crash every frame.
	add_child(agent_node)

	# Set nav target to spawn position so NavigationAgent2D considers the
	# agent "arrived" at the correct location rather than targeting (0,0).
	agent_node.nav_agent.target_position = spawn_pos

	send_to_peer.emit(peer_id, {
		"event": "spawned",
		"agent_id": agent_id,
		"location": location_id,
		"position": { "x": spawn_pos.x / TILE_SIZE, "y": spawn_pos.y / TILE_SIZE },
	})


## Avoid two agents landing on the exact same tile when spawning / arriving.
## If another agent is already within ~1.5 tiles of the intended position,
## offset the spawn point around a 6-slot circle so each gets a distinct
## cell. Deterministic per-count so successive arrivals don't collide with
## each other either.
##
## ``exclude_peer`` skips one agent from the collision count — used by
## arrival dedup so an already-placed agent doesn't think it's colliding
## with itself when checked at its own position.
func _offset_if_occupied(pos: Vector2, exclude_peer: int = -1) -> Vector2:
	var radius: float = TILE_SIZE * 1.5
	var collisions: int = 0
	for peer in _agents.keys():
		if peer == exclude_peer:
			continue
		var other: CharacterBody2D = _agents[peer]
		if other == null or not is_instance_valid(other):
			continue
		if other.global_position.distance_to(pos) < radius:
			collisions += 1
	if collisions == 0:
		return pos
	var angle: float = float(collisions) * TAU / 6.0
	return pos + Vector2(cos(angle), sin(angle)) * TILE_SIZE * 1.2


func remove_agent(peer_id: int) -> void:
	if not _agents.has(peer_id):
		return

	var agent_node: CharacterBody2D = _agents[peer_id]
	var agent_id: String = _peer_to_id.get(peer_id, "")

	# Clean up proximity tracking for this peer
	var keys_to_erase: Array = []
	for key in _prev_nearby.keys():
		var k: String = str(key)
		if k.begins_with(str(peer_id) + ":") or k.ends_with(":" + str(peer_id)):
			keys_to_erase.append(key)
	for key in keys_to_erase:
		_prev_nearby.erase(key)

	_agents.erase(peer_id)
	_peer_to_id.erase(peer_id)
	if agent_id != "":
		_id_to_peer.erase(agent_id)
	_arrived_at.erase(peer_id)
	_peer_to_user.erase(peer_id)
	_agent_activity.erase(peer_id)
	_agent_speech.erase(peer_id)
	_speech_timers.erase(peer_id)

	agent_node.queue_free()


func handle_command(peer_id: int, data: Dictionary) -> void:
	if not _agents.has(peer_id):
		send_to_peer.emit(peer_id, {
			"event": "command_error",
			"error": "No agent spawned for this peer",
		})
		return

	var cmd: String = str(data.get("cmd", ""))
	match cmd:
		"move":
			_handle_move(peer_id, data)
		"idle":
			_handle_idle(peer_id)
		"get_locations":
			_handle_get_locations(peer_id)
		"set_activity":
			_handle_set_activity(peer_id, data)
		"set_speech":
			var text: String = str(data.get("text", ""))
			_agent_speech[peer_id] = text
			if text != "":
				_speech_timers[peer_id] = SPEECH_DURATION
			else:
				_speech_timers.erase(peer_id)
		"set_facing":
			var direction: String = str(data.get("direction", ""))
			if direction in ["south", "west", "east", "north"]:
				var agent_node: CharacterBody2D = _agents[peer_id]
				agent_node.facing = direction
		"set_conversation_group":
			var ids: Array = data.get("agent_ids", [])
			face_agents_toward_centroid(ids)
		"update_agent_sprites":
			var agent_node: CharacterBody2D = _agents[peer_id]
			var new_anims: Dictionary = data.get("sprite_animations", {})
			if new_anims.is_empty():
				return
			agent_node.sprite_animations = new_anims
			print("[AgentController] update_agent_sprites peer=%d count=%d" % [peer_id, new_anims.size()])
		_:
			send_to_peer.emit(peer_id, {
				"event": "command_error",
				"error": "Unknown command: %s" % cmd,
				"cmd": cmd,
			})


func face_agents_toward_centroid(agent_ids: Array) -> void:
	## Face each agent in the list toward the centroid of the others.
	if agent_ids.size() < 2:
		return
	var id_to_node: Dictionary = {}
	for aid in agent_ids:
		if _id_to_peer.has(aid):
			var peer: int = _id_to_peer[aid]
			if _agents.has(peer):
				id_to_node[aid] = _agents[peer]
	if id_to_node.size() < 2:
		return
	for aid in id_to_node:
		var agent_node: CharacterBody2D = id_to_node[aid]
		var centroid: Vector2 = Vector2.ZERO
		var count: int = 0
		for other_aid in id_to_node:
			if other_aid != aid:
				centroid += id_to_node[other_aid].global_position
				count += 1
		centroid /= count
		var delta: Vector2 = centroid - agent_node.global_position
		if abs(delta.x) > abs(delta.y):
			agent_node.facing = "east" if delta.x > 0 else "west"
		else:
			agent_node.facing = "south" if delta.y > 0 else "north"


func get_world_state_for_viewer() -> Dictionary:
	var agents_arr: Array = []
	for peer_id in _agents.keys():
		var agent_node: CharacterBody2D = _agents[peer_id]
		var agent_id: String = _peer_to_id.get(peer_id, "")
		var location_id: String = _get_agent_location(agent_node.global_position)
		agents_arr.append({
			"id": agent_id,
			"name": agent_node.display_name,
			"x": agent_node.global_position.x,
			"y": agent_node.global_position.y,
			"facing": agent_node.facing,
			"is_moving": not agent_node.nav_agent.is_navigation_finished(),
			"location": location_id,
			"activity": _agent_activity.get(peer_id, ""),
			"speechText": _agent_speech.get(peer_id, ""),
			"spriteUrl": agent_node.sprite_url,
			"spriteAnimations": agent_node.sprite_animations,
			"context": "town",
			"userId": _peer_to_user.get(peer_id, ""),
		})
	return { "agents": agents_arr }


func get_agent_data(peer_id: int) -> Dictionary:
	## Return agent info for transition. Empty dict if peer not found.
	if not _agents.has(peer_id):
		return {}
	var agent_node: CharacterBody2D = _agents[peer_id]
	return {
		"agent_id": _peer_to_id.get(peer_id, ""),
		"name": agent_node.display_name,
		"sprite_url": agent_node.sprite_url,
		"sprite_animations": agent_node.sprite_animations,
		"facing": agent_node.facing,
	}


func get_user_id(peer_id: int) -> String:
	return _peer_to_user.get(peer_id, "")


func has_peer(peer_id: int) -> bool:
	return _agents.has(peer_id)


func _process(delta: float) -> void:
	if _agents.is_empty():
		return

	_check_arrivals()
	_check_proximity()

	# Tick speech timers and clear expired speech
	var expired_peers: Array = []
	for peer_id in _speech_timers.keys():
		_speech_timers[peer_id] -= delta
		if _speech_timers[peer_id] <= 0.0:
			expired_peers.append(peer_id)
	for peer_id in expired_peers:
		_speech_timers.erase(peer_id)
		_agent_speech.erase(peer_id)

	_state_timer += delta
	if _state_timer >= STATE_INTERVAL:
		_state_timer -= STATE_INTERVAL
		_broadcast_agent_states()


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

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

	# Jitter destination so multiple agents don't stack on the exact same pixel
	var jitter := Vector2(
		randf_range(-DESTINATION_JITTER, DESTINATION_JITTER),
		randf_range(-DESTINATION_JITTER, DESTINATION_JITTER),
	)
	var raw_target := marker.global_position + jitter
	# Clamp to navmesh so jitter near buildings doesn't land outside walkable area
	var map_rid: RID = agent_node.nav_agent.get_navigation_map()
	var target_pos: Vector2 = NavigationServer2D.map_get_closest_point(map_rid, raw_target)
	agent_node.set_navigation_target(target_pos)

	# Clear arrival state so we can detect arrival at new destination
	_arrived_at.erase(peer_id)

	send_to_peer.emit(peer_id, {
		"event": "move_started",
		"location": location_id,
		"target": { "x": target_pos.x / TILE_SIZE, "y": target_pos.y / TILE_SIZE },
	})


func _handle_idle(peer_id: int) -> void:
	var agent_node: CharacterBody2D = _agents[peer_id]
	agent_node.velocity = Vector2.ZERO
	# Reset navigation so the agent stops
	agent_node.set_navigation_target(agent_node.global_position)

	send_to_peer.emit(peer_id, {
		"event": "idle",
		"location": _get_agent_location(agent_node.global_position),
	})


func _handle_set_activity(peer_id: int, data: Dictionary) -> void:
	var activity_name: String = str(data.get("activity", ""))
	_agent_activity[peer_id] = activity_name

	send_to_peer.emit(peer_id, {
		"event": "activity_set",
		"activity": activity_name,
	})


func _handle_get_locations(peer_id: int) -> void:
	var locs: Array = []
	for marker in _locations:
		var loc_id: String = marker.name.to_snake_case()
		locs.append({
			"id": loc_id,
			"x": marker.global_position.x / TILE_SIZE,
			"y": marker.global_position.y / TILE_SIZE,
		})
	send_to_peer.emit(peer_id, {
		"event": "locations",
		"locations": locs,
	})


# ---------------------------------------------------------------------------
# Arrival detection
# ---------------------------------------------------------------------------

func _check_arrivals() -> void:
	for peer_id in _agents.keys():
		var agent_node: CharacterBody2D = _agents[peer_id]

		var nav_finished: bool = agent_node.nav_agent.is_navigation_finished()
		var stopped: bool = agent_node.velocity.length() < 1.0

		if nav_finished and stopped:
			var current_loc: String = _get_agent_location(agent_node.global_position)
			var prev_loc: String = _arrived_at.get(peer_id, "")

			# Only emit arrived if location changed (dedup)
			if current_loc != prev_loc:
				# F2: if another agent is already sitting on (or within 1.5
				# tiles of) this spot, nudge ourselves to an offset slot.
				# PR #164 handled the spawn path, but two agents pathfinding
				# to the same destination from different directions could
				# still land on identical tiles because both nav-finished in
				# the same physics tick.
				var dedup_pos: Vector2 = _offset_if_occupied(
					agent_node.global_position, peer_id
				)
				if dedup_pos != agent_node.global_position:
					agent_node.global_position = dedup_pos
					agent_node.nav_agent.target_position = dedup_pos

				_arrived_at[peer_id] = current_loc
				send_to_peer.emit(peer_id, {
					"event": "arrived",
					"location": current_loc,
					"position": {
						"x": agent_node.global_position.x / TILE_SIZE,
						"y": agent_node.global_position.y / TILE_SIZE,
					},
				})
		else:
			# Agent is moving — clear arrival state so next stop triggers arrived
			if _arrived_at.has(peer_id) and not nav_finished:
				_arrived_at.erase(peer_id)


# ---------------------------------------------------------------------------
# Proximity detection
# ---------------------------------------------------------------------------

func _check_proximity() -> void:
	var peer_ids: Array = _agents.keys()
	var current_nearby: Dictionary = {}

	for i in range(peer_ids.size()):
		for j in range(i + 1, peer_ids.size()):
			var pid_a: int = peer_ids[i]
			var pid_b: int = peer_ids[j]
			var agent_a: CharacterBody2D = _agents[pid_a]
			var agent_b: CharacterBody2D = _agents[pid_b]
			var dist: float = agent_a.global_position.distance_to(agent_b.global_position)

			var key: String = "%d:%d" % [pid_a, pid_b]

			if dist <= PROXIMITY_THRESHOLD:
				current_nearby[key] = dist
				# Emit nearby only on change (was not nearby before)
				if not _prev_nearby.has(key):
					var tile_dist: float = dist / TILE_SIZE
					var id_a: String = _peer_to_id.get(pid_a, "")
					var id_b: String = _peer_to_id.get(pid_b, "")
					send_to_peer.emit(pid_a, {
						"event": "nearby",
						"agent_id": id_b,
						"agent_name": agent_b.display_name,
						"distance": tile_dist,
					})
					send_to_peer.emit(pid_b, {
						"event": "nearby",
						"agent_id": id_a,
						"agent_name": agent_a.display_name,
						"distance": tile_dist,
					})
			else:
				# Emit nearby_left only on change (was nearby before)
				if _prev_nearby.has(key):
					var id_a: String = _peer_to_id.get(pid_a, "")
					var id_b: String = _peer_to_id.get(pid_b, "")
					send_to_peer.emit(pid_a, {
						"event": "nearby_left",
						"agent_id": id_b,
						"agent_name": agent_b.display_name,
					})
					send_to_peer.emit(pid_b, {
						"event": "nearby_left",
						"agent_id": id_a,
						"agent_name": agent_a.display_name,
					})

	_prev_nearby = current_nearby


# ---------------------------------------------------------------------------
# Periodic state broadcast
# ---------------------------------------------------------------------------

func _broadcast_agent_states() -> void:
	for peer_id in _agents.keys():
		var agent_node: CharacterBody2D = _agents[peer_id]
		var location_id: String = _get_agent_location(agent_node.global_position)
		var nearby: Array = _get_nearby_for(peer_id)
		var available: Array = _get_available_actions(location_id, peer_id)

		var speed: float = agent_node.velocity.length() / TILE_SIZE

		send_to_peer.emit(peer_id, {
			"event": "agent_state",
			"position": {
				"x": agent_node.global_position.x / TILE_SIZE,
				"y": agent_node.global_position.y / TILE_SIZE,
			},
			"location": location_id,
			"facing": agent_node.facing,
			"speed": speed,
			"context": "town",
			"userId": _peer_to_user.get(peer_id, ""),
			"activity": _agent_activity.get(peer_id, "idle" if speed < 0.1 else "walking"),
			"nearby_agents": nearby,
			"available_actions": available,
			"environment": {
				"location_id": location_id,
			},
		})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

func _find_location(location_id: String) -> Marker2D:
	var search: String = location_id.to_snake_case()
	for marker in _locations:
		if marker.name.to_snake_case() == search:
			return marker
	return null


func _get_agent_location(agent_pos: Vector2) -> String:
	var closest_id: String = "unknown"
	var closest_dist: float = INF

	for marker in _locations:
		var dist: float = agent_pos.distance_to(marker.global_position)
		if dist < closest_dist:
			closest_dist = dist
			closest_id = marker.name.to_snake_case()

	return closest_id


func _get_nearby_for(peer_id: int) -> Array:
	if not _agents.has(peer_id):
		return []

	var agent_node: CharacterBody2D = _agents[peer_id]
	var result: Array = []

	for other_pid in _agents.keys():
		if other_pid == peer_id:
			continue
		var other_node: CharacterBody2D = _agents[other_pid]
		var dist: float = agent_node.global_position.distance_to(other_node.global_position)
		if dist <= PROXIMITY_THRESHOLD:
			result.append({
				"agent_id": _peer_to_id.get(other_pid, ""),
				"name": other_node.display_name,
				"distance": dist / TILE_SIZE,
				"facing": other_node.facing,
			})

	return result


func _get_available_actions(location_id: String, peer_id: int) -> Array:
	var actions: Array = []

	# Move destinations: all locations except current
	for marker in _locations:
		var loc_id: String = marker.name.to_snake_case()
		if loc_id != location_id:
			actions.append({
				"type": "move",
				"location": loc_id,
			})

	# Idle action always available
	actions.append({ "type": "idle" })

	# Interact with nearby agents
	var nearby: Array = _get_nearby_for(peer_id)
	for agent_info in nearby:
		actions.append({
			"type": "interact",
			"agent_id": agent_info["agent_id"],
			"agent_name": agent_info["name"],
		})

	return actions
