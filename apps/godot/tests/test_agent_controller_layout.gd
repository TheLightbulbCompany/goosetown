## Tests for ``_compute_layout_targets`` — the pure geometry helper behind
## the ``layout_conversation`` server command.
##
## The game server uses this to decide where each participant in a chat
## should stand so the group reads as an axis-aligned pair (2 agents) or a
## centered circle (3+). Buggy output shows up as overlapping sprites,
## agents facing into walls, or jarring long walks when a chat starts —
## none of which are easy to see in a unit test, so we cover the geometry.
extends GdUnitTestSuite

const AgentControllerScript := preload("res://scripts/server/agent_controller.gd")


# ---------------------------------------------------------------------------
# 2-agent layouts
# ---------------------------------------------------------------------------


func test_two_agents_horizontal_separation_lays_out_along_x() -> void:
	## When the pair is further apart horizontally than vertically, the
	## layout keeps them on the x-axis and centers them on the midpoint.
	var positions := {
		"a": Vector2(100, 200),
		"b": Vector2(300, 220),
	}
	var targets := AgentControllerScript._compute_layout_targets(["a", "b"], positions)

	var midpoint := Vector2(200, 210)
	var spacing: float = AgentControllerScript.CONVERSATION_SPACING
	# a was left, so a stays left of the midpoint
	assert_that(targets["a"]).is_equal(midpoint + Vector2(-spacing / 2.0, 0))
	assert_that(targets["b"]).is_equal(midpoint + Vector2(spacing / 2.0, 0))


func test_two_agents_vertical_separation_lays_out_along_y() -> void:
	## When the pair is further apart vertically than horizontally, the
	## layout keeps them on the y-axis instead.
	var positions := {
		"a": Vector2(250, 100),
		"b": Vector2(260, 400),
	}
	var targets := AgentControllerScript._compute_layout_targets(["a", "b"], positions)

	var midpoint := Vector2(255, 250)
	var spacing: float = AgentControllerScript.CONVERSATION_SPACING
	assert_that(targets["a"]).is_equal(midpoint + Vector2(0, -spacing / 2.0))
	assert_that(targets["b"]).is_equal(midpoint + Vector2(0, spacing / 2.0))


func test_two_agents_preserve_left_right_ordering_after_layout() -> void:
	## The agent that was to the east stays to the east. Preserves visual
	## continuity with "who walked to whom" — swapping would read as a
	## teleport.
	var positions := {
		"alice": Vector2(400, 300),
		"bob": Vector2(100, 310),
	}
	var targets := AgentControllerScript._compute_layout_targets(["alice", "bob"], positions)
	# Alice started east (x=400), Bob started west (x=100). After layout
	# alice.x > bob.x still.
	assert_that(targets["alice"].x).is_greater(targets["bob"].x)


func test_two_agents_same_tile_still_separate() -> void:
	## Same-tile spawn bug: if both agents are at the exact same pixel the
	## layout should still produce two distinct target positions
	## CONVERSATION_SPACING apart, not collapse to the same point.
	var positions := {
		"a": Vector2(270, 453),
		"b": Vector2(270, 453),
	}
	var targets := AgentControllerScript._compute_layout_targets(["a", "b"], positions)
	var distance: float = targets["a"].distance_to(targets["b"])
	assert_that(distance).is_equal(AgentControllerScript.CONVERSATION_SPACING)


# ---------------------------------------------------------------------------
# 3+ agent layouts
# ---------------------------------------------------------------------------


func test_three_agents_lay_out_on_circle_around_centroid() -> void:
	## Group conversations: every agent lands on a circle of
	## CONVERSATION_CIRCLE_RADIUS around the group centroid.
	var positions := {
		"a": Vector2(100, 100),
		"b": Vector2(400, 100),
		"c": Vector2(250, 500),
	}
	var targets := AgentControllerScript._compute_layout_targets(["a", "b", "c"], positions)

	var expected_centroid := Vector2(250, 700.0 / 3.0)
	var radius: float = AgentControllerScript.CONVERSATION_CIRCLE_RADIUS
	# Each target sits at exactly `radius` from the centroid.
	for aid in ["a", "b", "c"]:
		var dist: float = targets[aid].distance_to(expected_centroid)
		assert_float(dist).is_equal_approx(radius, 0.001)


func test_four_agents_spaced_at_90_degree_intervals() -> void:
	## 4 participants should sit at cardinal points on the circle. The
	## backing math is (TAU * i) / n, so for n=4 each angular step is 90°.
	var c := Vector2(500, 500)
	var positions := {
		"a": c + Vector2(100, 0),
		"b": c + Vector2(-100, 0),
		"c": c + Vector2(0, 100),
		"d": c + Vector2(0, -100),
	}
	var targets := AgentControllerScript._compute_layout_targets(["a", "b", "c", "d"], positions)

	# Centroid of the 4 input points is `c`.
	var radius: float = AgentControllerScript.CONVERSATION_CIRCLE_RADIUS
	# Each target at radius from centroid.
	for aid in ["a", "b", "c", "d"]:
		var dist: float = targets[aid].distance_to(c)
		assert_float(dist).is_equal_approx(radius, 0.001)

	# Neighboring targets on the circle are CONVERSATION_CIRCLE_RADIUS * sqrt(2)
	# apart for a 4-point uniform circle. Only checks one pair; enough to
	# confirm the agents are actually spread around the circle and not
	# collapsed to one point.
	var d_ab: float = targets["a"].distance_to(targets["b"])
	var expected: float = radius * sqrt(2.0)
	assert_float(d_ab).is_equal_approx(expected, 0.001)


# ---------------------------------------------------------------------------
# Degenerate inputs
# ---------------------------------------------------------------------------


func test_single_agent_returns_empty_dict() -> void:
	## A 1-agent "conversation" has no meaningful layout; return empty so
	## the caller can short-circuit rather than emitting a move command.
	var targets := AgentControllerScript._compute_layout_targets(
		["only"], {"only": Vector2.ZERO}
	)
	assert_that(targets).is_empty()


func test_missing_position_keys_filtered_out() -> void:
	## If an agent id is in the requested list but missing from the
	## position map (e.g. it disconnected mid-chat), the helper should
	## skip it. 3 ids with 1 missing → still do the 2-agent layout path.
	var positions := {
		"a": Vector2(100, 100),
		"c": Vector2(300, 110),
	}
	var targets := AgentControllerScript._compute_layout_targets(
		["a", "ghost", "c"], positions
	)
	assert_that(targets.size()).is_equal(2)
	assert_that(targets.has("ghost")).is_false()


func test_empty_input_returns_empty() -> void:
	var targets := AgentControllerScript._compute_layout_targets([], {})
	assert_that(targets).is_empty()


# ---------------------------------------------------------------------------
# _targets_share_axis
# ---------------------------------------------------------------------------


func test_share_axis_true_when_xs_match() -> void:
	## Vertical layout case — both targets share an x, differ on y.
	var targets := {"a": Vector2(100, 200), "b": Vector2(100, 400)}
	assert_that(AgentControllerScript._targets_share_axis(targets)).is_true()


func test_share_axis_true_when_ys_match() -> void:
	## Horizontal layout case — both targets share a y, differ on x.
	var targets := {"a": Vector2(100, 300), "b": Vector2(400, 300)}
	assert_that(AgentControllerScript._targets_share_axis(targets)).is_true()


func test_share_axis_true_within_epsilon() -> void:
	## 2px sub-pixel drift still counts as aligned. Default epsilon=2.0.
	var targets := {"a": Vector2(100, 200), "b": Vector2(101, 400)}
	assert_that(AgentControllerScript._targets_share_axis(targets)).is_true()


func test_share_axis_false_when_clamp_drifted_both_axes() -> void:
	## Simulates the post-navmesh-clamp failure mode: one target got
	## pushed off the intended axis by a building clamp, so neither x nor
	## y match anymore. Caller should fall back to "don't walk".
	var targets := {"a": Vector2(100, 200), "b": Vector2(150, 250)}
	assert_that(AgentControllerScript._targets_share_axis(targets)).is_false()


func test_share_axis_false_for_non_two_agents() -> void:
	## Alignment check only applies to the 2-agent case — 3+ agents use
	## the circle layout, which has no shared axis. Returns false so
	## callers know to skip the check in that branch.
	var targets := {
		"a": Vector2(100, 200),
		"b": Vector2(100, 400),
		"c": Vector2(100, 600),
	}
	assert_that(AgentControllerScript._targets_share_axis(targets)).is_false()


func test_share_axis_false_on_empty() -> void:
	assert_that(AgentControllerScript._targets_share_axis({})).is_false()
