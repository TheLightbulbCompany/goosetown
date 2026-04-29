## Manages the shared library SubViewport instance on the game server.
##
## Unlike ApartmentManager (one SubViewport per user), the library is a
## SHARED interior — there's a single SubViewport with one LibraryMap and
## one LibraryAgentController. All agents who enter the library spawn into
## this same instance and see each other.
##
## The viewport is created lazily on first entry so the navmesh registration
## happens in a frame where the SubViewport is already in the SceneTree.
class_name LibraryManager
extends Node

const LibraryScene: PackedScene = preload("res://maps/library/LibraryMap.tscn")

signal send_to_peer(peer_id: int, data: Dictionary)

var _viewport: SubViewport = null
var _controller: LibraryAgentController = null
var _peers: Array[int] = []  # peer_ids currently in the library


func _ensure_instance() -> void:
	## Create the singleton library instance the first time someone enters.
	if _controller != null:
		return

	_viewport = SubViewport.new()
	_viewport.name = "Library"
	_viewport.world_2d = World2D.new()
	_viewport.size = Vector2i(1536, 1024)
	_viewport.render_target_update_mode = SubViewport.UPDATE_DISABLED  # Headless

	# IMPORTANT: Add the viewport to the tree BEFORE adding children to it.
	# NavigationRegion2D registers with a navigation map during _enter_tree,
	# and an orphaned SubViewport falls back to the root viewport's map,
	# breaking pathfinding. Same gotcha as ApartmentManager — see comments
	# there for details.
	add_child(_viewport)

	var map_node: Node = LibraryScene.instantiate()
	_viewport.add_child(map_node)

	var locations: Array[Marker2D] = []
	var locations_node: Node = map_node.get_node_or_null("Locations")
	if locations_node:
		for child in locations_node.get_children():
			if child is Marker2D:
				locations.append(child as Marker2D)

	_controller = LibraryAgentController.new()
	_controller.initialize(locations)
	_controller.send_to_peer.connect(_on_send_to_peer)
	_viewport.add_child(_controller)

	# Force navmesh sync so the first move command after spawn gets valid
	# pathfinding. Without this, NavigationServer2D returns empty paths
	# until the next physics tick.
	var nav_map: RID = _viewport.world_2d.navigation_map
	NavigationServer2D.map_force_update(nav_map)

	print("LibraryManager: created shared library instance (%d locations)" % locations.size())


func get_controller() -> LibraryAgentController:
	return _controller


func spawn_agent(peer_id: int, data: Dictionary) -> void:
	_ensure_instance()
	if peer_id not in _peers:
		_peers.append(peer_id)
	_controller.spawn_agent(peer_id, data)


func remove_agent(peer_id: int) -> void:
	_peers.erase(peer_id)
	if _controller:
		_controller.remove_agent(peer_id)


func has_peer(peer_id: int) -> bool:
	return peer_id in _peers


func handle_command(peer_id: int, data: Dictionary) -> void:
	if _controller:
		_controller.handle_command(peer_id, data)


func get_all_agent_states() -> Array:
	## Return all agent states inside the library for world_state broadcast.
	if _controller == null:
		return []
	var state: Dictionary = _controller.get_world_state_for_viewer()
	return state.get("agents", [])


func _on_send_to_peer(peer_id: int, data: Dictionary) -> void:
	send_to_peer.emit(peer_id, data)
