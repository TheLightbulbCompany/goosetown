## JS Bridge singleton for React ↔ Godot communication.
## Registered as autoload "JsBridge" in project.godot.
## This node is an autoload and survives get_tree().change_scene_to_packed() calls,
## so JS callbacks registered here remain valid across scene switches.
extends Node

signal spawn_agent_requested(agent_id: String, data: Dictionary)
signal move_agent_requested(agent_id: String, x: float, y: float)
signal remove_agent_requested(agent_id: String)
signal update_agent_state_requested(agent_id: String, data: Dictionary)
signal set_camera_requested(x: float, y: float, zoom: float)
signal switch_scene_requested(scene_name: String)
signal world_state_received(data: Dictionary)

## Load map scenes on demand to avoid blocking editor startup.
var TOWN_SCENE: PackedScene = null
var APARTMENT_SCENE: PackedScene = null

## Tracks which scene is currently active for agent filtering.
var current_scene: String = "town"

var _callbacks: Dictionary = {}


func _ready() -> void:
	if OS.has_feature("web"):
		_register_js_callbacks()
		_emit_ready_event()


func _register_js_callbacks() -> void:
	_callbacks["spawnAgent"] = JavaScriptBridge.create_callback(_on_spawn_agent)
	_callbacks["moveAgent"] = JavaScriptBridge.create_callback(_on_move_agent)
	_callbacks["removeAgent"] = JavaScriptBridge.create_callback(_on_remove_agent)
	_callbacks["updateAgentState"] = JavaScriptBridge.create_callback(_on_update_agent_state)
	_callbacks["setCamera"] = JavaScriptBridge.create_callback(_on_set_camera)
	_callbacks["switchScene"] = JavaScriptBridge.create_callback(_on_switch_scene)
	_callbacks["updateWorldState"] = JavaScriptBridge.create_callback(_on_update_world_state)

	var window: JavaScriptObject = JavaScriptBridge.get_interface("window")
	window.set("godotSpawnAgent", _callbacks["spawnAgent"])
	window.set("godotMoveAgent", _callbacks["moveAgent"])
	window.set("godotRemoveAgent", _callbacks["removeAgent"])
	window.set("godotUpdateAgentState", _callbacks["updateAgentState"])
	window.set("godotSetCamera", _callbacks["setCamera"])
	window.set("godotSwitchScene", _callbacks["switchScene"])
	window.set("godotUpdateWorldState", _callbacks["updateWorldState"])


func _emit_ready_event() -> void:
	# Set the bridge object that React looks for
	JavaScriptBridge.eval("""
		window._godotBridge = {
			spawnAgent: function(id, spriteUrl, x, y, facing, name) {
				window.godotSpawnAgent(JSON.stringify({id:id, spriteUrl:spriteUrl, x:x, y:y, facing:facing, name:name}));
			},
			moveAgent: function(id, x, y, facing, speed) {
				window.godotMoveAgent(JSON.stringify({id:id, x:x, y:y, facing:facing||'', speed:speed||0}));
			},
			removeAgent: function(id) {
				window.godotRemoveAgent(JSON.stringify({id:id}));
			},
			updateAgentState: function(id, data) {
				window.godotUpdateAgentState(JSON.stringify({id:id, data:data}));
			},
			setCamera: function(x, y, zoom) {
				window.godotSetCamera(JSON.stringify({x:x, y:y, zoom:zoom||1.0}));
			},
			switchScene: function(sceneName) {
				window.godotSwitchScene(JSON.stringify({scene:sceneName}));
			}
		};
		window.dispatchEvent(new CustomEvent('godot_ready'));
		console.log('[GooseTown] Godot bridge ready');
	""")


## Build the JS string that call_js() will eval. Pure function for testability.
## Both inputs MUST come from non-user-controlled sources (fn_name is a
## whitelist constant in every call site; key is generated here from time+rand).
## User payload data goes through window.set() — never into the eval string.
func _build_eval_script(fn_name: String, key: String) -> String:
	# Defense-in-depth: key must match the pattern call_js() generates.
	# Prevents a future caller from accidentally interpolating a hostile
	# value into the eval string.
	var re := RegEx.new()
	re.compile("^__gt_payload_[0-9]+_[0-9]+$")
	if re.search(key) == null:
		push_error("JsBridge._build_eval_script: rejected malformed key %s" % key)
		return ""
	return (
		"(function(){"
		+ "var p = window['%s'];" % key
		+ "if(window.__gt_%s && p)window.__gt_%s(JSON.parse(p));" % [fn_name, fn_name]
		+ "delete window['%s'];" % key
		+ "})()"
	)


## Call a pre-defined JS handler (window.__gt_<fn_name>) with a dictionary
## payload. User-controlled values are stashed on the window object via the
## JavaScriptObject.set API, never interpolated into the eval string.
##
## The receiving side (frontend JS) must define window.__gt_<fn_name>(args).
func call_js(fn_name: String, args: Dictionary) -> void:
	if not OS.has_feature("web"):
		return
	# fn_name becomes part of the eval string (`window.__gt_<fn_name>`).
	# Require identifier-safe characters to prevent injection.
	var fn_re := RegEx.new()
	fn_re.compile("^[a-zA-Z0-9_]+$")
	if fn_re.search(fn_name) == null:
		push_error("JsBridge.call_js: rejected unsafe fn_name: %s" % fn_name)
		return
	var window: JavaScriptObject = JavaScriptBridge.get_interface("window")
	var json_str: String = JSON.stringify(args)
	var key: String = "__gt_payload_%d_%d" % [Time.get_ticks_msec(), randi()]
	var script: String = _build_eval_script(fn_name, key)
	if script == "":
		return
	# Stash user data as a string property — never touches eval.
	window.set(key, json_str)
	JavaScriptBridge.eval(script)


func emit_to_react(event_name: String, data: Dictionary = {}) -> void:
	# event_name is always a constant literal at the call site (whitelist);
	# data goes through call_js which stashes it via window.set().
	if not OS.has_feature("web"):
		return
	call_js("dispatch_event_" + event_name, data)


func _on_spawn_agent(args: Array) -> void:
	var parsed: Dictionary = JSON.parse_string(str(args[0]))
	if parsed:
		spawn_agent_requested.emit(parsed.get("id", ""), parsed)


func _on_move_agent(args: Array) -> void:
	var parsed: Dictionary = JSON.parse_string(str(args[0]))
	if parsed:
		move_agent_requested.emit(
			parsed.get("id", ""),
			float(parsed.get("x", 0)),
			float(parsed.get("y", 0)),
		)


func _on_remove_agent(args: Array) -> void:
	var parsed: Dictionary = JSON.parse_string(str(args[0]))
	if parsed:
		remove_agent_requested.emit(parsed.get("id", ""))


func _on_update_agent_state(args: Array) -> void:
	var parsed: Dictionary = JSON.parse_string(str(args[0]))
	if parsed:
		update_agent_state_requested.emit(parsed.get("id", ""), parsed)


func _on_set_camera(args: Array) -> void:
	var parsed: Dictionary = JSON.parse_string(str(args[0]))
	if parsed:
		set_camera_requested.emit(
			float(parsed.get("x", 0)),
			float(parsed.get("y", 0)),
			float(parsed.get("zoom", 1.0)),
		)


## Handles world_state updates forwarded from React's WebSocket.
## This bypasses Godot's WebSocketPeer URL issues in WebGL.
func _on_update_world_state(args: Array) -> void:
	var parsed: Dictionary = JSON.parse_string(str(args[0]))
	if parsed:
		_emit_world_state_deferred.call_deferred(parsed)


func _emit_world_state_deferred(data: Dictionary) -> void:
	world_state_received.emit(data)


## Handles scene switch requests from React.
## Valid scene names: "town", "apartment".
## After the switch, dispatches a "godot_scene_changed" CustomEvent to React
## with {scene: name} so the React side can confirm the transition completed.
## Awaits one frame after change_scene_to_packed so the new scene tree is live
## before emitting scene_changed — prevents race conditions where React sends
## world_state before the new AgentManager is ready.
func _on_switch_scene(args: Array) -> void:
	var parsed: Dictionary = JSON.parse_string(str(args[0]))
	if not parsed:
		push_error("JsBridge._on_switch_scene: failed to parse args")
		return

	var scene_name: String = str(parsed.get("scene", ""))
	print("[JsBridge] Scene switch requested: %s" % scene_name)
	switch_scene_requested.emit(scene_name)

	# Lazy-load scenes on first switch to avoid blocking editor startup
	if TOWN_SCENE == null:
		TOWN_SCENE = load("res://maps/isol88/Isol88Map.tscn")
	if APARTMENT_SCENE == null:
		APARTMENT_SCENE = load("res://maps/apartment/ApartmentMap.tscn")

	var target_scene: PackedScene
	match scene_name:
		"town":
			target_scene = TOWN_SCENE
		"apartment":
			target_scene = APARTMENT_SCENE
		_:
			push_error("JsBridge._on_switch_scene: unknown scene '%s'" % scene_name)
			return

	# Update current_scene before the switch so AgentManager filters immediately
	current_scene = scene_name

	var err: int = get_tree().change_scene_to_packed(target_scene)
	if err != OK:
		push_error("JsBridge._on_switch_scene: change_scene_to_packed failed: %s" % error_string(err))
		return

	# Wait one frame so the new scene tree is fully initialized before notifying React.
	await get_tree().process_frame

	print("[JsBridge] Scene switch complete: %s" % scene_name)
	# Notify React that the scene switch completed successfully.
	emit_to_react("scene_changed", {"scene": scene_name})
