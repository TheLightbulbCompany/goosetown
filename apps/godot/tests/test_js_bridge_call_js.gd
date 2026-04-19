## Tests for ``JsBridge.call_js`` secure helper.
##
## The whole point of the helper is that user-controlled data never appears
## in the string passed to JavaScriptBridge.eval. These tests verify that
## invariant against the pure script-builder; the actual eval side-effect is
## covered by the Playwright smoke extension (Track E).
extends GdUnitTestSuite

const JsBridgeScript := preload("res://scripts/autoloads/js_bridge.gd")


func _make_bridge() -> Node:
	var bridge: Node = JsBridgeScript.new()
	add_child(auto_free(bridge))
	return bridge


func test_build_eval_script_contains_only_fn_name_and_key() -> void:
	var bridge := _make_bridge()
	var script: String = bridge._build_eval_script("load_sprite", "__gt_payload_123_456")

	# The fn_name and key must appear; nothing else user-controlled should.
	assert_that(script).contains("__gt_load_sprite")
	assert_that(script).contains("__gt_payload_123_456")


func test_build_eval_script_does_not_contain_payload_values() -> void:
	var bridge := _make_bridge()
	var script: String = bridge._build_eval_script("load_sprite", "__gt_payload_1")

	# The builder takes no payload — that goes through window.set() — so even
	# with a malicious key attempt it cannot leak into the eval string.
	assert_that(script).not_contains("alert")
	assert_that(script).not_contains("'; ")
	assert_that(script).not_contains("<script")


func test_json_stringify_escapes_single_quotes_in_payload() -> void:
	# Confirm the Godot built-in JSON handles the hostile strings we worry
	# about. If this ever regresses we need a different escape.
	var hostile := {"url": "https://evil.example/'); alert(1);//"}
	var serialized := JSON.stringify(hostile)
	var round_trip: Variant = JSON.parse_string(serialized)
	assert_that(round_trip is Dictionary).is_true()
	assert_that((round_trip as Dictionary).get("url")).is_equal(hostile["url"])


func test_build_eval_script_rejects_hostile_key() -> void:
	var bridge := _make_bridge()
	# A hostile key with quote / bracket injection attempt
	var result: String = bridge._build_eval_script("load_sprite", "']); alert(1); window['x")
	assert_that(result).is_equal("")


func test_build_eval_script_rejects_non_pattern_key() -> void:
	var bridge := _make_bridge()
	# Keys must match __gt_payload_<int>_<int>; this does not
	var result: String = bridge._build_eval_script("load_sprite", "random_other_key")
	assert_that(result).is_equal("")
