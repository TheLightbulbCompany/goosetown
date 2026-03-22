## WebSocket client for the browser Godot viewer.
## Connects to the GooseTown backend and receives world_state events
## for rendering agents in the WebGL export.
extends Node
class_name ViewerProtocol

signal world_state_received(data: Dictionary)
signal connected()
signal disconnected()

var _ws: WebSocketPeer = WebSocketPeer.new()
var _url: String = ""
var _connected: bool = false
var _reconnect_timer: float = 0.0
var _should_reconnect: bool = false


func connect_to_server(url: String) -> void:
	_url = url
	_should_reconnect = true
	var err = _ws.connect_to_url(url)
	if err != OK:
		push_error("ViewerProtocol: failed to connect: %s" % error_string(err))


func disconnect_from_server() -> void:
	_should_reconnect = false
	_ws.close()


func _process(delta: float) -> void:
	_ws.poll()
	var state = _ws.get_ready_state()

	if state == WebSocketPeer.STATE_OPEN:
		if not _connected:
			_connected = true
			# Send viewer identification
			_ws.send_text(JSON.stringify({"type": "viewer"}))
			connected.emit()
			print("[ViewerProtocol] Connected to server")

		# Read incoming messages
		while _ws.get_available_packet_count() > 0:
			var text = _ws.get_packet().get_string_from_utf8()
			var json = JSON.new()
			if json.parse(text) == OK:
				var data = json.get_data()
				if data is Dictionary and data.get("event") == "world_state":
					world_state_received.emit(data)

	elif state == WebSocketPeer.STATE_CLOSED and _connected:
		_connected = false
		disconnected.emit()
		print("[ViewerProtocol] Disconnected")
		if _should_reconnect:
			_reconnect_timer = 2.0

	# Reconnection
	if not _connected and _should_reconnect and _reconnect_timer > 0:
		_reconnect_timer -= delta
		if _reconnect_timer <= 0:
			print("[ViewerProtocol] Reconnecting...")
			_ws.connect_to_url(_url)
