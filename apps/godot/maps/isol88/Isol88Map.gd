extends Node2D

const MAP_SIZE := Vector2i(1408, 864)
const TILE_SIZE := 32
const MAP_TILES := Vector2i(44, 27)
const DEFAULT_ZOOM := 2.0
const MIN_ZOOM := 0.75
const MAX_ZOOM := 4.0

# Time-of-day tileset variants (all 160x640, only colors differ)
const TILESET_TEXTURES: Dictionary = {
	"dawn":  preload("res://assets/isol88/tilesets/tileset_dawn.png"),
	"day":   preload("res://assets/isol88/tilesets/tileset_day.png"),
	"dusk":  preload("res://assets/isol88/tilesets/tileset_dusk.png"),
	"night": preload("res://assets/isol88/tilesets/tileset_night.png"),
}

@onready var camera: Camera2D = $Camera2D
@onready var player_spawn: Marker2D = $Markers/PlayerSpawn
@onready var plaza: Marker2D = $Markers/Plaza
@onready var library: Marker2D = $Markers/Library
@onready var cafe: Marker2D = $Markers/Cafe
@onready var activity_center: Marker2D = $Markers/ActivityCenter
@onready var bank: Marker2D = $Markers/Bank
@onready var residence: Marker2D = $Markers/Residence
@onready var _terrain_layer: TileMapLayer = $TerrainLayer

var _dragging: bool = false
var _touches: Dictionary = {}  # touch index -> screen position
var _pinch_start_dist: float = 0.0
var _pinch_start_zoom: float = DEFAULT_ZOOM
var _current_tod: String = ""


func _ready() -> void:
	camera.position = player_spawn.position
	var z0: float = maxf(DEFAULT_ZOOM, _min_zoom())
	camera.zoom = Vector2(z0, z0)

	camera.limit_left = 0
	camera.limit_top = 0
	camera.limit_right = MAP_SIZE.x
	camera.limit_bottom = MAP_SIZE.y

	get_viewport().size_changed.connect(_on_viewport_resized)
	_clamp_camera()

	JsBridge.set_camera_requested.connect(_on_set_camera)

	# AgentManager receives world_state via JsBridge (React forwards from its WebSocket).
	# No ViewerProtocol needed — React manages the WebSocket connection.

	# Apply initial tileset based on wall-clock time
	_current_tod = _get_time_of_day()
	_apply_tileset_texture(_current_tod)


func _get_time_of_day() -> String:
	## Map local wall-clock hour to a time-of-day variant.
	## dawn: 5-8, day: 8-17, dusk: 17-20, night: 20-5
	var hour: int = Time.get_datetime_dict_from_system().hour
	if hour >= 5 and hour < 8:
		return "dawn"
	elif hour >= 8 and hour < 17:
		return "day"
	elif hour >= 17 and hour < 20:
		return "dusk"
	else:
		return "night"


func _apply_tileset_texture(tod: String) -> void:
	if not TILESET_TEXTURES.has(tod):
		return
	if _terrain_layer == null or _terrain_layer.tile_set == null:
		return
	var tex: Texture2D = TILESET_TEXTURES[tod]
	var ts: TileSet = _terrain_layer.tile_set
	# Swap the atlas texture on every atlas source
	for i: int in range(ts.get_source_count()):
		var source_id: int = ts.get_source_id(i)
		var source: TileSetSource = ts.get_source(source_id)
		if source is TileSetAtlasSource:
			(source as TileSetAtlasSource).texture = tex


func _process(_delta: float) -> void:
	# Check time-of-day band every frame — _get_time_of_day() is pure arithmetic,
	# no I/O. Only calls _apply_tileset_texture when the band actually changes
	# (which happens at most 4 times per day).
	var tod: String = _get_time_of_day()
	if tod != _current_tod:
		_current_tod = tod
		_apply_tileset_texture(tod)


func _unhandled_input(event: InputEvent) -> void:
	# Click drag to pan
	if event is InputEventMouseButton:
		var mb: InputEventMouseButton = event as InputEventMouseButton
		if mb.button_index == MOUSE_BUTTON_LEFT or mb.button_index == MOUSE_BUTTON_MIDDLE:
			_dragging = mb.pressed

	if event is InputEventMouseMotion and _dragging and _touches.size() < 2:
		var mm: InputEventMouseMotion = event as InputEventMouseMotion
		camera.position -= mm.relative / camera.zoom
		_clamp_camera()

	# Consume scroll/pan gestures so the page doesn't scroll
	if event is InputEventMouseButton:
		var mb2: InputEventMouseButton = event as InputEventMouseButton
		if mb2.button_index == MOUSE_BUTTON_WHEEL_UP or mb2.button_index == MOUSE_BUTTON_WHEEL_DOWN:
			get_viewport().set_input_as_handled()

	if event is InputEventPanGesture:
		# Use trackpad pan gesture to pan the camera
		var pg: InputEventPanGesture = event as InputEventPanGesture
		camera.position += pg.delta * 10.0 / camera.zoom
		_clamp_camera()
		get_viewport().set_input_as_handled()

	# Touch: two-finger pinch zoom. Single-finger pan arrives via Godot's
	# emulate_mouse_from_touch (project default) through the mouse path above.
	if event is InputEventScreenTouch:
		var st: InputEventScreenTouch = event as InputEventScreenTouch
		if st.pressed:
			_touches[st.index] = st.position
		else:
			_touches.erase(st.index)
			if _touches.size() < 2:
				_pinch_start_dist = 0.0
		if _touches.size() == 2:
			var keys: Array = _touches.keys()
			var a: Vector2 = _touches[keys[0]]
			var b: Vector2 = _touches[keys[1]]
			_pinch_start_dist = a.distance_to(b)
			_pinch_start_zoom = camera.zoom.x
			# Second finger down: stop emulated-mouse panning. Known trade-off:
			# after lifting one finger, the remaining finger can't pan until a
			# fresh touch (emulate_mouse_from_touch only re-arms on a new press).
			_dragging = false

	if event is InputEventScreenDrag:
		var sd: InputEventScreenDrag = event as InputEventScreenDrag
		_touches[sd.index] = sd.position
		if _touches.size() == 2 and _pinch_start_dist > 0.0:
			var keys2: Array = _touches.keys()
			var a2: Vector2 = _touches[keys2[0]]
			var b2: Vector2 = _touches[keys2[1]]
			var dist: float = a2.distance_to(b2)
			var z: float = clampf(_pinch_start_zoom * dist / _pinch_start_dist, _min_zoom(), MAX_ZOOM)
			# Anchor the zoom to the pinch midpoint: keep the world point under
			# the fingers stationary instead of zooming toward camera center.
			var midpoint: Vector2 = (a2 + b2) / 2.0
			var vp_center: Vector2 = get_viewport_rect().size / 2.0
			var world_anchor: Vector2 = camera.position + (midpoint - vp_center) / camera.zoom.x
			camera.zoom = Vector2(z, z)
			camera.position = world_anchor - (midpoint - vp_center) / z
			_clamp_camera()
			get_viewport().set_input_as_handled()


func _clamp_camera() -> void:
	var viewport_size: Vector2 = get_viewport_rect().size / camera.zoom
	var half_vp: Vector2 = viewport_size / 2.0
	camera.position.x = clampf(camera.position.x, half_vp.x, MAP_SIZE.x - half_vp.x)
	camera.position.y = clampf(camera.position.y, half_vp.y, MAP_SIZE.y - half_vp.y)


func _cover_zoom() -> float:
	## Smallest zoom at which the map fills the viewport in both axes.
	## Below this, void shows around the map edges (letterboxing).
	var vp: Vector2 = get_viewport_rect().size
	return maxf(vp.x / float(MAP_SIZE.x), vp.y / float(MAP_SIZE.y))


func _min_zoom() -> float:
	## Effective zoom floor: never below MIN_ZOOM, never below cover zoom —
	## so the map always fills the screen and panning stops at the borders.
	return maxf(MIN_ZOOM, _cover_zoom())


func _on_viewport_resized() -> void:
	## Rotation / browser-chrome changes alter the viewport: re-enforce the
	## cover-zoom floor and border clamp.
	var z: float = clampf(camera.zoom.x, _min_zoom(), MAX_ZOOM)
	camera.zoom = Vector2(z, z)
	_clamp_camera()


func _on_set_camera(x: float, y: float, zoom: float) -> void:
	if x != 0 or y != 0:
		camera.position = Vector2(x, y)
	_clamp_camera()


func get_location_position(location_name: String) -> Vector2:
	match location_name:
		"plaza": return plaza.global_position
		"library": return library.global_position
		"cafe": return cafe.global_position
		"activity_center": return activity_center.global_position
		"bank": return bank.global_position
		"residence": return residence.global_position
		"player_spawn": return player_spawn.global_position
		_: return player_spawn.global_position
