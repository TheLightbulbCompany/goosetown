## Agent node: represents a character on the map.
## Spawned by AgentManager when the JS bridge requests it.
## Loads a walk spritesheet from a URL (288x192: 6 frames x 4 directions, 48px each).
## Moves toward a target position with animation.
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

# Animation state
var _spritesheet: Texture2D = null
var _anim_frame: int = 0
var _anim_timer: float = 0.0
const ANIM_FPS: float = 8.0
var _is_moving: bool = false
var _use_navigation: bool = false  # True in headless/server mode


func setup(id: String, url: String, x: float, y: float, facing_json: String, display: String) -> void:
	agent_id = id
	sprite_url = url
	display_name = display
	position = Vector2(x, y)
	target_position = position
	name_label.text = display_name
	speech_bubble.visible = false

	_parse_facing(facing_json)

	if sprite_url != "" and not OS.has_feature("headless"):
		_load_sprite_from_url(sprite_url)

	_update_sprite_frame()


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


func _physics_process(delta: float) -> void:
	if _use_navigation:
		# Navigation-based movement (server and viewer use the same code)
		if not nav_agent.is_navigation_finished():
			var next_pos: Vector2 = nav_agent.get_next_path_position()
			var direction: Vector2 = (next_pos - global_position).normalized()
			velocity = direction * move_speed
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
	if _spritesheet != null:
		if _is_moving:
			_anim_timer += delta
			if _anim_timer >= 1.0 / ANIM_FPS:
				_anim_timer -= 1.0 / ANIM_FPS
				_anim_frame = (_anim_frame + 1) % FRAME_COUNT
				_update_sprite_frame()
		else:
			if _anim_frame != 0:
				_anim_frame = 0
				_update_sprite_frame()


func _update_sprite_frame() -> void:
	if _spritesheet == null:
		return

	var row: int = DIR_ROW.get(facing, 0)
	var region := Rect2(
		_anim_frame * FRAME_SIZE,
		row * FRAME_SIZE,
		FRAME_SIZE,
		FRAME_SIZE,
	)

	# Use AtlasTexture to show just one frame from the spritesheet
	var atlas := AtlasTexture.new()
	atlas.atlas = _spritesheet
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


func set_spritesheet(tex: Texture2D) -> void:
	_spritesheet = tex
	_anim_frame = 0
	_update_sprite_frame()
