## ApartmentMap — interior scene script for the per-user apartment.
##
## Fixed camera view — the entire apartment is visible at once.
## No dragging, no panning, no zoom controls. The room is centered
## in the viewport with black bars filling any extra space (letterboxing).
class_name ApartmentMap
extends Node2D

# Room bounds: 24x16 tile room starts at tile (2,1) = pixel (64,32).
const ROOM_LEFT := 64
const ROOM_TOP := 32
const ROOM_RIGHT := 832
const ROOM_BOTTOM := 544
const TILE_SIZE := 32

@onready var camera: Camera2D = $Camera2D
@onready var locations: Node2D = $Locations


func _ready() -> void:
	# Center camera on the actual room center
	var room_w: float = float(ROOM_RIGHT - ROOM_LEFT)
	var room_h: float = float(ROOM_BOTTOM - ROOM_TOP)
	camera.position = Vector2(
		float(ROOM_LEFT) + room_w / 2.0,
		float(ROOM_TOP) + room_h / 2.0
	)

	# Calculate zoom to fit the entire room in the viewport.
	# Use the SMALLER ratio so the full room is visible (letterboxed).
	var vp_size: Vector2 = get_viewport_rect().size
	if vp_size.x > 0 and vp_size.y > 0:
		var zoom_x: float = vp_size.x / room_w
		var zoom_y: float = vp_size.y / room_h
		var fit_zoom: float = minf(zoom_x, zoom_y)
		camera.zoom = Vector2(fit_zoom, fit_zoom)
	else:
		camera.zoom = Vector2(2.0, 2.0)

	# Disable all camera limits — camera is fixed, no panning
	camera.limit_left = -10000
	camera.limit_top = -10000
	camera.limit_right = 10000
	camera.limit_bottom = 10000

	# Recalculate on viewport resize (browser window resize)
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


func _on_viewport_size_changed() -> void:
	var room_w: float = float(ROOM_RIGHT - ROOM_LEFT)
	var room_h: float = float(ROOM_BOTTOM - ROOM_TOP)
	var vp_size: Vector2 = get_viewport_rect().size
	if vp_size.x > 0 and vp_size.y > 0:
		var zoom_x: float = vp_size.x / room_w
		var zoom_y: float = vp_size.y / room_h
		var fit_zoom: float = minf(zoom_x, zoom_y)
		camera.zoom = Vector2(fit_zoom, fit_zoom)


func _on_set_camera(_x: float, _y: float, _zoom: float) -> void:
	# Camera is fixed — ignore external set_camera requests
	pass


func get_location_position(location_name: String) -> Vector2:
	var marker: Node = locations.get_node_or_null(location_name)
	if marker:
		return marker.global_position
	var exit_marker: Node = locations.get_node_or_null("exit")
	if exit_marker:
		return exit_marker.global_position
	return Vector2(float(ROOM_LEFT + ROOM_RIGHT) / 2.0, float(ROOM_BOTTOM) - TILE_SIZE)
