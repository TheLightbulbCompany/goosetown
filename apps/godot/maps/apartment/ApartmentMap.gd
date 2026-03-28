## ApartmentMap — interior scene script for the per-user apartment.
##
## 24x16 tile grid (768x512 px at 32px/tile, rendered at 2x zoom = 1536x1024).
## Camera supports drag-to-pan and scroll zoom, same as the town map.
##
## Location markers live under the "Locations" child node (not "Markers").
## TODO: game_server.gd currently looks for "Markers" when collecting location
## nodes. It will need a context-aware lookup (or a shared interface) before
## this scene can be loaded by the headless game server for apartment support.
##
## Viewer WebSocket wiring follows the same pattern as Isol88Map.gd.
class_name ApartmentMap
extends Node2D

## 24 * 32px wide, 16 * 32px tall
const MAP_SIZE := Vector2i(768, 512)
const TILE_SIZE := 32
const MAP_TILES := Vector2i(24, 16)
const DEFAULT_ZOOM := 2.0
const MIN_ZOOM := 1.0
const MAX_ZOOM := 4.0
const ZOOM_STEP := 0.2

var _dragging: bool = false

@onready var camera: Camera2D = $Camera2D
@onready var locations: Node2D = $Locations


func _ready() -> void:
	camera.position = Vector2(MAP_SIZE.x / 2.0, MAP_SIZE.y / 2.0)
	camera.zoom = Vector2(DEFAULT_ZOOM, DEFAULT_ZOOM)
	camera.limit_left = 0
	camera.limit_top = 0
	camera.limit_right = MAP_SIZE.x
	camera.limit_bottom = MAP_SIZE.y

	# Viewer WebSocket wiring (browser WebGL only).
	if OS.has_feature("web"):
		var js_ws_url: String = JavaScriptBridge.eval("window._goosetown_ws_url || ''")
		if js_ws_url != "" and js_ws_url != null:
			var viewer := ViewerProtocol.new()
			add_child(viewer)
			# AgentManager may not exist in the apartment scene yet
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

		# Scroll wheel to zoom
		if mb.button_index == MOUSE_BUTTON_WHEEL_UP and mb.pressed:
			var new_zoom: float = minf(camera.zoom.x + ZOOM_STEP, MAX_ZOOM)
			camera.zoom = Vector2(new_zoom, new_zoom)
			_clamp_camera()
			get_viewport().set_input_as_handled()
		elif mb.button_index == MOUSE_BUTTON_WHEEL_DOWN and mb.pressed:
			var new_zoom: float = maxf(camera.zoom.x - ZOOM_STEP, MIN_ZOOM)
			camera.zoom = Vector2(new_zoom, new_zoom)
			_clamp_camera()
			get_viewport().set_input_as_handled()

	if event is InputEventMouseMotion and _dragging:
		var mm: InputEventMouseMotion = event as InputEventMouseMotion
		camera.position -= mm.relative / camera.zoom
		_clamp_camera()

	# Trackpad pan gesture
	if event is InputEventPanGesture:
		var pg: InputEventPanGesture = event as InputEventPanGesture
		camera.position += pg.delta * 10.0 / camera.zoom
		_clamp_camera()
		get_viewport().set_input_as_handled()


func _clamp_camera() -> void:
	var viewport_size: Vector2 = get_viewport_rect().size / camera.zoom
	var half_vp: Vector2 = viewport_size / 2.0
	camera.position.x = clampf(camera.position.x, half_vp.x, float(MAP_SIZE.x) - half_vp.x)
	camera.position.y = clampf(camera.position.y, half_vp.y, float(MAP_SIZE.y) - half_vp.y)


## Returns the world-space position of a named location spot.
## Falls back to the exit marker when the name is unrecognised.
func get_location_position(location_name: String) -> Vector2:
	var marker: Node = locations.get_node_or_null(location_name)
	if marker:
		return marker.global_position
	var exit_marker: Node = locations.get_node_or_null("exit")
	if exit_marker:
		return exit_marker.global_position
	return Vector2(MAP_SIZE.x / 2.0, MAP_SIZE.y - TILE_SIZE)
