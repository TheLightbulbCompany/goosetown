## Tests for EconomyCue.style_for — the pure type→visual mapping for in-world
## economy cues (posted/awarded/payout).
extends GdUnitTestSuite

const EconomyCue := preload("res://scripts/economy_cue.gd")


func test_payout_is_gold_with_plus() -> void:
	var s: Dictionary = EconomyCue.style_for("payout")
	assert_that(s.has("color")).is_true()
	assert_that(str(s.get("glyph", "")).contains("+")).is_true()


func test_posted_and_awarded_have_styles() -> void:
	assert_that(EconomyCue.style_for("posted").is_empty()).is_false()
	assert_that(EconomyCue.style_for("awarded").is_empty()).is_false()


func test_unknown_type_returns_empty() -> void:
	assert_that(EconomyCue.style_for("nope").is_empty()).is_true()
