## LibraryMap — interior scene for the shared library building.
##
## Fixed camera view, mirrors the apartment's letterboxed approach:
## the entire room is visible at once, no panning or zoom controls.
## User-driven layout: the TileMapLayer + NavigationRegion2D + Locations
## markers are placeholders authored in the editor.
class_name LibraryMap
extends Node2D

# Room bounds — match the painted room (verified 2026-04-28 from saved scene).
const ROOM_LEFT := -440
const ROOM_TOP := -260
const ROOM_RIGHT := 990
const ROOM_BOTTOM := 700
const TILE_SIZE := 32

@onready var camera: Camera2D = $Camera2D
@onready var locations: Node2D = $Locations


func _ready() -> void:
	# Center camera on the room and scale to fit the viewport.
	var room_w: float = float(ROOM_RIGHT - ROOM_LEFT)
	var room_h: float = float(ROOM_BOTTOM - ROOM_TOP)
	camera.position = Vector2(
		float(ROOM_LEFT) + room_w / 2.0,
		float(ROOM_TOP) + room_h / 2.0
	)

	var vp_size: Vector2 = get_viewport_rect().size
	if vp_size.x > 0 and vp_size.y > 0:
		var zoom_x: float = vp_size.x / room_w
		var zoom_y: float = vp_size.y / room_h
		var fit_zoom: float = minf(zoom_x, zoom_y)
		camera.zoom = Vector2(fit_zoom, fit_zoom)
	else:
		camera.zoom = Vector2(2.0, 2.0)

	camera.limit_left = -10000
	camera.limit_top = -10000
	camera.limit_right = 10000
	camera.limit_bottom = 10000

	get_viewport().size_changed.connect(_on_viewport_size_changed)

	JsBridge.set_camera_requested.connect(_on_set_camera)


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
	# Fixed camera — ignore external set_camera requests.
	pass


func get_location_position(location_name: String) -> Vector2:
	var marker: Node = locations.get_node_or_null(location_name)
	if marker:
		return marker.global_position
	var exit_marker: Node = locations.get_node_or_null("exit")
	if exit_marker:
		return exit_marker.global_position
	return Vector2(float(ROOM_LEFT + ROOM_RIGHT) / 2.0, float(ROOM_BOTTOM) - TILE_SIZE)
