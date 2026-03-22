## Main orchestrator for the headless GooseTown game server.
##
## Loads the town map, wires up ServerProtocol and AgentController,
## routes messages between peers and the controller, and broadcasts
## world state to viewer peers on a timer.
extends Node

const PORT: int = 8080
const VIEWER_STATE_INTERVAL: float = 2.0

var _protocol: ServerProtocol = null
var _controller: AgentController = null
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
		push_warning("GameServer: map has no 'Markers' child — no locations available")

	print("GameServer: loaded map with %d locations" % locations.size())

	# Create and initialize the agent controller.
	_controller = AgentController.new()
	_controller.name = "AgentController"
	_controller.initialize(locations)
	_controller.send_to_peer.connect(_on_send_to_peer)
	add_child(_controller)

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

	print("GameServer: ready — listening on port %d" % PORT)


func _process(delta: float) -> void:
	if _viewer_peers.is_empty():
		return

	_viewer_timer += delta
	if _viewer_timer >= VIEWER_STATE_INTERVAL:
		_viewer_timer -= VIEWER_STATE_INTERVAL
		var data: Dictionary = _controller.get_world_state_for_viewer()
		data["event"] = "world_state"
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
		_controller.spawn_agent(peer_id, data)
		# Send available locations to the newly connected agent.
		_controller.handle_command(peer_id, { "cmd": "get_locations" })
		print("GameServer: agent connected (peer %d)" % peer_id)
	else:
		push_warning("GameServer: peer %d connected with unknown type '%s'" % [peer_id, client_type])


func _on_client_disconnected(peer_id: int) -> void:
	var idx: int = _viewer_peers.find(peer_id)
	if idx >= 0:
		_viewer_peers.remove_at(idx)
		print("GameServer: viewer disconnected (peer %d)" % peer_id)
	else:
		_controller.remove_agent(peer_id)
		print("GameServer: agent disconnected (peer %d)" % peer_id)


func _on_command_received(peer_id: int, data: Dictionary) -> void:
	_controller.handle_command(peer_id, data)


func _on_send_to_peer(peer_id: int, data: Dictionary) -> void:
	_protocol.send_to(peer_id, data)
