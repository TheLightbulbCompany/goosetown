## A brief in-world economy cue (Label) anchored above an agent. Plays a short
## tween then frees itself. style_for() is the pure type→visual mapping.
extends Node2D

const RISE_PX: float = 24.0
const LIFETIME: float = 1.2

static func style_for(cue_type: String) -> Dictionary:
	match cue_type:
		"posted":
			return {"glyph": "!", "color": Color(0.85, 0.74, 0.45)}
		"awarded":
			return {"glyph": "★", "color": Color(1.0, 0.84, 0.3)}
		"payout":
			return {"glyph": "+", "color": Color(1.0, 0.84, 0.3)}
		_:
			return {}

## type: cue type; amount: crumb figure or -1 if none; from_pos/to_pos: world
## positions (to_pos used for the payout arc; equal to from_pos otherwise).
func play(cue_type: String, amount: int, from_pos: Vector2, to_pos: Vector2) -> void:
	var style: Dictionary = style_for(cue_type)
	if style.is_empty():
		queue_free()
		return
	var label := Label.new()
	var glyph: String = str(style["glyph"])
	label.text = glyph if amount < 0 else "%s%d" % [glyph, amount]
	label.modulate = style["color"]
	label.z_index = 20
	add_child(label)
	global_position = from_pos + Vector2(0, -40)
	var target := (to_pos if cue_type == "payout" else from_pos) + Vector2(0, -40 - RISE_PX)
	var tween := create_tween()
	tween.set_parallel(true)
	tween.tween_property(self, "global_position", target, LIFETIME)
	tween.tween_property(label, "modulate:a", 0.0, LIFETIME)
	tween.chain().tween_callback(queue_free)
