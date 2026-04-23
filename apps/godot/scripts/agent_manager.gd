## Manages all agents on the map.
## Supports two modes:
##   1. ViewerProtocol — receives world_state events via WebSocket (preferred)
##   2. JsBridge signals — legacy spawn/move/remove from React (fallback)
extends Node

const AgentScene: PackedScene = preload("res://scenes/agent.tscn")

## Single source of truth: agent_id -> Agent node.
## (Previously split across `agents` and `_known_agents`; consolidated for clarity.)
var agents: Dictionary = {}
var _viewer: ViewerProtocol = null
var _sprite_callback = null


func _ready() -> void:
	# Connect to JS bridge signals (legacy fallback)
	JsBridge.spawn_agent_requested.connect(_on_spawn_agent)
	JsBridge.move_agent_requested.connect(_on_move_agent)
	JsBridge.remove_agent_requested.connect(_on_remove_agent)
	JsBridge.update_agent_state_requested.connect(_on_update_agent_state)

	# Register sprite update callback for web
	if OS.has_feature("web"):
		_sprite_callback = JavaScriptBridge.create_callback(_on_sprite_loaded)
		var window: JavaScriptObject = JavaScriptBridge.get_interface("window")
		window.set("godotUpdateAgentSprite", _sprite_callback)

	# Listen for world_state forwarded from React via JS bridge
	JsBridge.world_state_received.connect(_on_world_state)


## Connect to a ViewerProtocol instance to receive world_state events.
func setup_viewer(viewer_protocol: ViewerProtocol) -> void:
	_viewer = viewer_protocol
	_viewer.world_state_received.connect(_on_world_state)
	print("AgentManager: Viewer protocol connected")


func _on_world_state(data: Dictionary) -> void:
	var current_context: String = JsBridge.current_scene
	# userId injected by React for apartment filtering
	var my_user_id: String = str(data.get("_userId", ""))

	var total_count: int = 0
	var incoming_agents: Array = []
	if data.has("agents") and data["agents"] is Array:
		total_count = data["agents"].size()
		for agent_data in data["agents"]:
			if not agent_data is Dictionary:
				continue
			var ctx: String = str(agent_data.get("context", "town"))
			# In town: show all town-context agents
			# In apartment: show only own agents (matching userId)
			if ctx != current_context:
				continue
			if current_context == "apartment" and my_user_id != "":
				var agent_user: String = str(agent_data.get("userId", ""))
				if agent_user != my_user_id:
					continue
			incoming_agents.append(agent_data)

	# Avoid console spam — only log when agent count changes
	var _prev_count: int = agents.size()
	if incoming_agents.size() != _prev_count:
		print("[AgentManager] world_state: %d agents (filtered from %d, context=%s)" % [incoming_agents.size(), total_count, current_context])

	# Track which agent IDs are present in this update
	var seen_ids: Dictionary = {}

	for agent_data in incoming_agents:
		if not agent_data is Dictionary:
			continue

		var agent_id: String = str(agent_data.get("id", ""))
		if agent_id == "":
			continue

		seen_ids[agent_id] = true

		var x: float = float(agent_data.get("x", 0))
		var y: float = float(agent_data.get("y", 0))

		if agent_id in agents:
			# Existing agent — use navmesh pathfinding so agents walk around buildings.
			# Skip if the new target is within 2px (distance_squared < 4) to avoid
			# unnecessary navmesh recalculations every world_state broadcast.
			var agent_node: CharacterBody2D = agents[agent_id]
			var new_target: Vector2 = Vector2(x, y)
			if agent_node.target_position.distance_squared_to(new_target) > 4.0:
				agent_node.set_navigation_target(new_target)

			# Update speech/state if present
			if agent_data.has("speechText") or agent_data.has("speech"):
				agents[agent_id].update_state(agent_data)

			# Update activity state
			var activity: String = str(agent_data.get("activity", ""))
			if activity != "walking" and activity != "idle":
				agents[agent_id].current_activity = activity
			else:
				agents[agent_id].current_activity = ""

			# Sync facing from world_state. Walking agents derive facing from
			# velocity via `_update_facing_from_velocity`; non-walking agents
			# (talking, idle, sitting) need the explicit value pushed by the
			# game server — `_handle_chat` calls set_facing on both parties
			# so they look at each other, but without this sync the viewer
			# stuck at whatever facing it had at spawn time.
			var incoming_facing: String = str(agent_data.get("facing", ""))
			if incoming_facing in ["south", "west", "east", "north"]:
				var is_walking: bool = bool(agent_data.get("is_moving", false))
				if not is_walking:
					agent_node.facing = incoming_facing

			# Diff sprite animations — reload any that are new or whose URL
			# changed, AND re-apply animation configs when fps/frameCount
			# shift (e.g. a runtime fps tweak from backend). Without the
			# config sync, fps changes on talking never reach Godot because
			# the URL stays the same.
			var incoming_anims: Dictionary = agent_data.get("spriteAnimations", {})
			if not incoming_anims.is_empty():
				var current_anims: Dictionary = agent_node.sprite_animations
				var url_changed: bool = false
				var config_changed: bool = false
				for anim_name in incoming_anims:
					var new_url: String = str(incoming_anims[anim_name].get("url", ""))
					var old_url: String = ""
					var new_fps: float = float(incoming_anims[anim_name].get("fps", 0))
					var new_frames: int = int(incoming_anims[anim_name].get("frameCount", 0))
					var old_fps: float = 0.0
					var old_frames: int = 0
					if current_anims.has(anim_name):
						old_url = str(current_anims[anim_name].get("url", ""))
						old_fps = float(current_anims[anim_name].get("fps", 0))
						old_frames = int(current_anims[anim_name].get("frameCount", 0))
					if new_url != "" and new_url != old_url:
						agent_node.load_sprite(new_url, anim_name)
						url_changed = true
					if new_fps != old_fps or new_frames != old_frames:
						config_changed = true
				if url_changed or config_changed:
					agent_node.sprite_animations = incoming_anims
					agent_node.set_animation_configs(incoming_anims)
		else:
			# New agent — instantiate
			var agent: CharacterBody2D = AgentScene.instantiate()
			add_child(agent)

			var _url_raw: Variant = agent_data.get("spriteUrl")
			var url: String = "" if _url_raw == null else str(_url_raw)
			var facing_str: String = str(agent_data.get("facing", ""))
			var display_name: String = str(agent_data.get("name", agent_id))

			agent.setup(agent_id, url, x, y, facing_str, display_name)
			agents[agent_id] = agent
			print("AgentManager: Spawned %s at (%d, %d) via world_state" % [display_name, int(x), int(y)])

			# Set initial activity state (agent may already be mid-activity)
			var initial_activity: String = str(agent_data.get("activity", ""))
			if initial_activity != "walking" and initial_activity != "idle":
				agent.current_activity = initial_activity

			# Load animation configs if present in world_state
			var animations: Dictionary = agent_data.get("spriteAnimations", {})
			if not animations.is_empty():
				agent.set_animation_configs(animations)
				agent.sprite_animations = animations
				for anim_name in animations:
					var anim_url: String = str(animations[anim_name].get("url", ""))
					if anim_url != "" and anim_name != "walk":
						agent.load_sprite(anim_url, anim_name)

	# Remove agents no longer present in world_state
	var ids_to_remove: Array = []
	for agent_id in agents:
		if agent_id not in seen_ids:
			ids_to_remove.append(agent_id)

	for agent_id in ids_to_remove:
		print("AgentManager: Removing %s (no longer in world_state)" % agent_id)
		agents[agent_id].queue_free()
		agents.erase(agent_id)


# --- Legacy JsBridge handlers ---

func _on_spawn_agent(agent_id: String, data: Dictionary) -> void:
	if agent_id in agents:
		return  # Already spawned

	var agent: CharacterBody2D = AgentScene.instantiate()
	add_child(agent)

	var url: String = str(data.get("spriteUrl", ""))
	var x: float = float(data.get("x", 0))
	var y: float = float(data.get("y", 0))
	var facing_json: String = str(data.get("facing", ""))
	var display_name: String = str(data.get("name", agent_id))

	agent.setup(agent_id, url, x, y, facing_json, display_name)
	agents[agent_id] = agent
	print("AgentManager: Spawned %s at (%d, %d)" % [display_name, int(x), int(y)])


func _on_move_agent(agent_id: String, x: float, y: float) -> void:
	if agent_id not in agents:
		return
	agents[agent_id].move_to(x, y, "", 60.0)
	print("AgentManager: Move %s to (%d, %d)" % [agent_id, int(x), int(y)])


func _on_remove_agent(agent_id: String) -> void:
	if agent_id not in agents:
		return
	agents[agent_id].queue_free()
	agents.erase(agent_id)
	print("AgentManager: Removed %s" % agent_id)


func _on_update_agent_state(agent_id: String, data: Dictionary) -> void:
	if agent_id not in agents:
		return
	agents[agent_id].update_state(data)


func _on_sprite_loaded(args: Array) -> void:
	var parsed: Variant = JSON.parse_string(str(args[0]))
	if not parsed is Dictionary:
		return
	var agent_id: String = str(parsed.get("id", ""))
	var base64: String = str(parsed.get("base64", ""))
	var animation_name: String = str(parsed.get("animation", "walk"))
	if agent_id == "" or base64 == "" or agent_id not in agents:
		return

	var raw: PackedByteArray = Marshalls.base64_to_raw(base64)
	var image: Image = Image.new()
	if image.load_png_from_buffer(raw) == OK:
		var tex: ImageTexture = ImageTexture.create_from_image(image)
		agents[agent_id].set_spritesheet(tex, animation_name)
		print("AgentManager: Loaded %s spritesheet for %s (%dx%d)" % [animation_name, agent_id, image.get_width(), image.get_height()])


