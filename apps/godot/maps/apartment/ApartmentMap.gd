## ApartmentMap — interior scene script for the per-user apartment.
##
## Canvas is 44x27 tiles (1408x864) matching the town viewport.
## Camera limits restrict view to the room interior.
## Drag to pan, same as the town map.
class_name ApartmentMap
extends Node2D

const TILE_SIZE := 32

# Room interior bounds (where the camera is allowed to show).
# These define the edges of the actual apartment room within the larger canvas.
# Derived from furniture positions: leftmost x=132, rightmost x=718,
# topmost y=42, bottommost y=496. With wall padding.
const ROOM_LEFT := 32
const ROOM_TOP := 32
const ROOM_RIGHT := 736
const ROOM_BOTTOM := 496

@onready var camera: Camera2D = $Camera2D
@onready var locations: Node2D = $Locations

var _dragging: bool = false


func _ready() -> void:
	camera.zoom = Vector2(2.0, 2.0)
	camera.position = Vector2(
		(ROOM_LEFT + ROOM_RIGHT) / 2.0,
		(ROOM_TOP + ROOM_BOTTOM) / 2.0
	)

	# Godot Camera2D limits prevent showing anything outside the room.
	camera.limit_left = ROOM_LEFT
	camera.limit_top = ROOM_TOP
	camera.limit_right = ROOM_RIGHT
	camera.limit_bottom = ROOM_BOTTOM

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
	# Click drag to pan — Godot's camera limits handle clamping automatically
	if event is InputEventMouseButton:
		var mb: InputEventMouseButton = event as InputEventMouseButton
		if mb.button_index == MOUSE_BUTTON_LEFT or mb.button_index == MOUSE_BUTTON_MIDDLE:
			_dragging = mb.pressed

	if event is InputEventMouseMotion and _dragging:
		var mm: InputEventMouseMotion = event as InputEventMouseMotion
		camera.position -= mm.relative / camera.zoom

	# Consume scroll/pan gestures so the page doesn't scroll
	if event is InputEventMouseButton:
		var mb2: InputEventMouseButton = event as InputEventMouseButton
		if mb2.button_index == MOUSE_BUTTON_WHEEL_UP or mb2.button_index == MOUSE_BUTTON_WHEEL_DOWN:
			get_viewport().set_input_as_handled()

	if event is InputEventPanGesture:
		var pg: InputEventPanGesture = event as InputEventPanGesture
		camera.position += pg.delta * 10.0 / camera.zoom
		get_viewport().set_input_as_handled()


func _on_set_camera(x: float, y: float, _zoom: float) -> void:
	if x != 0 or y != 0:
		camera.position = Vector2(x, y)


func get_location_position(location_name: String) -> Vector2:
	var marker: Node = locations.get_node_or_null(location_name)
	if marker:
		return marker.global_position
	var exit_marker: Node = locations.get_node_or_null("exit")
	if exit_marker:
		return exit_marker.global_position
	return Vector2((ROOM_LEFT + ROOM_RIGHT) / 2.0, ROOM_BOTTOM - TILE_SIZE)
