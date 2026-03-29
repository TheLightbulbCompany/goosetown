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

const MAP_SIZE := Vector2i(768, 512)
const TILE_SIZE := 32
const MAP_TILES := Vector2i(24, 16)

@onready var camera: Camera2D = $Camera2D
@onready var locations: Node2D = $Locations

var _dragging: bool = false


func _ready() -> void:
	_apply_zoom()
	camera.position = Vector2(MAP_SIZE.x / 2.0, MAP_SIZE.y / 2.0)

	camera.limit_left = 0
	camera.limit_top = 0
	camera.limit_right = MAP_SIZE.x
	camera.limit_bottom = MAP_SIZE.y

	get_viewport().size_changed.connect(_on_viewport_size_changed)

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


func _apply_zoom() -> void:
	## Compute zoom so the map always covers the full viewport — no void bars.
	## Add a small margin (1.05x) so there's always room to pan and no edge void.
	var vp_size: Vector2 = get_viewport_rect().size
	if vp_size.x <= 0 or vp_size.y <= 0:
		camera.zoom = Vector2(2.0, 2.0)
		return
	var min_zoom: float = maxf(vp_size.x / float(MAP_SIZE.x), vp_size.y / float(MAP_SIZE.y))
	# Zoom in slightly past the fill point so the map overflows the viewport
	# on both axes — this guarantees pan room and no gray edges.
	var zoom_level: float = min_zoom * 1.1
	camera.zoom = Vector2(zoom_level, zoom_level)
	_clamp_camera()


func _on_viewport_size_changed() -> void:
	_apply_zoom()
	_clamp_camera()


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
	# If viewport is wider/taller than map on an axis, center on that axis
	if half_vp.x * 2.0 >= float(MAP_SIZE.x):
		camera.position.x = float(MAP_SIZE.x) / 2.0
	else:
		camera.position.x = clampf(camera.position.x, half_vp.x, float(MAP_SIZE.x) - half_vp.x)
	if half_vp.y * 2.0 >= float(MAP_SIZE.y):
		camera.position.y = float(MAP_SIZE.y) / 2.0
	else:
		camera.position.y = clampf(camera.position.y, half_vp.y, float(MAP_SIZE.y) - half_vp.y)


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
	return Vector2(MAP_SIZE.x / 2.0, MAP_SIZE.y - TILE_SIZE)
