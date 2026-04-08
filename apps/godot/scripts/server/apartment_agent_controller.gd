## Manages agent nodes inside a single user's apartment instance.
##
## Same interface as AgentController but for apartment locations.
## No proximity detection (private space). Locations from ApartmentMap markers.
class_name ApartmentAgentController
extends Node

const AgentScene: PackedScene = preload("res://scenes/agent.tscn")
const TILE_SIZE: float = 32.0
const DESTINATION_JITTER: float = 12.0  # Smaller jitter for indoor space
## Seconds between periodic agent_state broadcasts
const STATE_INTERVAL: float = 2.0

signal send_to_peer(peer_id: int, data: Dictionary)

var user_id: String = ""
var _agents: Dictionary = {}        # peer_id -> agent node
var _peer_to_id: Dictionary = {}    # peer_id -> agent_id
var _id_to_peer: Dictionary = {}    # agent_id -> peer_id
var _locations: Array[Marker2D] = []
var _arrived_at: Dictionary = {}
var _agent_activity: Dictionary = {}  # peer_id -> activity name
var _agent_speech: Dictionary = {}
var _speech_timers: Dictionary = {}
const SPEECH_DURATION: float = 5.0
## Accumulator for periodic state broadcast
var _state_timer: float = 0.0


func initialize(location_markers: Array[Marker2D], owner_user_id: String) -> void:
	_locations = location_markers
	user_id = owner_user_id


func spawn_agent(peer_id: int, data: Dictionary) -> void:
	if _agents.has(peer_id):
		push_warning("ApartmentAgentController: peer %d already has an agent" % peer_id)
		return

	# Deduplicate by agent_id: if this agent exists on a stale peer, remove it first
	var agent_id_check: String = str(data.get("agent_id", ""))
	if agent_id_check != "" and _id_to_peer.has(agent_id_check):
		var old_peer: int = _id_to_peer[agent_id_check]
		if old_peer != peer_id:
			print("ApartmentAgentController: removing stale peer %d for agent %s" % [old_peer, agent_id_check])
			remove_agent(old_peer)

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
		"position": { "x": spawn_pos.x / TILE_SIZE, "y": spawn_pos.y / TILE_SIZE },
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
	_agent_speech.erase(peer_id)
	_speech_timers.erase(peer_id)
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
		"get_locations":
			_handle_get_locations(peer_id)
		"set_activity":
			var activity_name: String = str(data.get("activity", ""))
			_agent_activity[peer_id] = activity_name
			send_to_peer.emit(peer_id, {"event": "activity_set", "activity": activity_name})
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
		"update_agent_sprites":
			var agent_node: CharacterBody2D = _agents[peer_id]
			var new_anims: Dictionary = data.get("sprite_animations", {})
			if new_anims.is_empty():
				return
			agent_node.sprite_animations = new_anims
			print("[ApartmentAgentController] update_agent_sprites peer=%d count=%d" % [peer_id, new_anims.size()])
		_:
			send_to_peer.emit(peer_id, {
				"event": "command_error",
				"error": "Unknown command: %s" % cmd,
				"cmd": cmd,
			})


func get_world_state_for_viewer() -> Dictionary:
	## Return dict with 'agents' array, matching AgentController interface.
	var agents_arr: Array = []
	for peer_id in _agents.keys():
		var agent_node: CharacterBody2D = _agents[peer_id]
		var agent_id: String = _peer_to_id.get(peer_id, "")
		var loc: String = _get_agent_location(agent_node.global_position)
		agents_arr.append({
			"id": agent_id,
			"name": agent_node.display_name,
			"x": agent_node.global_position.x,
			"y": agent_node.global_position.y,
			"facing": agent_node.facing,
			"is_moving": not agent_node.nav_agent.is_navigation_finished(),
			"location": loc,
			"activity": _agent_activity.get(peer_id, ""),
			"speechText": _agent_speech.get(peer_id, ""),
			"spriteUrl": agent_node.sprite_url,
			"spriteAnimations": agent_node.sprite_animations,
			"context": "apartment",
			"userId": user_id,
		})
	return {"agents": agents_arr}


func get_peer_ids() -> Array:
	return _agents.keys()


func has_peer(peer_id: int) -> bool:
	return _agents.has(peer_id)


func get_agent_data(peer_id: int) -> Dictionary:
	## Return agent info for transition. Empty dict if peer not found.
	if not _agents.has(peer_id):
		return {}
	var agent_node: CharacterBody2D = _agents[peer_id]
	return {
		"agent_id": _peer_to_id.get(peer_id, ""),
		"name": agent_node.display_name,
		"sprite_url": agent_node.sprite_url,
		"facing": agent_node.facing,
	}


func is_empty() -> bool:
	return _agents.is_empty()


func _process(delta: float) -> void:
	if _agents.is_empty():
		return

	_check_arrivals()

	# Tick speech timers
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

	send_to_peer.emit(peer_id, {
		"event": "idle",
		"location": _get_agent_location(agent_node.global_position),
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


func _broadcast_agent_states() -> void:
	for peer_id in _agents.keys():
		var agent_node: CharacterBody2D = _agents[peer_id]
		var location_id: String = _get_agent_location(agent_node.global_position)
		var speed: float = agent_node.velocity.length() / TILE_SIZE

		var available: Array = []
		for marker in _locations:
			var loc_id: String = marker.name.to_snake_case()
			if loc_id != location_id:
				available.append({ "type": "move", "location": loc_id })
		available.append({ "type": "idle" })

		send_to_peer.emit(peer_id, {
			"event": "agent_state",
			"position": {
				"x": agent_node.global_position.x / TILE_SIZE,
				"y": agent_node.global_position.y / TILE_SIZE,
			},
			"location": location_id,
			"facing": agent_node.facing,
			"speed": speed,
			"context": "apartment",
			"userId": user_id,
			"activity": "idle" if speed < 0.1 else "walking",
			"available_actions": available,
			"environment": {
				"location_id": location_id,
			},
		})


func _check_arrivals() -> void:
	for peer_id in _agents.keys():
		var agent_node: CharacterBody2D = _agents[peer_id]

		var nav_finished: bool = agent_node.nav_agent.is_navigation_finished()
		var speed: float = agent_node.velocity.length()
		var stopped: bool = speed < 1.0
		var dist_to_target: float = agent_node.global_position.distance_to(agent_node.nav_agent.target_position)

		# Debug: log every 60 frames (~1s at 60fps) to see why arrived never fires
		if Engine.get_process_frames() % 60 == 0:
			print("[APT_ARRIVAL] peer=%d nav_finished=%s speed=%.1f dist=%.1f pos=(%.0f,%.0f) target=(%.0f,%.0f)" % [
				peer_id, nav_finished, speed, dist_to_target,
				agent_node.global_position.x, agent_node.global_position.y,
				agent_node.nav_agent.target_position.x, agent_node.nav_agent.target_position.y,
			])

		if nav_finished and stopped:
			var loc: String = _get_agent_location(agent_node.global_position)
			if _arrived_at.get(peer_id, "") != loc:
				_arrived_at[peer_id] = loc
				send_to_peer.emit(peer_id, {
					"event": "arrived",
					"location": loc,
					"position": {
						"x": agent_node.global_position.x / TILE_SIZE,
						"y": agent_node.global_position.y / TILE_SIZE,
					},
				})
		else:
			# Agent is moving -- clear arrival state so next stop triggers arrived
			if _arrived_at.has(peer_id) and not nav_finished:
				_arrived_at.erase(peer_id)


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
