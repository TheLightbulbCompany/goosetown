## Tests for ``JsBridge._on_update_world_state``.
##
## Regression guard for the 2026-04-19 agent-invisible bug: since Apr 1
## the callback used ``call_deferred`` to emit ``world_state_received``,
## which never drained in the Godot 4 WebGL JavaScriptBridge context, so
## no agent ever spawned via world_state. The fix reverts to synchronous
## emit. These tests lock in that the signal fires in the same frame the
## callback runs, and that malformed input is rejected cleanly.
extends GdUnitTestSuite

const JsBridgeScript := preload("res://scripts/autoloads/js_bridge.gd")


func _make_bridge() -> Node:
	var bridge: Node = JsBridgeScript.new()
	add_child(auto_free(bridge))
	return bridge


func test_valid_json_dictionary_emits_world_state_received_synchronously() -> void:
	var bridge := _make_bridge()
	var received: Array = []
	bridge.world_state_received.connect(
		func(data: Dictionary) -> void: received.append(data)
	)

	bridge._on_update_world_state(['{"event":"world_state","agents":[]}'])

	# Synchronous emit: we should already have the payload before returning.
	assert_that(received.size()).is_equal(1)
	assert_that(received[0].get("event")).is_equal("world_state")


func test_valid_json_with_agents_array_preserves_structure() -> void:
	var bridge := _make_bridge()
	var received: Array = []
	bridge.world_state_received.connect(
		func(data: Dictionary) -> void: received.append(data)
	)

	var payload := '{"event":"world_state","_userId":"u1","agents":[{"id":"a1","name":"Monica","context":"town","x":100,"y":200}]}'
	bridge._on_update_world_state([payload])

	assert_that(received.size()).is_equal(1)
	assert_that(received[0].get("_userId")).is_equal("u1")
	var agents: Array = received[0].get("agents", [])
	assert_that(agents.size()).is_equal(1)
	assert_that((agents[0] as Dictionary).get("id")).is_equal("a1")


func test_invalid_json_does_not_emit() -> void:
	var bridge := _make_bridge()
	var received: Array = []
	bridge.world_state_received.connect(
		func(data: Dictionary) -> void: received.append(data)
	)

	bridge._on_update_world_state(['not-json-at-all'])

	assert_that(received.size()).is_equal(0)


func test_non_dictionary_json_does_not_emit() -> void:
	var bridge := _make_bridge()
	var received: Array = []
	bridge.world_state_received.connect(
		func(data: Dictionary) -> void: received.append(data)
	)

	# Array at top level, not an object — must be rejected.
	bridge._on_update_world_state(['[1, 2, 3]'])

	assert_that(received.size()).is_equal(0)
