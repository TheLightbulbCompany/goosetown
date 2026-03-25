## JS Bridge singleton for React ↔ Godot communication.
## Registered as autoload "JsBridge" in project.godot.
extends Node

signal spawn_agent_requested(agent_id: String, data: Dictionary)
signal move_agent_requested(agent_id: String, x: float, y: float)
signal remove_agent_requested(agent_id: String)
signal update_agent_state_requested(agent_id: String, data: Dictionary)
signal set_camera_requested(x: float, y: float, zoom: float)

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

	var window: JavaScriptObject = JavaScriptBridge.get_interface("window")
	window.set("godotSpawnAgent", _callbacks["spawnAgent"])
	window.set("godotMoveAgent", _callbacks["moveAgent"])
	window.set("godotRemoveAgent", _callbacks["removeAgent"])
	window.set("godotUpdateAgentState", _callbacks["updateAgentState"])
	window.set("godotSetCamera", _callbacks["setCamera"])


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
			}
		};
		window.dispatchEvent(new CustomEvent('godot_ready'));
		console.log('[GooseTown] Godot bridge ready');
	""")


func emit_to_react(event_name: String, data: Dictionary = {}) -> void:
	if OS.has_feature("web"):
		var json_str: String = JSON.stringify(data)
		JavaScriptBridge.eval(
			"window.dispatchEvent(new CustomEvent('godot_%s', {detail: JSON.parse('%s')}))"
			% [event_name, json_str]
		)


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
