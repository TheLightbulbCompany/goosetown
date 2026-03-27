## ApartmentMap — interior scene script for the per-user apartment.
##
## 24x16 tile grid (768x512 px at 32px/tile, rendered at 2x zoom = 1536x1024).
## Camera is locked to the center of the map.
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


@onready var camera: Camera2D = $Camera2D
@onready var locations: Node2D = $Locations


func _ready() -> void:
	# Lock camera to center — the 2x-zoomed 768x512 map fills most viewports.
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
			var agent_mgr: Node = %AgentManager
			agent_mgr.setup_viewer(viewer)
			viewer.connect_to_server(js_ws_url)
			print("[ApartmentMap] Viewer connecting to: %s" % js_ws_url)


## Returns the world-space position of a named location spot.
## Falls back to the exit marker when the name is unrecognised.
## Uses dynamic lookup from Locations child nodes instead of hardcoded refs.
func get_location_position(location_name: String) -> Vector2:
	var marker: Node = locations.get_node_or_null(location_name)
	if marker:
		return marker.global_position
	# Fallback to exit
	var exit_marker: Node = locations.get_node_or_null("exit")
	if exit_marker:
		return exit_marker.global_position
	return Vector2(MAP_SIZE.x / 2.0, MAP_SIZE.y - TILE_SIZE)
