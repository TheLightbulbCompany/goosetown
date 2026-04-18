## Tests for pure location-lookup helpers in ``AgentController``.
##
## Covers ``_find_location`` and ``_get_agent_location`` — the two small
## functions called on every frame during ``_check_arrivals`` and every
## world_state broadcast.  A wrong location string silently misreports
## agents to the daemon, so these deserve regression coverage even though
## the rest of ``AgentController`` (scene instantiation, navigation) is
## harder to test headless.
extends GdUnitTestSuite

const AgentControllerScript := preload("res://scripts/server/agent_controller.gd")


## Build a fresh AgentController wired to a set of named Marker2D positions.
func _make_controller(markers: Array[Marker2D]) -> AgentController:
	var controller: AgentController = AgentControllerScript.new()
	add_child(auto_free(controller))
	controller.initialize(markers)
	return controller


func _make_marker(name_str: String, x: float, y: float) -> Marker2D:
	var m := Marker2D.new()
	m.name = name_str
	m.global_position = Vector2(x, y)
	add_child(auto_free(m))
	return m


# ---------------------------------------------------------------------------
# _find_location
# ---------------------------------------------------------------------------


func test_find_location_returns_matching_marker() -> void:
	var plaza := _make_marker("Plaza", 100, 100)
	var library := _make_marker("Library", 400, 400)
	var controller := _make_controller([plaza, library])

	assert_that(controller._find_location("plaza")).is_same(plaza)
	assert_that(controller._find_location("library")).is_same(library)


func test_find_location_normalises_to_snake_case() -> void:
	var marker := _make_marker("ActivityCenter", 0, 0)
	var controller := _make_controller([marker])

	# CamelCase name matches a lowercase snake_case lookup.
	assert_that(controller._find_location("activity_center")).is_same(marker)


func test_find_location_returns_null_when_unknown() -> void:
	var plaza := _make_marker("Plaza", 0, 0)
	var controller := _make_controller([plaza])

	assert_that(controller._find_location("nonexistent")).is_null()


# ---------------------------------------------------------------------------
# _get_agent_location — returns nearest marker name by distance.
# ---------------------------------------------------------------------------


func test_get_agent_location_returns_nearest_marker_name() -> void:
	var plaza := _make_marker("Plaza", 0, 0)
	var library := _make_marker("Library", 1000, 1000)
	var controller := _make_controller([plaza, library])

	# Position near plaza
	assert_that(controller._get_agent_location(Vector2(50, 50))).is_equal("plaza")
	# Position near library
	assert_that(controller._get_agent_location(Vector2(900, 900))).is_equal("library")


func test_get_agent_location_returns_snake_case_of_camel_name() -> void:
	var act_center := _make_marker("ActivityCenter", 100, 100)
	var controller := _make_controller([act_center])

	assert_that(controller._get_agent_location(Vector2(100, 100))).is_equal("activity_center")


func test_get_agent_location_returns_unknown_with_no_markers() -> void:
	var controller := _make_controller([] as Array[Marker2D])

	# Falls through the distance loop with no update → sentinel "unknown"
	assert_that(controller._get_agent_location(Vector2(0, 0))).is_equal("unknown")
