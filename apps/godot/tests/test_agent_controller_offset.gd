## Tests for ``_pick_free_slot`` — the pure helper behind
## ``_offset_if_occupied``.
##
## Regression: two agents arriving at the same destination in the same
## physics tick used to compute ``collisions=1`` and ``angle=TAU/6`` each,
## so both ended up on the *same* offset slot ((251, 420) in the 2026-04-20
## dry-run). The new helper picks the first *free* slot deterministically,
## so simultaneous-arrivers land on distinct tiles.
extends GdUnitTestSuite

const AgentControllerScript := preload("res://scripts/server/agent_controller.gd")
const TILE_SIZE: float = 32.0
const SLOT_DISTANCE: float = 1.6 * TILE_SIZE
const RADIUS: float = 1.5 * TILE_SIZE


func test_returns_input_when_nothing_nearby() -> void:
	## No occupants → no offset.
	var pos := Vector2(500, 500)
	var result: Vector2 = AgentControllerScript._pick_free_slot(pos, [], 0)
	assert_that(result).is_equal(pos)


func test_returns_input_when_occupant_outside_radius() -> void:
	## Occupant 2 tiles away (> 1.5 tile radius) doesn't push us off.
	var pos := Vector2(500, 500)
	var occupant := pos + Vector2(2 * TILE_SIZE, 0)
	var result: Vector2 = AgentControllerScript._pick_free_slot(pos, [occupant], 0)
	assert_that(result).is_equal(pos)


func test_picks_slot_1_when_origin_occupied() -> void:
	## Single occupant on origin → first scanned slot is i=1 at angle TAU/6.
	var pos := Vector2(500, 500)
	var occupant := pos
	var result: Vector2 = AgentControllerScript._pick_free_slot(pos, [occupant], 0)

	var expected_angle: float = TAU / 6.0
	var expected := pos + Vector2(cos(expected_angle), sin(expected_angle)) * SLOT_DISTANCE
	assert_that(result.distance_to(expected)).is_less(0.01)


func test_simultaneous_arrival_picks_distinct_slots() -> void:
	## The bug: two agents arriving at the same plaza tile in the same
	## physics tick. When agent A is processed first and snapped to slot 1,
	## agent B should see slot 1 as occupied and skip past it.
	var plaza := Vector2(500, 500)

	# Agent A (peer 1) arrives. Sees only agent B at plaza. Snaps to slot 1.
	var a_target: Vector2 = AgentControllerScript._pick_free_slot(plaza, [plaza], 1)

	# Agent B (peer 2) arrives next in the same tick. Now agent A has been
	# moved to its slot — but agent B is still at plaza, and we exclude B
	# from `occupied` (caller responsibility), so the only occupant B sees
	# is A's snapped position.
	var b_target: Vector2 = AgentControllerScript._pick_free_slot(plaza, [a_target], 2)

	# A and B must end up on distinct tiles.
	assert_that(a_target.distance_to(b_target)).is_greater(TILE_SIZE)


func test_three_agents_get_three_distinct_slots() -> void:
	## Three agents arriving at the same spot all end up on different
	## tiles. Demonstrates the slot scan walks past taken slots.
	var plaza := Vector2(500, 500)
	var a: Vector2 = AgentControllerScript._pick_free_slot(plaza, [plaza], 1)
	var b: Vector2 = AgentControllerScript._pick_free_slot(plaza, [a], 2)
	var c: Vector2 = AgentControllerScript._pick_free_slot(plaza, [a, b], 3)

	assert_that(a.distance_to(b)).is_greater(TILE_SIZE)
	assert_that(a.distance_to(c)).is_greater(TILE_SIZE)
	assert_that(b.distance_to(c)).is_greater(TILE_SIZE)


func test_all_slots_full_uses_seed_for_fallback() -> void:
	## Edge case: pos + every slot occupied. The fallback should depend on
	## ``fallback_seed`` so two callers with different peer ids in the same
	## tick don't both land on the same fallback slot.
	var pos := Vector2(500, 500)
	# Pile occupants on slot 0 and all 6 ring slots.
	var occupants: Array = [pos]
	for i in range(1, 7):
		var angle: float = float(i) * TAU / 6.0
		occupants.append(pos + Vector2(cos(angle), sin(angle)) * SLOT_DISTANCE)

	var seed_a: Vector2 = AgentControllerScript._pick_free_slot(pos, occupants, 1)
	var seed_b: Vector2 = AgentControllerScript._pick_free_slot(pos, occupants, 2)

	# Different seeds → different fallback slots.
	assert_that(seed_a.distance_to(seed_b)).is_greater(TILE_SIZE)


func test_static_helper_is_deterministic() -> void:
	## Same inputs → same output. Important because game-server state is
	## broadcast and any nondeterminism here would show up as agent jitter.
	var pos := Vector2(500, 500)
	var occupants: Array = [pos, pos + Vector2(20, 5)]
	var first: Vector2 = AgentControllerScript._pick_free_slot(pos, occupants, 7)
	var second: Vector2 = AgentControllerScript._pick_free_slot(pos, occupants, 7)
	assert_that(first).is_equal(second)
