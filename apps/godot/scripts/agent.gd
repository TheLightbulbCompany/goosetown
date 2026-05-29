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
## Threshold (in pixels) for a journey to count as "walking" in viewer mode.
## See ``_physics_process``'s _viewer_interp branch — anything below this is
## treated as a same-tile micro-adjustment (idle), anything above triggers the
## walk animation. 4 px ≈ 1/8 of a tile, well below any real walk distance.
const MIN_WALK_INTERP_DIST: float = 4.0
## How long a viewer-side interp can sit without a fresh broadcast before we
## stop trying to lerp and snap to the latest target. Was ``INTERP_DURATION_MS * 2``
## (4 s) which fired on every minor network jitter — agents teleported visibly.
## 30 s gives normal network jitter ~15× headroom while still catching the real
## case (browser tab backgrounded, render loop frozen, broadcasts queue up).
const STALE_INTERP_THRESHOLD_MS: int = 30000
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
		# _is_moving = "still traveling toward latest broadcast target".
		# Key off the *total* interpolation distance, not the per-tick delta.
		# At normal walking speeds (~30-60 px/sec over a 2 s broadcast window)
		# the per-tick delta lands at ~0.5-1.0 px, which used to false-fail
		# the threshold and freeze the walk-cycle on frame 0 mid-walk. The
		# interp distance, however, captures the journey size — if there's
		# meaningful motion to cover and we haven't completed it, we're
		# walking, regardless of how slow each tick is.
		var interp_dist: float = (_interp_target - _interp_prev).length()
		_is_moving = t < 1.0 and interp_dist > MIN_WALK_INTERP_DIST
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
		load_sprite(sprite_url, "walk")
	elif not OS.has_feature("headless"):
		# No sprite URL — use shadow placeholder
		set_spritesheet(SHADOW_TEXTURE, "walk")

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
		speech_text_label.text = text
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
	##
	## Staleness detection (F7): when the browser backgrounds the tab, both
	## the render loop AND _physics_process freeze, but the WebSocket still
	## delivers world_state broadcasts. Each one calls _set_interp_target
	## with `global_position` as the new prev — which is frozen at whatever
	## position the agent had when the tab went away. Once the tab is
	## refocused the lerp "plays back" the accumulated backlog, walking
	## agents in a straight line through buildings to their current spot.
	##
	## If more than ``STALE_INTERP_THRESHOLD_MS`` has elapsed since the last
	## target, treat this as a wake-from-background and snap instantly
	## instead of lerping across who-knows-how-many minutes of stale state.
	var now_ms: int = Time.get_ticks_msec()
	var stale: bool = _viewer_interp and (now_ms - _interp_start_ms) > STALE_INTERP_THRESHOLD_MS
	_viewer_interp = true
	if stale:
		global_position = target
		_interp_prev = target
	else:
		_interp_prev = global_position
	_interp_target = target
	_interp_start_ms = now_ms


func has_loaded_sprite(animation_name: String) -> bool:
	## Returns true when ``set_spritesheet`` has been called with a real
	## texture for this animation. Used by ``agent_manager.gd`` to detect
	## the case where a previous broadcast advertised a sprite URL but
	## the JS-side fetch dropped (CORS hiccup, browser tab throttled,
	## network blip) and the texture never made it into the agent — so
	## the diff-by-URL retry condition wouldn't refire. With this hook
	## the manager can re-issue the load whenever the texture is missing,
	## regardless of whether the URL "changed."
	return _spritesheets.has(animation_name)


func set_spritesheet(tex: Texture2D, animation_name: String = "walk") -> void:
	_spritesheets[animation_name] = tex
	# Invalidate cached AtlasTextures for this animation so they are rebuilt
	# with the new texture on next _update_sprite_frame call.
	for key in _atlas_cache.keys():
		if (key as String).begins_with(animation_name + "/"):
			_atlas_cache.erase(key)
	# Only force-switch the visible animation when we're updating the sheet
	# that's ALREADY being played. The old `or _spritesheets.size() == 1`
	# shortcut auto-picked whatever loaded first, which pinned idle agents to
	# `talking` whenever `talking.png` loaded before `walk.png` (observed live
	# on 2026-04-22: pam's walk kept erroring, talking succeeded first, and
	# the first-sheet shortcut trapped her in a talking loop that the tick
	# loop couldn't unwind).
	#
	# The default `_current_animation = "walk"` set at the variable
	# declaration is already the correct starting state — no auto-pick
	# needed. The tick loop in `_physics_process` picks the right animation
	# every frame based on activity and motion.
	if animation_name == _current_animation:
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
	# The @onready var `sprite` is only populated once the node enters the tree
	# (after `add_child`). `setup()` is called BEFORE `add_child` by callers
	# like `AgentController.spawn_agent` (agent_controller.gd:124 vs :140), which
	# means setup → set_spritesheet → _update_sprite_frame can fire while
	# `sprite` is still null. Observed on the headless game server as the
	# `agent.gd:457 — assign 'texture' on Nil` script error that fired on every
	# cross-context spawn for spriteless agents (Phase-1 residents), bouncing
	# them out of interior scenes. _physics_process re-runs the frame update
	# every frame once the node is alive, so returning early here is safe — the
	# texture lands the moment @onready has populated.
	if sprite == null:
		return
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


## Sprite load: single public API with retry + shadow fallback.
##
## Previously this lived in two places — `agent.gd:_load_sprite_from_url`
## for the walk spritesheet at setup, and `agent_manager.gd:_load_sprite_for_agent`
## for everything else (idle, talking, runs after a regenerate). The two paths
## did the same JS-bridge call but only the first had retry, which meant
## non-walk animations failed silently with no observability. Unified here
## so one retry implementation covers every sprite the agent ever loads.
##
## The JS-side fetch has no error callback back to GDScript, so we poll
## `_spritesheets` — if nothing arrived after each backoff window, retry.
## After all retries fail, fall back to SHADOW_TEXTURE so at minimum the
## agent renders a visible silhouette instead of a floating name label.
const _SPRITE_LOAD_MAX_ATTEMPTS: int = 3
const _SPRITE_LOAD_BACKOFF_SEC: Array = [2.0, 4.0, 8.0]

## Public API — load (or reload) a spritesheet for a specific animation name.
## Safe to call multiple times for the same anim; the retry coroutine is
## fire-and-forget. Godot holds the coroutine alive via the timer signal it
## awaits, so no explicit reference tracking is needed.
func load_sprite(url: String, anim_name: String = "walk") -> void:
	if url == "" or not OS.has_feature("web"):
		return
	_load_sprite_with_retry(url, anim_name, 0)


## Retry loop for a single anim's fetch. Declared without a return type
## annotation because it's a coroutine (uses `await`); a `-> void`
## annotation makes Godot 4's parser reject fire-and-forget calls from
## `load_sprite`. Called without `await` on purpose — the signal-driven
## timer keeps the coroutine alive until it resolves or the agent is freed.
func _load_sprite_with_retry(url: String, anim_name: String, attempt: int):
	if attempt >= _SPRITE_LOAD_MAX_ATTEMPTS:
		push_warning(
			"Sprite load failed after %d attempts for agent %s anim=%s (%s) — falling back to shadow placeholder"
			% [_SPRITE_LOAD_MAX_ATTEMPTS, agent_id, anim_name, url]
		)
		# Shadow fallback only for walk. Non-walk anims just go missing
		# (the agent will fall back to `walk` at the _physics_process level).
		if anim_name == "walk":
			set_spritesheet(SHADOW_TEXTURE, "walk")
		return
	# Observability: log every JS fetch attempt so we can correlate failures
	# with browser-console `sprite load failed` warnings from the JS side.
	print(
		"Sprite load dispatch: agent=%s anim=%s attempt=%d url=%s"
		% [agent_id, anim_name, attempt + 1, url]
	)
	# Fire the JS fetch — still handled by the load_sprite callback the
	# frontend registers on window. If it succeeds, the JS side calls back
	# into Godot via godotUpdateAgentSprite which ends up in set_spritesheet.
	JsBridge.call_js("load_sprite", {
		"url": url,
		"agent_id": agent_id,
		"animation_name": anim_name,
	})
	# Wait for the response (or silence) and decide whether to retry.
	var backoff: float = _SPRITE_LOAD_BACKOFF_SEC[attempt]
	await get_tree().create_timer(backoff).timeout
	# If the agent was freed mid-wait, bail silently.
	if not is_inside_tree():
		return
	if _spritesheets.has(anim_name) and _spritesheets[anim_name] != SHADOW_TEXTURE:
		return
	print(
		"Sprite load retry %d/%d for agent %s anim=%s (%s)"
		% [attempt + 1, _SPRITE_LOAD_MAX_ATTEMPTS, agent_id, anim_name, url]
	)
	_load_sprite_with_retry(url, anim_name, attempt + 1)

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
