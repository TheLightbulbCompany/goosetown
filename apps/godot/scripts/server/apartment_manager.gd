## Manages per-user apartment instances on the game server.
##
## Each user gets an isolated SubViewport with its own World2D,
## containing an ApartmentMap and ApartmentAgentController.
## Instances are created on demand and freed when empty.
class_name ApartmentManager
extends Node

const ApartmentScene: PackedScene = preload("res://maps/apartment/ApartmentMap.tscn")

signal send_to_peer(peer_id: int, data: Dictionary)

## user_id -> { viewport: SubViewport, controller: ApartmentAgentController }
var _instances: Dictionary = {}
## peer_id -> user_id (for routing commands)
var _peer_to_user: Dictionary = {}


func create_instance(owner_user_id: String) -> ApartmentAgentController:
	## Create or return existing apartment instance for a user.
	if _instances.has(owner_user_id):
		return _instances[owner_user_id].controller

	# Create isolated SubViewport with its own World2D
	var viewport := SubViewport.new()
	viewport.name = "Apartment_%s" % owner_user_id.substr(0, 8)
	viewport.world_2d = World2D.new()
	viewport.size = Vector2i(960, 540)
	viewport.render_target_update_mode = SubViewport.UPDATE_DISABLED  # Headless: physics only

	# IMPORTANT: Add the viewport to the tree BEFORE adding children to it.
	# NavigationRegion2D registers with a navigation map during _enter_tree.
	# If the viewport is orphaned (not in the SceneTree) when children enter,
	# NavigationRegion2D.get_world_2d() falls through to the root viewport's
	# default World2D instead of the SubViewport's custom World2D. This causes
	# the navmesh to register on the wrong map, leaving the SubViewport's
	# navigation map empty and breaking all NavigationAgent2D pathfinding.
	add_child(viewport)

	# Now instantiate the apartment map inside the (in-tree) viewport.
	# NavigationRegion2D will correctly find viewport.world_2d.navigation_map.
	var map_node: Node = ApartmentScene.instantiate()
	viewport.add_child(map_node)

	# Collect location markers from apartment map
	var locations: Array[Marker2D] = []
	var locations_node: Node = map_node.get_node_or_null("Locations")
	if locations_node:
		for child in locations_node.get_children():
			if child is Marker2D:
				locations.append(child as Marker2D)

	# Create controller inside the viewport (shares its World2D)
	var controller := ApartmentAgentController.new()
	controller.initialize(locations, owner_user_id)
	controller.send_to_peer.connect(_on_send_to_peer)
	viewport.add_child(controller)

	# Force navmesh sync so the first move command gets valid pathfinding.
	# The NavigationRegion2D is now registered on this map, so the update
	# will process the navmesh polygons and make them queryable immediately.
	var nav_map: RID = viewport.world_2d.navigation_map
	NavigationServer2D.map_force_update(nav_map)

	_instances[owner_user_id] = {
		"viewport": viewport,
		"controller": controller,
	}

	print("ApartmentManager: created instance for user %s (%d locations)" % [
		owner_user_id.substr(0, 8), locations.size(),
	])
	return controller


func get_controller(owner_user_id: String) -> ApartmentAgentController:
	if not _instances.has(owner_user_id):
		return null
	return _instances[owner_user_id].controller


func get_controller_for_peer(peer_id: int) -> ApartmentAgentController:
	var owner_user_id: String = _peer_to_user.get(peer_id, "")
	if owner_user_id == "":
		return null
	return get_controller(owner_user_id)


func spawn_agent(owner_user_id: String, peer_id: int, data: Dictionary) -> void:
	var controller: ApartmentAgentController = create_instance(owner_user_id)
	_peer_to_user[peer_id] = owner_user_id
	controller.spawn_agent(peer_id, data)


func remove_agent(peer_id: int) -> void:
	var owner_user_id: String = _peer_to_user.get(peer_id, "")
	if owner_user_id == "":
		return
	var controller: ApartmentAgentController = get_controller(owner_user_id)
	if controller:
		controller.remove_agent(peer_id)
		# Free instance if no agents remain
		if controller.is_empty():
			free_instance(owner_user_id)
	_peer_to_user.erase(peer_id)


func free_instance(owner_user_id: String) -> void:
	if not _instances.has(owner_user_id):
		return
	# Clean up all peer_to_user entries for this user
	var stale_peers: Array = []
	for peer_id in _peer_to_user.keys():
		if _peer_to_user[peer_id] == owner_user_id:
			stale_peers.append(peer_id)
	for peer_id in stale_peers:
		_peer_to_user.erase(peer_id)

	var instance: Dictionary = _instances[owner_user_id]
	var viewport: SubViewport = instance.viewport
	viewport.queue_free()
	_instances.erase(owner_user_id)
	print("ApartmentManager: freed instance for user %s" % owner_user_id.substr(0, 8))


func has_peer(peer_id: int) -> bool:
	return _peer_to_user.has(peer_id)


func handle_command(peer_id: int, data: Dictionary) -> void:
	var controller: ApartmentAgentController = get_controller_for_peer(peer_id)
	if controller:
		controller.handle_command(peer_id, data)


func get_all_agent_states() -> Array:
	## Collect agent states from all apartment instances for world_state.
	var all_agents: Array = []
	for owner_user_id in _instances.keys():
		var controller: ApartmentAgentController = _instances[owner_user_id].controller
		var state: Dictionary = controller.get_world_state_for_viewer()
		all_agents.append_array(state.get("agents", []))
	return all_agents


func _on_send_to_peer(peer_id: int, data: Dictionary) -> void:
	send_to_peer.emit(peer_id, data)
