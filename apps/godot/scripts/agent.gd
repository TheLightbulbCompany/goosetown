## Agent node: represents a character on the map.
## Spawned by AgentManager when the JS bridge requests it.
## Loads named animation spritesheets (288x192 default: 6 frames x 4 directions, 48px each).
## Supports multiple animations (walk, idle, run, drink, etc.) with per-animation frame counts and FPS.
extends CharacterBody2D

# ---------------------------------------------------------------------------
# Constants and enums
# ---------------------------------------------------------------------------

# Spritesheet constants (matches backend sprite_storage.py)
const FRAME_SIZE: int = 96
const FRAME_COUNT: int = 6
const SHEET_WIDTH: int = 576  # 6 * 96
const SHEET_HEIGHT: int = 384  # 4 * 96

const DEFAULT_FPS: float = 8.0
const DEFAULT_FRAME_COUNT: int = 6
const SHADOW_TEXTURE: Texture2D = preload("res://assets/sprites/shadow_placeholder.png")

# Direction → row index in spritesheet
const DIR_ROW: Dictionary = {
	"south": 0,
	"west": 1,
	"east": 2,
	"north": 3,
}

# Activity name → animation asset name (from backend ANIMATION_REGISTRY)
const ACTIVITY_ANIMATION: Dictionary = {
	"people_watch": "sitting",
	"perform": "performing",
	"read": "reading",
	"study": "reading",
	"write_journal": "writing",
	"order_coffee": "drink",
	"sit_and_chat": "sitting",
	"work_on_laptop": "working",
	"exercise": "exercising",
	"play_game": "gaming",
	"garden": "gardening",
	"check_balance": "idle",
	"browse_listings": "reading",
	"sit_outside": "sitting",
	"sleep": "sleeping",
	"nap": "sleeping",
	"work_at_desk": "working",
	"cook": "cooking",
	"watch_tv": "sitting",
	"read_book": "reading",
	"listen_to_music": "sitting",
	"watch_aquarium": "sitting",
	"rest": "idle",
	"talking": "talking",
}

# ---------------------------------------------------------------------------
# Public variables
# ---------------------------------------------------------------------------

var agent_id: String = ""
var display_name: String = ""
var sprite_url: String = ""
var target_position: Vector2 = Vector2.ZERO
var move_speed: float = 100.0
var facing: String = "south"
var speech_text: String = ""

## Sprite animation metadata from backend (raw dict for world_state broadcast)
var sprite_animations: Dictionary = {}
var current_activity: String = ""

# ---------------------------------------------------------------------------
# Private variables
# ---------------------------------------------------------------------------

## animation_name -> Texture2D
var _spritesheets: Dictionary = {}
## animation_name -> { "frame_count": int, "fps": float }
var _anim_configs: Dictionary = {}
## Currently playing animation name
var _current_animation: String = "walk"
var _anim_frame: int = 0
var _anim_timer: float = 0.0
var _is_moving: bool = false
var _idle_bob_timer: float = 0.0
## True when NavigationAgent2D pathfinding is active (set on first set_navigation_target call)
var _use_navigation: bool = false
## Viewer uses snapshot interpolation instead of nav — lerp from last pos to new pos
## over the broadcast interval. Nav on viewer causes circles because server broadcasts
## the agent's *current* position every 2s, which is always a moving target.
var _viewer_interp: bool = false
var _interp_prev: Vector2 = Vector2.ZERO
var _interp_target: Vector2 = Vector2.ZERO
var _interp_start_ms: int = 0
## Nominal time to traverse between two broadcasts. Matches game server broadcast cadence.
const INTERP_DURATION_MS: int = 2000
## Cached AtlasTexture instances keyed by "animation/facing" to avoid per-frame allocations
var _atlas_cache: Dictionary = {}

# ---------------------------------------------------------------------------
# @onready variables
# ---------------------------------------------------------------------------

@onready var sprite: Sprite2D = $Sprite
@onready var nameplate: Control = $NameplateContainer
@onready var name_label: Label = $NameplateContainer/Pill/NameLabel
@onready var speech_container: NinePatchRect = $SpeechBubbleContainer
@onready var speech_text_label: Label = $SpeechBubbleContainer/SpeechText
@onready var nav_agent: NavigationAgent2D = $NavigationAgent2D

# ---------------------------------------------------------------------------
# Built-in virtual methods
# ---------------------------------------------------------------------------

func _ready() -> void:
	if speech_container:
		speech_container.visible = false
	if nameplate:
		nameplate.visible = false
	# Connect avoidance callback for RVO steering
	nav_agent.velocity_computed.connect(_on_velocity_computed)


func _physics_process(delta: float) -> void:
	# Hover check — poll mouse position in WebGL viewer only
	if OS.has_feature("web"):
		_update_hover()

	if _viewer_interp:
		# Viewer mode — snapshot interpolation between server broadcasts.
		# Server broadcasts are authoritative; viewer just smoothly moves
		# from the previous broadcast position to the latest one.
		var elapsed_ms: int = Time.get_ticks_msec() - _interp_start_ms
		var t: float = clampf(float(elapsed_ms) / float(INTERP_DURATION_MS), 0.0, 1.0)
		var new_pos: Vector2 = _interp_prev.lerp(_interp_target, t)
		var delta_vec: Vector2 = new_pos - global_position
		global_position = new_pos
		# _is_moving is "still traveling toward latest broadcast target"
		_is_moving = t < 1.0 and delta_vec.length() > 0.5
		# Update facing from motion direction
		if _is_moving:
			if abs(delta_vec.x) > abs(delta_vec.y):
				facing = "east" if delta_vec.x > 0 else "west"
			else:
				facing = "south" if delta_vec.y > 0 else "north"
	elif _use_navigation:
		# Server mode — pathfinding via NavigationAgent2D.
		if not nav_agent.is_navigation_finished():
			var next_pos: Vector2 = nav_agent.get_next_path_position()
			var desired_velocity: Vector2 = (next_pos - global_position).normalized() * move_speed
			if nav_agent.avoidance_enabled:
				# Let RVO compute a safe velocity via callback
				nav_agent.velocity = desired_velocity
			else:
				velocity = desired_velocity
				_update_facing_from_velocity(velocity)
				move_and_slide()
			_is_moving = true
		else:
			velocity = Vector2.ZERO
			_is_moving = false
	else:
		# Direct movement (no navmesh, no interpolation — used by JS bridge moveAgent)
		var direction: Vector2 = target_position - position
		var distance: float = direction.length()

		if distance > 2.0:
			velocity = direction.normalized() * move_speed
			move_and_slide()
			_is_moving = true

			# Update facing from movement direction
			if abs(direction.x) > abs(direction.y):
				facing = "east" if direction.x > 0 else "west"
			else:
				facing = "south" if direction.y > 0 else "north"
		else:
			velocity = Vector2.ZERO
			_is_moving = false

	# Animate
	if not _spritesheets.is_empty():
		# Determine target animation from activity or movement
		var target_anim: String = "walk"
		if current_activity != "" and current_activity in ACTIVITY_ANIMATION:
			var desired: String = ACTIVITY_ANIMATION[current_activity]
			if _spritesheets.has(desired):
				target_anim = desired
			elif _spritesheets.has("idle"):
				target_anim = "idle"
		elif not _is_moving:
			target_anim = "idle" if _spritesheets.has("idle") else "walk"

		if target_anim != _current_animation and _spritesheets.has(target_anim):
			play_animation(target_anim)

		# Frame cycling
		var cfg: Dictionary = _anim_configs.get(_current_animation, {})
		var fps: float = cfg.get("fps", DEFAULT_FPS)
		var fc: int = cfg.get("frame_count", DEFAULT_FRAME_COUNT)

		if _is_moving or _current_animation != "walk":
			_anim_timer += delta
			if _anim_timer >= 1.0 / fps:
				_anim_timer -= 1.0 / fps
				_anim_frame = (_anim_frame + 1) % fc
				_update_sprite_frame()
		else:
			if _anim_frame != 0:
				_anim_frame = 0
				_update_sprite_frame()

	# Idle bob — subtle vertical sway when stationary
	if not _is_moving and (_current_animation == "idle" or (_current_animation == "walk" and not _is_moving)):
		_idle_bob_timer += delta
		sprite.position.y = -16 + sin(_idle_bob_timer * 2.0) * 1.0
	else:
		_idle_bob_timer = 0.0
		sprite.position.y = -16

# ---------------------------------------------------------------------------
# Public methods
# ---------------------------------------------------------------------------

func setup(id: String, url: String, x: float, y: float, facing_json: String, display: String) -> void:
	agent_id = id
	sprite_url = url
	display_name = display
	position = Vector2(x, y)
	target_position = position
	if name_label:
		name_label.text = display_name
	if speech_container:
		speech_container.visible = false

	_parse_facing(facing_json)

	if sprite_url != "" and not OS.has_feature("headless"):
		_load_sprite_from_url(sprite_url)
	elif not OS.has_feature("headless"):
		# No sprite URL — use shadow placeholder
		set_spritesheet(SHADOW_TEXTURE, "walk")

	_update_sprite_frame()


func move_to(x: float, y: float, facing_json: String, speed: float) -> void:
	target_position = Vector2(x, y)
	if speed > 0:
		move_speed = speed
	_parse_facing(facing_json)


const SPEECH_MAX_CHARS: int = 60

func update_state(data: Dictionary) -> void:
	var text: String = str(data.get("speechText", ""))
	if text != "" and text != speech_text:
		speech_text = text
		# Truncate for the bubble — full text is available in the conversation log.
		var display_text: String = text
		if display_text.length() > SPEECH_MAX_CHARS:
			display_text = display_text.substr(0, SPEECH_MAX_CHARS - 1) + "…"
		speech_text_label.text = display_text
		speech_text_label.clip_text = true
		_show_speech_bubble()
		if nameplate:
			nameplate.visible = false
	elif text == "":
		speech_text = ""
		_hide_speech_bubble()


func _show_speech_bubble() -> void:
	if speech_container == null:
		return
	speech_container.modulate.a = 0.0
	speech_container.visible = true
	var tween: Tween = create_tween()
	tween.tween_property(speech_container, "modulate:a", 1.0, 0.3)


func _hide_speech_bubble() -> void:
	if speech_container == null or not speech_container.visible:
		return
	var tween: Tween = create_tween()
	tween.tween_property(speech_container, "modulate:a", 0.0, 0.3)
	tween.tween_callback(func(): speech_container.visible = false)


func set_navigation_target(target: Vector2) -> void:
	## Server-mode pathfinding via NavigationAgent2D. The headless game server
	## uses this when an agent issues a move command — nav plots a path around
	## buildings on the navmesh.
	##
	## In the viewer (WebGL), this is called by agent_manager on every
	## world_state broadcast with the agent's CURRENT position. Using nav
	## against a moving target causes endless re-planning (circles), so the
	## viewer routes through snapshot interpolation instead.
	if OS.has_feature("web"):
		_set_interp_target(target)
		return
	_use_navigation = true
	nav_agent.target_position = target


func _set_interp_target(target: Vector2) -> void:
	## Set a new snapshot target for viewer-mode interpolation.
	## Captures current visual position as prev so the lerp is continuous.
	_viewer_interp = true
	_interp_prev = global_position
	_interp_target = target
	_interp_start_ms = Time.get_ticks_msec()


func set_spritesheet(tex: Texture2D, animation_name: String = "walk") -> void:
	_spritesheets[animation_name] = tex
	# Invalidate cached AtlasTextures for this animation so they are rebuilt
	# with the new texture on next _update_sprite_frame call.
	for key in _atlas_cache.keys():
		if (key as String).begins_with(animation_name + "/"):
			_atlas_cache.erase(key)
	if animation_name == _current_animation or _spritesheets.size() == 1:
		_current_animation = animation_name
		_anim_frame = 0
		_update_sprite_frame()


func set_animation_configs(configs: Dictionary) -> void:
	## configs: { "walk": { "frameCount": 6, "fps": 8.0 }, ... }
	for anim_name in configs:
		var cfg: Dictionary = configs[anim_name]
		_anim_configs[anim_name] = {
			"frame_count": int(cfg.get("frameCount", DEFAULT_FRAME_COUNT)),
			"fps": float(cfg.get("fps", DEFAULT_FPS)),
		}


func play_animation(animation_name: String) -> void:
	if animation_name == _current_animation:
		return
	if not _spritesheets.has(animation_name):
		return
	var was_walk: bool = _current_animation == "walk"
	var going_to_activity: bool = animation_name != "walk" and animation_name != "idle"
	_current_animation = animation_name
	_anim_frame = 0
	if was_walk and going_to_activity and _spritesheets.has("idle"):
		_anim_timer = -0.2
	else:
		_anim_timer = 0.0
	_update_sprite_frame()

# ---------------------------------------------------------------------------
# Private methods
# ---------------------------------------------------------------------------

func _update_hover() -> void:
	# Poll mouse position — Godot WebGL Area2D input_pickable is unreliable
	if speech_text != "":
		return  # Speech takes priority over name
	var mouse_pos: Vector2 = get_global_mouse_position()
	# Sprite rect: ~48px wide, ~48px tall, positioned 16px above body center
	var rect := Rect2(global_position.x - 24, global_position.y - 40, 48, 48)
	nameplate.visible = rect.has_point(mouse_pos)


func _update_facing_from_velocity(vel: Vector2) -> void:
	if abs(vel.x) > abs(vel.y):
		facing = "east" if vel.x > 0 else "west"
	else:
		facing = "south" if vel.y > 0 else "north"


func _update_sprite_frame() -> void:
	var tex: Texture2D = _spritesheets.get(_current_animation)
	if tex == null:
		if _spritesheets.is_empty():
			return
		tex = _spritesheets.values()[0]

	var cfg: Dictionary = _anim_configs.get(_current_animation, {})
	var fc: int = cfg.get("frame_count", DEFAULT_FRAME_COUNT)
	var safe_frame: int = _anim_frame % fc

	var row: int = DIR_ROW.get(facing, 0)

	# Build a cache key from animation, facing, and frame index so each unique
	# region gets exactly one AtlasTexture allocation for the lifetime of the node.
	var cache_key: String = "%s/%s/%d" % [_current_animation, facing, safe_frame]
	var atlas: AtlasTexture = _atlas_cache.get(cache_key)
	if atlas == null:
		atlas = AtlasTexture.new()
		atlas.atlas = tex
		atlas.region = Rect2(
			safe_frame * FRAME_SIZE,
			row * FRAME_SIZE,
			FRAME_SIZE,
			FRAME_SIZE,
		)
		_atlas_cache[cache_key] = atlas

	sprite.texture = atlas


func _parse_facing(facing_str: String) -> void:
	if facing_str == "":
		return
	# Accept plain direction strings (e.g. "south") from game server
	if facing_str in DIR_ROW:
		facing = facing_str
		return
	# Also accept JSON object format (e.g. {"direction": "south"}) for legacy callers
	var parsed: Variant = JSON.parse_string(facing_str)
	if parsed is Dictionary:
		var dir: String = str(parsed.get("direction", ""))
		if dir in DIR_ROW:
			facing = dir


func _load_sprite_from_url(url: String) -> void:
	if not OS.has_feature("web"):
		return
	JsBridge.call_js("load_sprite", {"url": url, "agent_id": agent_id})

# ---------------------------------------------------------------------------
# Signal callbacks
# ---------------------------------------------------------------------------

func _on_velocity_computed(safe_velocity: Vector2) -> void:
	# Called by NavigationServer when avoidance is enabled
	if is_queued_for_deletion() or not is_inside_tree():
		return
	if not _use_navigation or nav_agent.is_navigation_finished():
		return
	velocity = safe_velocity
	_update_facing_from_velocity(velocity)
	move_and_slide()
