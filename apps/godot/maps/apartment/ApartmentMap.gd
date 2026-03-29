## ApartmentMap — interior scene script for the per-user apartment.
##
## 24x16 tile grid (768x512 px at 32px/tile).
## Camera zoom computed dynamically so the map always fills the viewport
## with no void/black bars, regardless of window or canvas size.
##
## Location markers live under the "Locations" child node.
## TODO: game_server.gd currently looks for "Markers" when collecting location
## nodes. It will need a context-aware lookup before this scene can be loaded
## by the headless game server for apartment support.
class_name ApartmentMap
extends Node2D

# Full canvas matches the town viewport (44x27 tiles) so Godot rendering
# fills the screen with no void.  The camera limits below restrict the
# view to the actual room interior (24x16 tiles = 768x512).
const CANVAS_SIZE := Vector2i(1408, 864)
const ROOM_SIZE := Vector2i(768, 512)
const TILE_SIZE := 32
const CANVAS_TILES := Vector2i(44, 27)
const ROOM_TILES := Vector2i(24, 16)

@onready var camera: Camera2D = $Camera2D
@onready var locations: Node2D = $Locations

var _dragging: bool = false


func _ready() -> void:
	camera.position = Vector2(ROOM_SIZE.x / 2.0, ROOM_SIZE.y / 2.0)
	camera.zoom = Vector2(2.0, 2.0)

	# Disable Godot's built-in camera limits — _clamp_camera() handles
	# all constraint logic manually (same pattern as the town map).
	# Setting limits to extreme values effectively disables them.
	camera.limit_left = -10000
	camera.limit_top = -10000
	camera.limit_right = 10000
	camera.limit_bottom = 10000
	camera.limit_smoothed = false

	JsBridge.set_camera_requested.connect(_on_set_camera)

	# Viewer WebSocket wiring (browser WebGL only).
	if OS.has_feature("web"):
		var js_ws_url: String = JavaScriptBridge.eval("window._goosetown_ws_url || ''")
		if js_ws_url != "" and js_ws_url != null:
			var viewer := ViewerProtocol.new()
			add_child(viewer)
			var agent_mgr: Node = get_node_or_null("%AgentManager")
			if agent_mgr:
				agent_mgr.setup_viewer(viewer)
			viewer.connect_to_server(js_ws_url)
			print("[ApartmentMap] Viewer connecting to: %s" % js_ws_url)



func _unhandled_input(event: InputEvent) -> void:
	# Click drag to pan
	if event is InputEventMouseButton:
		var mb: InputEventMouseButton = event as InputEventMouseButton
		if mb.button_index == MOUSE_BUTTON_LEFT or mb.button_index == MOUSE_BUTTON_MIDDLE:
			_dragging = mb.pressed

	if event is InputEventMouseMotion and _dragging:
		var mm: InputEventMouseMotion = event as InputEventMouseMotion
		camera.position -= mm.relative / camera.zoom
		_clamp_camera()

	# Consume scroll/pan gestures so the page doesn't scroll
	if event is InputEventMouseButton:
		var mb2: InputEventMouseButton = event as InputEventMouseButton
		if mb2.button_index == MOUSE_BUTTON_WHEEL_UP or mb2.button_index == MOUSE_BUTTON_WHEEL_DOWN:
			get_viewport().set_input_as_handled()

	if event is InputEventPanGesture:
		var pg: InputEventPanGesture = event as InputEventPanGesture
		camera.position += pg.delta * 10.0 / camera.zoom
		_clamp_camera()
		get_viewport().set_input_as_handled()


func _clamp_camera() -> void:
	var viewport_size: Vector2 = get_viewport_rect().size / camera.zoom
	var half_vp: Vector2 = viewport_size / 2.0
	camera.position.x = clampf(camera.position.x, half_vp.x, float(ROOM_SIZE.x) - half_vp.x)
	camera.position.y = clampf(camera.position.y, half_vp.y, float(ROOM_SIZE.y) - half_vp.y)


func _on_set_camera(x: float, y: float, zoom: float) -> void:
	if x != 0 or y != 0:
		camera.position = Vector2(x, y)
	_clamp_camera()


func get_location_position(location_name: String) -> Vector2:
	var marker: Node = locations.get_node_or_null(location_name)
	if marker:
		return marker.global_position
	var exit_marker: Node = locations.get_node_or_null("exit")
	if exit_marker:
		return exit_marker.global_position
	return Vector2(ROOM_SIZE.x / 2.0, ROOM_SIZE.y - TILE_SIZE)
