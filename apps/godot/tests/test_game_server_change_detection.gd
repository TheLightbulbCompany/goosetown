## Tests for GameServer.viewer_state_changed — the skip-when-unchanged gate
## that keeps the 10Hz viewer broadcast load-bounded to actual activity.
##
## Key note: the actual viewer agent dict uses "id" (not "agent_id") and
## "speechText" (not "speech") — confirmed from agent_controller.gd:532-546.
extends GdUnitTestSuite

const GameServer := preload("res://scripts/server/game_server.gd")


func _agent(id: String, x: float, y: float, facing := "south", activity := "idle", speech_text := "") -> Dictionary:
	return {"id": id, "x": x, "y": y, "facing": facing, "activity": activity, "speechText": speech_text}


func test_identical_state_is_unchanged() -> void:
	var prev := [_agent("1", 10.0, 20.0)]
	var curr := [_agent("1", 10.0, 20.0)]
	assert_that(GameServer.viewer_state_changed(prev, curr, 1.0)).is_false()


func test_subpixel_move_is_unchanged() -> void:
	var prev := [_agent("1", 10.0, 20.0)]
	var curr := [_agent("1", 10.5, 20.0)]
	assert_that(GameServer.viewer_state_changed(prev, curr, 1.0)).is_false()


func test_move_beyond_epsilon_is_changed() -> void:
	var prev := [_agent("1", 10.0, 20.0)]
	var curr := [_agent("1", 12.0, 20.0)]
	assert_that(GameServer.viewer_state_changed(prev, curr, 1.0)).is_true()


func test_facing_change_is_changed() -> void:
	var prev := [_agent("1", 10.0, 20.0, "south")]
	var curr := [_agent("1", 10.0, 20.0, "north")]
	assert_that(GameServer.viewer_state_changed(prev, curr, 1.0)).is_true()


func test_speech_change_is_changed() -> void:
	var prev := [_agent("1", 10.0, 20.0, "south", "idle", "")]
	var curr := [_agent("1", 10.0, 20.0, "south", "idle", "hi")]
	assert_that(GameServer.viewer_state_changed(prev, curr, 1.0)).is_true()


func test_roster_add_is_changed() -> void:
	var prev := [_agent("1", 10.0, 20.0)]
	var curr := [_agent("1", 10.0, 20.0), _agent("2", 5.0, 5.0)]
	assert_that(GameServer.viewer_state_changed(prev, curr, 1.0)).is_true()


func test_roster_swap_same_size_is_changed() -> void:
	var prev := [_agent("1", 10.0, 20.0)]
	var curr := [_agent("2", 10.0, 20.0)]
	assert_that(GameServer.viewer_state_changed(prev, curr, 1.0)).is_true()
