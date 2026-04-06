## Manages all agents on the map.
## Supports two modes:
##   1. ViewerProtocol — receives world_state events via WebSocket (preferred)
##   2. JsBridge signals — legacy spawn/move/remove from React (fallback)
extends Node

const AgentScene: PackedScene = preload("res://scenes/agent.tscn")

var agents: Dictionary = {}  # agent_id -> Agent node
var _viewer: ViewerProtocol = null
var _known_agents: Dictionary = {}  # agent_id -> agent_node (viewer protocol mode)
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
	var _prev_count: int = _known_agents.size()
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

		if agent_id in _known_agents:
			# Existing agent — use navmesh pathfinding so agents walk around buildings
			var agent_node: CharacterBody2D = _known_agents[agent_id]
			agent_node.set_navigation_target(Vector2(x, y))

			# Update speech/state if present
			if agent_data.has("speechText") or agent_data.has("speech"):
				_known_agents[agent_id].update_state(agent_data)

			# Update activity state
			var activity: String = str(agent_data.get("activity", ""))
			if activity != "walking" and activity != "idle":
				_known_agents[agent_id].current_activity = activity
			else:
				_known_agents[agent_id].current_activity = ""
		else:
			# New agent — instantiate
			var agent: CharacterBody2D = AgentScene.instantiate()
			add_child(agent)

			var url: String = str(agent_data.get("spriteUrl", ""))
			var facing_str: String = str(agent_data.get("facing", ""))
			var display_name: String = str(agent_data.get("name", agent_id))

			agent.setup(agent_id, url, x, y, facing_str, display_name)
			_known_agents[agent_id] = agent
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
						_load_sprite_for_agent(agent_id, anim_url, anim_name)

	# Remove agents no longer present in world_state
	var ids_to_remove: Array = []
	for agent_id in _known_agents:
		if agent_id not in seen_ids:
			ids_to_remove.append(agent_id)

	for agent_id in ids_to_remove:
		print("AgentManager: Removing %s (no longer in world_state)" % agent_id)
		_known_agents[agent_id].queue_free()
		_known_agents.erase(agent_id)
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
	_known_agents.erase(agent_id)
	print("AgentManager: Removed %s" % agent_id)


func _on_update_agent_state(agent_id: String, data: Dictionary) -> void:
	if agent_id not in agents:
		return
	agents[agent_id].update_state(data)


func _on_sprite_loaded(args: Array) -> void:
	var parsed: Dictionary = JSON.parse_string(str(args[0]))
	if parsed == null:
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


func _load_sprite_for_agent(agent_id: String, url: String, animation_name: String) -> void:
	if not OS.has_feature("web"):
		return
	var js_code := """
		(function() {
			fetch('%s')
				.then(function(r) { return r.blob(); })
				.then(function(b) {
					var reader = new FileReader();
					reader.onloadend = function() {
						var base64 = reader.result.split(',')[1];
						if (window.godotUpdateAgentSprite) {
							window.godotUpdateAgentSprite(JSON.stringify({
								id: '%s',
								base64: base64,
								animation: '%s'
							}));
						}
					};
					reader.readAsDataURL(b);
				})
				.catch(function(e) { console.error('Sprite load failed:', e); });
		})();
	""" % [url, agent_id, animation_name]
	JavaScriptBridge.eval(js_code)
