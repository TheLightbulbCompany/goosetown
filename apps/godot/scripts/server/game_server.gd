## Main orchestrator for the headless GooseTown game server.
##
## Loads the town map and apartment manager, wires up ServerProtocol,
## routes messages between peers and the correct controller (town or apartment),
## handles transitions between contexts, and broadcasts combined world state
## to viewer peers on a timer.
extends Node

const PORT: int = 8080
const VIEWER_STATE_INTERVAL: float = 2.0

var _protocol: ServerProtocol = null
var _controller: AgentController = null
var _apartment_manager: ApartmentManager = null
var _library_manager: LibraryManager = null
var _viewer_peers: Array[int] = []
var _viewer_timer: float = 0.0


func _ready() -> void:
	Engine.physics_ticks_per_second = 20

	# Load and instantiate the town map.
	var map_scene: PackedScene = load("res://maps/isol88/Isol88Map.tscn")
	var map_node: Node = map_scene.instantiate()
	add_child(map_node)

	# Collect Marker2D location nodes from the map's Markers child.
	var locations: Array[Marker2D] = []
	var markers_node: Node = map_node.get_node("Markers")
	if markers_node:
		for child in markers_node.get_children():
			if child is Marker2D:
				locations.append(child as Marker2D)
	else:
		push_warning("GameServer: map has no 'Markers' child -- no locations available")

	print("GameServer: loaded map with %d locations" % locations.size())

	# Create and initialize the town agent controller.
	_controller = AgentController.new()
	_controller.name = "AgentController"
	_controller.initialize(locations)
	_controller.send_to_peer.connect(_on_send_to_peer)
	add_child(_controller)

	# Create the apartment manager (per-user apartment instances).
	_apartment_manager = ApartmentManager.new()
	_apartment_manager.name = "ApartmentManager"
	_apartment_manager.send_to_peer.connect(_on_send_to_peer)
	add_child(_apartment_manager)

	print("GameServer: apartment manager ready")

	# Create the library manager (one shared SubViewport for all users).
	_library_manager = LibraryManager.new()
	_library_manager.name = "LibraryManager"
	_library_manager.send_to_peer.connect(_on_send_to_peer)
	add_child(_library_manager)

	print("GameServer: library manager ready")

	# Create and start the server protocol.
	_protocol = ServerProtocol.new()
	_protocol.name = "ServerProtocol"
	_protocol.client_connected.connect(_on_client_connected)
	_protocol.client_disconnected.connect(_on_client_disconnected)
	_protocol.command_received.connect(_on_command_received)
	add_child(_protocol)

	var err := _protocol.start(PORT)
	if err != OK:
		push_error("GameServer: failed to start server on port %d" % PORT)
		return

	print("GameServer: ready -- listening on port %d" % PORT)


func _process(delta: float) -> void:
	if _viewer_peers.is_empty():
		return

	_viewer_timer += delta
	if _viewer_timer >= VIEWER_STATE_INTERVAL:
		_viewer_timer -= VIEWER_STATE_INTERVAL
		_broadcast_world_state()


# ---------------------------------------------------------------------------
# World state broadcast
# ---------------------------------------------------------------------------

func _broadcast_world_state() -> void:
	# Combine town, apartment, and library agents into one world_state message.
	var town_state: Dictionary = _controller.get_world_state_for_viewer()
	var town_agents: Array = town_state.get("agents", [])
	var apartment_agents: Array = _apartment_manager.get_all_agent_states()
	var library_agents: Array = _library_manager.get_all_agent_states()

	var all_agents: Array = []
	all_agents.append_array(town_agents)
	all_agents.append_array(apartment_agents)
	all_agents.append_array(library_agents)

	var data: Dictionary = {
		"event": "world_state",
		"agents": all_agents,
	}
	for peer_id in _viewer_peers:
		_protocol.send_to(peer_id, data)


# ---------------------------------------------------------------------------
# Signal handlers
# ---------------------------------------------------------------------------

func _on_client_connected(peer_id: int, data: Dictionary) -> void:
	var client_type: String = str(data.get("type", ""))

	if client_type == "viewer":
		_viewer_peers.append(peer_id)
		print("GameServer: viewer connected (peer %d)" % peer_id)

	elif client_type == "agent":
		var context: String = str(data.get("context", "town"))
		var user_id: String = str(data.get("user_id", ""))

		if context == "apartment":
			if user_id == "":
				push_warning("GameServer: peer %d requested apartment but no user_id" % peer_id)
				_protocol.send_to(peer_id, {
					"event": "error",
					"error": "user_id required for apartment context",
				})
				return
			_apartment_manager.spawn_agent(user_id, peer_id, data)
			_apartment_manager.handle_command(peer_id, {"cmd": "get_locations"})
			print("GameServer: agent connected to apartment (peer %d, user %s)" % [
				peer_id, user_id.substr(0, 8),
			])
		elif context == "library":
			# Library is shared — user_id is stored per-agent for viewer
			# attribution but not required for spawn.
			_library_manager.spawn_agent(peer_id, data)
			_library_manager.handle_command(peer_id, {"cmd": "get_locations"})
			print("GameServer: agent connected to library (peer %d, user %s)" % [
				peer_id, user_id.substr(0, 8) if user_id != "" else "<anon>",
			])
		else:
			# Default: town context
			_controller.spawn_agent(peer_id, data)
			_controller.set_user_id(peer_id, user_id)
			_controller.handle_command(peer_id, {"cmd": "get_locations"})
			print("GameServer: agent connected to town (peer %d)" % peer_id)

	else:
		push_warning("GameServer: peer %d connected with unknown type '%s'" % [peer_id, client_type])


func _on_client_disconnected(peer_id: int) -> void:
	var idx: int = _viewer_peers.find(peer_id)
	if idx >= 0:
		_viewer_peers.remove_at(idx)
		print("GameServer: viewer disconnected (peer %d)" % peer_id)
	elif _apartment_manager.has_peer(peer_id):
		_apartment_manager.remove_agent(peer_id)
		print("GameServer: apartment agent disconnected (peer %d)" % peer_id)
	elif _library_manager.has_peer(peer_id):
		_library_manager.remove_agent(peer_id)
		print("GameServer: library agent disconnected (peer %d)" % peer_id)
	elif _controller.has_peer(peer_id):
		_controller.remove_agent(peer_id)
		print("GameServer: town agent disconnected (peer %d)" % peer_id)
	else:
		push_warning("GameServer: peer %d disconnected but not found in any controller (orphaned peer)" % peer_id)


func _on_command_received(peer_id: int, data: Dictionary) -> void:
	var cmd: String = str(data.get("cmd", ""))

	# Handle context transitions
	if cmd == "transition":
		_handle_transition(peer_id, data)
		return

	# Route to the controller that owns this peer
	if _apartment_manager.has_peer(peer_id):
		_apartment_manager.handle_command(peer_id, data)
	elif _library_manager.has_peer(peer_id):
		_library_manager.handle_command(peer_id, data)
	else:
		_controller.handle_command(peer_id, data)


# ---------------------------------------------------------------------------
# Context transitions
# ---------------------------------------------------------------------------

func _handle_transition(peer_id: int, data: Dictionary) -> void:
	var target: String = str(data.get("to", ""))

	if target == "apartment":
		_transition_to_apartment(peer_id, data)
	elif target == "library":
		_transition_to_library(peer_id, data)
	elif target == "town":
		# Source could be apartment or library — dispatch on current location.
		if _apartment_manager.has_peer(peer_id):
			_transition_to_town(peer_id, data)
		elif _library_manager.has_peer(peer_id):
			_transition_library_to_town(peer_id, data)
		else:
			_protocol.send_to(peer_id, {
				"event": "command_error",
				"error": "Not in any interior context",
				"cmd": "transition",
			})
	else:
		_protocol.send_to(peer_id, {
			"event": "command_error",
			"error": "Unknown transition target: %s" % target,
			"cmd": "transition",
		})


func _transition_to_apartment(peer_id: int, data: Dictionary) -> void:
	# Must be in town currently
	if _apartment_manager.has_peer(peer_id):
		_protocol.send_to(peer_id, {
			"event": "command_error",
			"error": "Already in apartment",
			"cmd": "transition",
		})
		return

	var user_id: String = str(data.get("user_id", ""))
	if user_id == "":
		# Try the town controller's stored user_id
		user_id = _controller.get_user_id(peer_id)
	if user_id == "":
		_protocol.send_to(peer_id, {
			"event": "command_error",
			"error": "user_id required for apartment transition",
			"cmd": "transition",
		})
		return

	# Capture agent data from town before removing
	var agent_data: Dictionary = _controller.get_agent_data(peer_id)
	if agent_data.is_empty():
		_protocol.send_to(peer_id, {
			"event": "command_error",
			"error": "No agent found in town for this peer",
			"cmd": "transition",
		})
		return
	agent_data["user_id"] = user_id

	# Remove from town
	_controller.remove_agent(peer_id)

	# Spawn in apartment at the exit spot (entry point)
	agent_data["location"] = "exit"
	_apartment_manager.spawn_agent(user_id, peer_id, agent_data)
	_apartment_manager.handle_command(peer_id, {"cmd": "get_locations"})

	_protocol.send_to(peer_id, {
		"event": "transitioned",
		"from": "town",
		"to": "apartment",
		"location": "exit",
	})
	print("GameServer: peer %d transitioned town -> apartment" % peer_id)


func _transition_to_town(peer_id: int, data: Dictionary) -> void:
	# Must be in apartment currently
	if not _apartment_manager.has_peer(peer_id):
		_protocol.send_to(peer_id, {
			"event": "command_error",
			"error": "Not in apartment",
			"cmd": "transition",
		})
		return

	# Capture agent data from apartment before removing
	var apt_controller: ApartmentAgentController = _apartment_manager.get_controller_for_peer(peer_id)
	if apt_controller == null:
		_protocol.send_to(peer_id, {
			"event": "command_error",
			"error": "No agent found in apartment for this peer",
			"cmd": "transition",
		})
		return

	var agent_data: Dictionary = apt_controller.get_agent_data(peer_id)
	if agent_data.is_empty():
		_protocol.send_to(peer_id, {
			"event": "command_error",
			"error": "No agent found in apartment for this peer",
			"cmd": "transition",
		})
		return

	var user_id: String = apt_controller.user_id

	# Remove from apartment
	_apartment_manager.remove_agent(peer_id)

	# Spawn in town at the residence location
	agent_data["location"] = "residence"
	print("GameServer: transition to town — spawning with location=%s, controller has %d locations" % [
		agent_data.get("location", "?"), _controller._locations.size()
	])
	_controller.spawn_agent(peer_id, agent_data)
	_controller.set_user_id(peer_id, user_id)
	_controller.handle_command(peer_id, {"cmd": "get_locations"})

	_protocol.send_to(peer_id, {
		"event": "transitioned",
		"from": "apartment",
		"to": "town",
		"location": "residence",
	})
	print("GameServer: peer %d transitioned apartment -> town" % peer_id)


func _transition_to_library(peer_id: int, data: Dictionary) -> void:
	# Must be in town currently
	if _library_manager.has_peer(peer_id):
		_protocol.send_to(peer_id, {
			"event": "command_error",
			"error": "Already in library",
			"cmd": "transition",
		})
		return

	# Capture agent data from town before removing
	var agent_data: Dictionary = _controller.get_agent_data(peer_id)
	if agent_data.is_empty():
		_protocol.send_to(peer_id, {
			"event": "command_error",
			"error": "No agent found in town for this peer",
			"cmd": "transition",
		})
		return

	var user_id: String = str(data.get("user_id", ""))
	if user_id == "":
		user_id = _controller.get_user_id(peer_id)
	# user_id is optional for library (shared interior); store for attribution.
	agent_data["user_id"] = user_id

	# Remove from town
	_controller.remove_agent(peer_id)

	# Spawn in library at the spawn marker (entry point)
	agent_data["location"] = "spawn"
	_library_manager.spawn_agent(peer_id, agent_data)
	_library_manager.handle_command(peer_id, {"cmd": "get_locations"})

	_protocol.send_to(peer_id, {
		"event": "transitioned",
		"from": "town",
		"to": "library",
		"location": "spawn",
	})
	print("GameServer: peer %d transitioned town -> library" % peer_id)


func _transition_library_to_town(peer_id: int, data: Dictionary) -> void:
	# Must be in library currently
	if not _library_manager.has_peer(peer_id):
		_protocol.send_to(peer_id, {
			"event": "command_error",
			"error": "Not in library",
			"cmd": "transition",
		})
		return

	var lib_controller: LibraryAgentController = _library_manager.get_controller()
	if lib_controller == null:
		_protocol.send_to(peer_id, {
			"event": "command_error",
			"error": "Library not initialized",
			"cmd": "transition",
		})
		return

	var agent_data: Dictionary = lib_controller.get_agent_data(peer_id)
	if agent_data.is_empty():
		_protocol.send_to(peer_id, {
			"event": "command_error",
			"error": "No agent found in library for this peer",
			"cmd": "transition",
		})
		return

	var user_id: String = str(agent_data.get("user_id", ""))

	# Remove from library
	_library_manager.remove_agent(peer_id)

	# Spawn back in town at the library marker (entry/exit point on town map)
	agent_data["location"] = "library"
	_controller.spawn_agent(peer_id, agent_data)
	_controller.set_user_id(peer_id, user_id)
	_controller.handle_command(peer_id, {"cmd": "get_locations"})

	_protocol.send_to(peer_id, {
		"event": "transitioned",
		"from": "library",
		"to": "town",
		"location": "library",
	})
	print("GameServer: peer %d transitioned library -> town" % peer_id)


func _on_send_to_peer(peer_id: int, data: Dictionary) -> void:
	_protocol.send_to(peer_id, data)
