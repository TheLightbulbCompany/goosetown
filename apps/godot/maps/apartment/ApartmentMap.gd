## ApartmentMap — interior scene script for the per-user apartment.
##
## Fixed camera view — the entire furnished room is visible at once.
## No dragging, no panning, no zoom controls. The camera frames the
## furniture's actual bounding box (computed at runtime from the
## YSortedObjects sprites), so it stays centered regardless of canvas
## size and self-corrects if the furniture layout changes. The painted
## floor extends past the furniture; that surplus floor fills the edges
## and any remaining space is letterboxed black.
class_name ApartmentMap
extends Node2D

# Fallback room bounds (used only if furniture bounds can't be computed,
# and by get_location_position's last-resort fallback). The live camera
# frames the furniture bbox instead — see _frame_camera().
const ROOM_LEFT := 90
const ROOM_TOP := -38
const ROOM_RIGHT := 858
const ROOM_BOTTOM := 474
const TILE_SIZE := 32

# Breathing room (world px) added around the furniture bbox so items
# aren't flush against the viewport edge.
const FRAME_MARGIN := 24.0

@onready var camera: Camera2D = $Camera2D
@onready var locations: Node2D = $Locations
@onready var ysorted: Node2D = $YSortedObjects


func _ready() -> void:
	_frame_camera()

	# Disable all camera limits — camera is fixed, no panning
	camera.limit_left = -10000
	camera.limit_top = -10000
	camera.limit_right = 10000
	camera.limit_bottom = 10000

	# Recalculate on viewport resize (browser window resize)
	get_viewport().size_changed.connect(_frame_camera)

	JsBridge.set_camera_requested.connect(_on_set_camera)

	# AgentManager receives world_state via JsBridge (React forwards from its WebSocket).
	# No ViewerProtocol needed — React manages the WebSocket connection.


## Center + zoom the camera so the furniture bounding box fits the viewport.
## Recomputed on ready and on every viewport resize, so it's correct for any
## canvas size with no hand-tuned offsets.
func _frame_camera() -> void:
	var bounds: Rect2 = _compute_furniture_bounds()
	if bounds.size.x <= 0.0 or bounds.size.y <= 0.0:
		# Fallback: frame the static room rectangle.
		bounds = Rect2(
			float(ROOM_LEFT),
			float(ROOM_TOP),
			float(ROOM_RIGHT - ROOM_LEFT),
			float(ROOM_BOTTOM - ROOM_TOP)
		)

	bounds = bounds.grow(FRAME_MARGIN)
	camera.position = bounds.get_center()

	var vp_size: Vector2 = get_viewport_rect().size
	if vp_size.x > 0.0 and vp_size.y > 0.0 and bounds.size.x > 0.0 and bounds.size.y > 0.0:
		var fit_zoom: float = minf(vp_size.x / bounds.size.x, vp_size.y / bounds.size.y)
		camera.zoom = Vector2(fit_zoom, fit_zoom)
	else:
		camera.zoom = Vector2(2.0, 2.0)


## Union the global AABBs of every Sprite2D under YSortedObjects.
## Returns a zero-size Rect2 if there are no textured sprites (caller falls
## back to the static room rect).
func _compute_furniture_bounds() -> Rect2:
	var sprites: Array[Sprite2D] = []
	_collect_sprites(ysorted, sprites)

	var bounds: Rect2 = Rect2()
	var have_any: bool = false
	for sprite in sprites:
		if sprite.texture == null:
			continue
		var local: Rect2 = sprite.get_rect()
		var xf: Transform2D = sprite.get_global_transform()
		# Transform all four corners (scale/rotation safe), then take the AABB.
		var c0: Vector2 = xf * local.position
		var c1: Vector2 = xf * Vector2(local.position.x + local.size.x, local.position.y)
		var c2: Vector2 = xf * Vector2(local.position.x, local.position.y + local.size.y)
		var c3: Vector2 = xf * (local.position + local.size)
		var mn: Vector2 = Vector2(
			minf(minf(c0.x, c1.x), minf(c2.x, c3.x)),
			minf(minf(c0.y, c1.y), minf(c2.y, c3.y))
		)
		var mx: Vector2 = Vector2(
			maxf(maxf(c0.x, c1.x), maxf(c2.x, c3.x)),
			maxf(maxf(c0.y, c1.y), maxf(c2.y, c3.y))
		)
		var r: Rect2 = Rect2(mn, mx - mn)
		if not have_any:
			bounds = r
			have_any = true
		else:
			bounds = bounds.merge(r)

	if not have_any:
		return Rect2()
	return bounds


func _collect_sprites(node: Node, out: Array[Sprite2D]) -> void:
	for child in node.get_children():
		if child is Sprite2D:
			out.append(child)
		_collect_sprites(child, out)


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
