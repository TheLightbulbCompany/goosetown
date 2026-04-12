extends Node2D

const MAP_SIZE := Vector2i(1408, 864)
const TILE_SIZE := 32
const MAP_TILES := Vector2i(44, 27)
const DEFAULT_ZOOM := 2.0
# 24 real minutes = 24 game hours (1 real second = 1 game minute)
const CYCLE_SECONDS: float = 1440.0

@onready var camera: Camera2D = $Camera2D
@onready var player_spawn: Marker2D = $Markers/PlayerSpawn
@onready var plaza: Marker2D = $Markers/Plaza
@onready var library: Marker2D = $Markers/Library
@onready var cafe: Marker2D = $Markers/Cafe
@onready var activity_center: Marker2D = $Markers/ActivityCenter
@onready var bank: Marker2D = $Markers/Bank
@onready var residence: Marker2D = $Markers/Residence

var _dragging: bool = false
var _day_night_overlay: ColorRect


func _ready() -> void:
	camera.position = player_spawn.position
	camera.zoom = Vector2(DEFAULT_ZOOM, DEFAULT_ZOOM)

	camera.limit_left = 0
	camera.limit_top = 0
	camera.limit_right = MAP_SIZE.x
	camera.limit_bottom = MAP_SIZE.y

	JsBridge.set_camera_requested.connect(_on_set_camera)

	# AgentManager receives world_state via JsBridge (React forwards from its WebSocket).
	# No ViewerProtocol needed — React manages the WebSocket connection.

	_setup_day_night_overlay()


func _setup_day_night_overlay() -> void:
	# Build the overlay in code rather than the .tscn to avoid Godot UID assignment
	# requirements for ShaderMaterial references in text scene files.
	var layer := CanvasLayer.new()
	layer.name = "DayNightLayer"
	layer.layer = 10
	add_child(layer)

	_day_night_overlay = ColorRect.new()
	_day_night_overlay.name = "DayNightOverlay"
	_day_night_overlay.set_anchors_preset(Control.PRESET_FULL_RECT)
	# Must ignore mouse input — otherwise the full-screen rect swallows all clicks
	# and breaks click-to-pan on the map.
	_day_night_overlay.mouse_filter = Control.MOUSE_FILTER_IGNORE

	var mat := ShaderMaterial.new()
	mat.shader = load("res://shaders/daynight.gdshader")
	_day_night_overlay.material = mat

	layer.add_child(_day_night_overlay)


func _process(_delta: float) -> void:
	if _day_night_overlay and _day_night_overlay.material is ShaderMaterial:
		var t: float = fmod(Time.get_unix_time_from_system(), CYCLE_SECONDS) / CYCLE_SECONDS
		(_day_night_overlay.material as ShaderMaterial).set_shader_parameter("time_of_day", t)


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
		# Use trackpad pan gesture to pan the camera
		var pg: InputEventPanGesture = event as InputEventPanGesture
		camera.position += pg.delta * 10.0 / camera.zoom
		_clamp_camera()
		get_viewport().set_input_as_handled()


func _clamp_camera() -> void:
	var viewport_size: Vector2 = get_viewport_rect().size / camera.zoom
	var half_vp: Vector2 = viewport_size / 2.0
	camera.position.x = clampf(camera.position.x, half_vp.x, MAP_SIZE.x - half_vp.x)
	camera.position.y = clampf(camera.position.y, half_vp.y, MAP_SIZE.y - half_vp.y)


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
