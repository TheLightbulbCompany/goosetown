## WebSocket server protocol using TCPServer + WebSocketPeer.
##
## Accepts incoming TCP connections, upgrades them to WebSocket,
## manages multiple concurrent peers, parses JSON text frames,
## and emits signals for connection lifecycle and commands.
class_name ServerProtocol
extends Node

## Emitted when a client sends its first message containing a "type" field.
signal client_connected(peer_id: int, data: Dictionary)

## Emitted when a peer's WebSocket connection closes.
signal client_disconnected(peer_id: int)

## Emitted when a client sends a message containing a "cmd" field.
signal command_received(peer_id: int, data: Dictionary)

var _tcp_server: TCPServer = null
var _peers: Dictionary = {}  # peer_id -> WebSocketPeer
var _pending_tcp: Dictionary = {}  # peer_id -> StreamPeerTCP (awaiting WS upgrade)
var _identified: Dictionary = {}  # peer_id -> bool (has sent first "type" message)
var _next_peer_id: int = 1


func start(port: int) -> Error:
	if _tcp_server != null and _tcp_server.is_listening():
		push_warning("ServerProtocol: already listening")
		return ERR_ALREADY_IN_USE

	_tcp_server = TCPServer.new()
	var err := _tcp_server.listen(port)
	if err != OK:
		push_error("ServerProtocol: failed to listen on port %d — %s" % [port, error_string(err)])
		_tcp_server = null
		return err

	print("ServerProtocol: listening on port %d" % port)
	return OK


func stop() -> void:
	# Close all WebSocket peers.
	for peer_id in _peers.keys():
		var ws: WebSocketPeer = _peers[peer_id]
		ws.close()
	_peers.clear()

	# Close pending TCP connections.
	for peer_id in _pending_tcp.keys():
		var tcp: StreamPeerTCP = _pending_tcp[peer_id]
		tcp.disconnect_from_host()
	_pending_tcp.clear()

	_identified.clear()
	_next_peer_id = 1

	if _tcp_server != null:
		_tcp_server.stop()
		_tcp_server = null

	print("ServerProtocol: stopped")


func send_to(peer_id: int, data: Dictionary) -> void:
	if not _peers.has(peer_id):
		push_warning("ServerProtocol: send_to unknown peer %d" % peer_id)
		return

	var ws: WebSocketPeer = _peers[peer_id]
	var json_str := JSON.stringify(data)
	ws.send_text(json_str)


func broadcast(data: Dictionary, exclude: int = -1) -> void:
	var json_str := JSON.stringify(data)
	for peer_id in _peers.keys():
		if peer_id == exclude:
			continue
		# Only broadcast to identified (connected) peers.
		if _identified.get(peer_id, false):
			var ws: WebSocketPeer = _peers[peer_id]
			ws.send_text(json_str)


func _process(_delta: float) -> void:
	if _tcp_server == null or not _tcp_server.is_listening():
		return

	# Accept new TCP connections.
	while _tcp_server.is_connection_available():
		var tcp: StreamPeerTCP = _tcp_server.take_connection()
		if tcp == null:
			break
		var peer_id := _next_peer_id
		_next_peer_id += 1
		_pending_tcp[peer_id] = tcp

	# Upgrade pending TCP connections to WebSocket.
	var upgraded_ids: Array = []
	for peer_id in _pending_tcp.keys():
		var tcp: StreamPeerTCP = _pending_tcp[peer_id]
		tcp.poll()
		if tcp.get_status() != StreamPeerTCP.STATUS_CONNECTED:
			continue

		var ws := WebSocketPeer.new()
		var err := ws.accept_stream(tcp)
		if err != OK:
			push_warning("ServerProtocol: WebSocket upgrade failed for peer %d — %s" % [peer_id, error_string(err)])
			tcp.disconnect_from_host()
			upgraded_ids.append(peer_id)
			continue

		_peers[peer_id] = ws
		_identified[peer_id] = false
		upgraded_ids.append(peer_id)

	for peer_id in upgraded_ids:
		_pending_tcp.erase(peer_id)

	# Poll active WebSocket peers.
	var disconnected_ids: Array = []
	for peer_id in _peers.keys():
		var ws: WebSocketPeer = _peers[peer_id]
		ws.poll()

		var state := ws.get_ready_state()

		if state == WebSocketPeer.STATE_CLOSED:
			disconnected_ids.append(peer_id)
			continue

		if state == WebSocketPeer.STATE_CLOSING:
			continue

		if state != WebSocketPeer.STATE_OPEN:
			continue

		# Read all available packets.
		while ws.get_available_packet_count() > 0:
			var packet := ws.get_packet()
			var text := packet.get_string_from_utf8()
			_handle_message(peer_id, text)

	# Emit disconnected signals and clean up.
	for peer_id in disconnected_ids:
		_peers.erase(peer_id)
		var was_identified: bool = _identified.get(peer_id, false)
		_identified.erase(peer_id)
		if was_identified:
			client_disconnected.emit(peer_id)


func _handle_message(peer_id: int, text: String) -> void:
	var json := JSON.new()
	var err := json.parse(text)
	if err != OK:
		push_warning("ServerProtocol: invalid JSON from peer %d — %s" % [peer_id, json.get_error_message()])
		return

	var data = json.get_data()
	if not data is Dictionary:
		push_warning("ServerProtocol: non-dictionary JSON from peer %d" % peer_id)
		return

	# First message must contain "type" → client_connected.
	if not _identified.get(peer_id, false):
		if data.has("type"):
			_identified[peer_id] = true
			client_connected.emit(peer_id, data)
		else:
			push_warning("ServerProtocol: first message from peer %d missing 'type' field" % peer_id)
		return

	# Subsequent messages with "cmd" → command_received.
	if data.has("cmd"):
		command_received.emit(peer_id, data)
	else:
		push_warning("ServerProtocol: message from peer %d missing 'cmd' field" % peer_id)
