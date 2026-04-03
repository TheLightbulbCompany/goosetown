## Agent node: represents a character on the map.
## Spawned by AgentManager when the JS bridge requests it.
## Loads named animation spritesheets (288x192 default: 6 frames x 4 directions, 48px each).
## Supports multiple animations (walk, idle, run, drink, etc.) with per-animation frame counts and FPS.
extends CharacterBody2D

@onready var sprite: Sprite2D = $Sprite
@onready var name_label: Label = $NameLabel
@onready var speech_bubble: Label = $SpeechBubble
@onready var nav_agent: NavigationAgent2D = $NavigationAgent2D

var agent_id: String = ""
var display_name: String = ""
var sprite_url: String = ""
var target_position: Vector2 = Vector2.ZERO
var move_speed: float = 100.0
var facing: String = "south"
var speech_text: String = ""

# Spritesheet constants (matches backend sprite_storage.py)
const FRAME_SIZE: int = 48
const FRAME_COUNT: int = 6
const SHEET_WIDTH: int = 288  # 6 * 48
const SHEET_HEIGHT: int = 192  # 4 * 48

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
}

# Animation state
## animation_name -> Texture2D
var _spritesheets: Dictionary = {}
## animation_name -> { "frame_count": int, "fps": float }
var _anim_configs: Dictionary = {}
## Currently playing animation name
var _current_animation: String = "walk"
var _anim_frame: int = 0
var _anim_timer: float = 0.0
const DEFAULT_FPS: float = 8.0
const DEFAULT_FRAME_COUNT: int = 6
## Sprite animation metadata from backend (raw dict for world_state broadcast)
var sprite_animations: Dictionary = {}
var _is_moving: bool = false
var _use_navigation: bool = false  # True in headless/server mode
var current_activity: String = ""


func setup(id: String, url: String, x: float, y: float, facing_json: String, display: String) -> void:
	agent_id = id
	sprite_url = url
	display_name = display
	position = Vector2(x, y)
	target_position = position
	if name_label:
		name_label.text = display_name
	if speech_bubble:
		speech_bubble.visible = false

	_parse_facing(facing_json)

	if sprite_url != "" and not OS.has_feature("headless"):
		_load_sprite_from_url(sprite_url)

	_update_sprite_frame()


func _ready() -> void:
	# Set label text after node enters tree (@onready vars now valid)
	if name_label and display_name != "":
		name_label.text = display_name
	if speech_bubble:
		speech_bubble.visible = false
	# Connect avoidance callback for RVO steering
	nav_agent.velocity_computed.connect(_on_velocity_computed)


func move_to(x: float, y: float, facing_json: String, speed: float) -> void:
	target_position = Vector2(x, y)
	if speed > 0:
		move_speed = speed
	_parse_facing(facing_json)


func update_state(data: Dictionary) -> void:
	var text: String = str(data.get("speechText", ""))
	if text != "" and text != speech_text:
		speech_text = text
		speech_bubble.text = speech_text
		speech_bubble.visible = true
	elif text == "":
		speech_text = ""
		speech_bubble.visible = false


func set_navigation_target(target: Vector2) -> void:
	_use_navigation = true
	nav_agent.target_position = target


func _update_facing_from_velocity(vel: Vector2) -> void:
	if abs(vel.x) > abs(vel.y):
		facing = "east" if vel.x > 0 else "west"
	else:
		facing = "south" if vel.y > 0 else "north"


func _on_velocity_computed(safe_velocity: Vector2) -> void:
	# Called by NavigationServer when avoidance is enabled
	if is_queued_for_deletion() or not is_inside_tree():
		return
	if not _use_navigation or nav_agent.is_navigation_finished():
		return
	velocity = safe_velocity
	_update_facing_from_velocity(velocity)
	move_and_slide()


func _physics_process(delta: float) -> void:
	if _use_navigation:
		# Navigation-based movement (server and viewer use the same code)
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
		# Direct movement (viewer/client mode)
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
	var region := Rect2(
		safe_frame * FRAME_SIZE,
		row * FRAME_SIZE,
		FRAME_SIZE,
		FRAME_SIZE,
	)
	var atlas := AtlasTexture.new()
	atlas.atlas = tex
	atlas.region = region
	sprite.texture = atlas


func _parse_facing(facing_str: String) -> void:
	if facing_str == "":
		return
	# Accept plain direction strings (e.g. "south") from game server
	if facing_str in DIR_ROW:
		facing = facing_str
		return
	# Also accept JSON object format (e.g. {"direction": "south"}) for legacy callers
	var parsed = JSON.parse_string(facing_str)
	if parsed is Dictionary:
		var dir: String = str(parsed.get("direction", ""))
		if dir in DIR_ROW:
			facing = dir


func _load_sprite_from_url(url: String) -> void:
	if not OS.has_feature("web"):
		return

	var js_code: String = """
	(async function() {
		try {
			var resp = await fetch('%s');
			var blob = await resp.blob();
			var reader = new FileReader();
			reader.onload = function() {
				var base64 = reader.result.split(',')[1];
				if (window.godotUpdateAgentSprite) {
					window.godotUpdateAgentSprite(JSON.stringify({id: '%s', base64: base64}));
				}
			};
			reader.readAsDataURL(blob);
		} catch(e) {
			console.warn('Failed to load sprite:', e);
		}
	})();
	""" % [url, agent_id]
	JavaScriptBridge.eval(js_code)


func set_spritesheet(tex: Texture2D, animation_name: String = "walk") -> void:
	_spritesheets[animation_name] = tex
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
	_current_animation = animation_name
	_anim_frame = 0
	_anim_timer = 0.0
	_update_sprite_frame()
