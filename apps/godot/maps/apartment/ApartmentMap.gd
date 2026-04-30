## ApartmentMap — interior scene script for the per-user apartment.
##
## Fixed camera view — the entire apartment is visible at once.
## No dragging, no panning, no zoom controls. The room is centered
## in the viewport with black bars filling any extra space (letterboxing).
class_name ApartmentMap
extends Node2D

# Room bounds tuned to center the view evenly with black bars.
const ROOM_LEFT := 90
const ROOM_TOP := -38
const ROOM_RIGHT := 858
const ROOM_BOTTOM := 474
const TILE_SIZE := 32

# Manual camera nudge applied on top of the room-centered position.
# Negative X = shift the visible content LEFT (reveals more left-wall area,
# crops the right-empty-floor area). Negative Y = shift UP. Tuned 2026-04-30
# after PR #245 (canvasResizePolicy: 0) shrank the effective canvas from
# 1728 CSS to 1408 CSS — the previous hand-tuning was for the wider canvas
# and now leaves the bottom-right corner visibly empty.
const CAMERA_OFFSET_X := -40
const CAMERA_OFFSET_Y := -20

@onready var camera: Camera2D = $Camera2D
@onready var locations: Node2D = $Locations


func _ready() -> void:
	# Center camera on the actual room center, then apply manual nudge.
	var room_w: float = float(ROOM_RIGHT - ROOM_LEFT)
	var room_h: float = float(ROOM_BOTTOM - ROOM_TOP)
	camera.position = Vector2(
		float(ROOM_LEFT) + room_w / 2.0 + CAMERA_OFFSET_X,
		float(ROOM_TOP) + room_h / 2.0 + CAMERA_OFFSET_Y
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

	# AgentManager receives world_state via JsBridge (React forwards from its WebSocket).
	# No ViewerProtocol needed — React manages the WebSocket connection.


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
