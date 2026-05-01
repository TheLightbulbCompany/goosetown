## Tests for ``JsBridge.SCENE_REGISTRY`` and ``_on_switch_scene`` lookup.
##
## The registry is the GDScript twin of ``apps/frontend/src/scenes.ts``.
## Tests guard the contract — id presence, path resolution, filter flag —
## that the React side relies on when calling ``godotSwitchScene``.
extends GdUnitTestSuite

const JsBridgeScript := preload("res://scripts/autoloads/js_bridge.gd")


func test_scene_registry_contains_town_apartment_library() -> void:
	var registry: Dictionary = JsBridgeScript.SCENE_REGISTRY
	assert_that(registry.has("town")).is_true()
	assert_that(registry.has("apartment")).is_true()
	assert_that(registry.has("library")).is_true()


func test_scene_registry_paths_resolve_to_existing_resources() -> void:
	for scene_id in JsBridgeScript.SCENE_REGISTRY.keys():
		var path: String = JsBridgeScript.SCENE_REGISTRY[scene_id]["path"]
		assert_that(ResourceLoader.exists(path)).is_true()


func test_filter_by_owner_is_true_only_for_apartment() -> void:
	var registry: Dictionary = JsBridgeScript.SCENE_REGISTRY
	assert_that(registry["town"]["filter_by_owner"]).is_false()
	assert_that(registry["apartment"]["filter_by_owner"]).is_true()
	assert_that(registry["library"]["filter_by_owner"]).is_false()
