## Tests for agent.gd's content-hugging nameplate + speech bubble.
##
## Regression: the nameplate pill was a fixed 60px Panel and the speech bubble
## a fixed 120x36 NinePatchRect, so long names ("Edgar Allan Poe") clipped and
## short utterances floated in a huge box while long ones overflowed. Both boxes
## are now sized in script to hug their text and stay centered above the agent.
extends GdUnitTestSuite

const AgentScene: PackedScene = preload("res://scenes/agent.tscn")


func _make_agent() -> Node:
	# Adding to the tree gives the Control nodes a theme so get_theme_font()
	# resolves — _resize_* no-ops without it (the guard we rely on in headless).
	var agent: Node = auto_free(AgentScene.instantiate())
	add_child(agent)
	return agent


func test_nameplate_hugs_a_long_name_wider_than_a_short_one() -> void:
	var short_agent: Node = _make_agent()
	short_agent.setup("a", "", 100.0, 100.0, "south", "Al")
	var short_w: float = short_agent.nameplate.size.x

	var long_agent: Node = _make_agent()
	long_agent.setup("b", "", 100.0, 100.0, "south", "Edgar Allan Poe")
	var long_w: float = long_agent.nameplate.size.x

	# The pill grows with the name instead of clipping at a fixed 60px.
	assert_that(long_w).is_greater(short_w)
	assert_that(long_w).is_greater(60.0)


func test_nameplate_is_centered_above_the_agent() -> void:
	var agent: Node = _make_agent()
	agent.setup("c", "", 100.0, 100.0, "south", "Athena")
	var np: Panel = agent.nameplate
	# Centered horizontally on the agent's local origin: left = -width/2.
	assert_float(np.position.x).is_equal_approx(-np.size.x * 0.5, 0.5)
	# Sits above the head: bottom edge at the configured slot.
	assert_float(np.position.y + np.size.y).is_equal_approx(-34.0, 0.5)


func test_speech_bubble_grows_for_a_long_line() -> void:
	var agent: Node = _make_agent()

	agent.update_state({"speechText": "Hi"})
	var short_size: Vector2 = agent.speech_container.size

	agent.update_state(
		{
			"speechText": (
				"I have a rather long thing to say that will certainly need to wrap"
				+ " across several lines inside this bubble."
			)
		}
	)
	var long_size: Vector2 = agent.speech_container.size

	# A wrapped multi-line line is taller than a two-character one, and the box
	# tracks it instead of staying a fixed 120x36.
	assert_that(long_size.y).is_greater(short_size.y)
	# Width is bounded by the wrap cap (max text width + padding).
	assert_that(long_size.x).is_less_equal(130.0 + 18.0 + 0.5)


func test_speech_bubble_is_centered_and_above_head() -> void:
	var agent: Node = _make_agent()
	agent.update_state({"speechText": "Hello there"})
	var bubble: NinePatchRect = agent.speech_container
	assert_float(bubble.position.x).is_equal_approx(-bubble.size.x * 0.5, 0.5)
	assert_float(bubble.position.y + bubble.size.y).is_equal_approx(-42.0, 0.5)


func test_speech_text_is_dark_not_white_on_white() -> void:
	# Regression: the speech bubble's Label had no font_color override, so it
	# defaulted to (near-)white text on the white bubble — visible as empty
	# boxes in the town. It must be dark enough to read on white.
	var agent: Node = _make_agent()
	var color: Color = agent.speech_text_label.get_theme_color("font_color")
	# Luminance well below the white bubble — i.e. actually readable.
	assert_float(color.get_luminance()).is_less(0.3)
