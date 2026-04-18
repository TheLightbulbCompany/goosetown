## Tests for ``ServerProtocol._handle_message`` routing.
##
## The protocol has two code paths:
##   1. First message from a peer must contain a ``type`` field
##      → emit ``client_connected`` and mark peer identified.
##   2. Subsequent messages must contain a ``cmd`` field
##      → emit ``command_received``.
##
## Invalid JSON, non-dictionary JSON, missing ``type`` on first message, and
## missing ``cmd`` on subsequent messages must NOT emit either signal — a
## regression there would silently drop agent commands.
##
## Tested directly without opening a TCP socket; ``_handle_message`` only
## touches the internal ``_identified`` map and the signal bus.
##
## Signals are captured via a lambda connected to each signal (native Godot
## pattern) rather than gdunit4's ``monitor_signals`` — the latter times out
## unreliably when signals fire synchronously inside the same call stack.
extends GdUnitTestSuite

const ServerProtocolScript := preload("res://scripts/server/server_protocol.gd")


## Wire a ServerProtocol with signal captures and return (proto, arrays).
func _make_protocol_with_captures() -> Array:
	var proto: ServerProtocol = ServerProtocolScript.new()
	add_child(auto_free(proto))
	var connected_calls: Array = []
	var command_calls: Array = []
	proto.client_connected.connect(
		func(pid: int, data: Dictionary) -> void:
			connected_calls.append([pid, data])
	)
	proto.command_received.connect(
		func(pid: int, data: Dictionary) -> void:
			command_calls.append([pid, data])
	)
	return [proto, connected_calls, command_calls]


# ---------------------------------------------------------------------------
# First-message path (client_connected emission)
# ---------------------------------------------------------------------------


func test_first_message_with_type_emits_client_connected() -> void:
	var r := _make_protocol_with_captures()
	var proto: ServerProtocol = r[0]
	var connected: Array = r[1]

	proto._handle_message(42, '{"type":"viewer"}')

	assert_that(connected.size()).is_equal(1)
	assert_that(connected[0][0]).is_equal(42)
	assert_that((connected[0][1] as Dictionary).get("type")).is_equal("viewer")


func test_first_message_marks_peer_identified() -> void:
	var r := _make_protocol_with_captures()
	var proto: ServerProtocol = r[0]

	proto._handle_message(42, '{"type":"agent"}')

	assert_that(proto._identified[42]).is_true()
	assert_that(proto._peer_type[42]).is_equal("agent")


func test_first_message_without_type_does_not_identify() -> void:
	var r := _make_protocol_with_captures()
	var proto: ServerProtocol = r[0]
	var connected: Array = r[1]
	var commands: Array = r[2]

	# Missing "type" on an unidentified peer — must not emit client_connected.
	proto._handle_message(42, '{"cmd":"hello"}')

	assert_that(connected.size()).is_equal(0)
	assert_that(commands.size()).is_equal(0)
	assert_that(proto._identified.get(42, false)).is_false()


# ---------------------------------------------------------------------------
# Subsequent-message path (command_received emission)
# ---------------------------------------------------------------------------


func test_subsequent_message_with_cmd_emits_command_received() -> void:
	var r := _make_protocol_with_captures()
	var proto: ServerProtocol = r[0]
	var commands: Array = r[2]

	proto._handle_message(42, '{"type":"viewer"}')
	proto._handle_message(42, '{"cmd":"move","target":"plaza"}')

	assert_that(commands.size()).is_equal(1)
	assert_that(commands[0][0]).is_equal(42)
	assert_that((commands[0][1] as Dictionary).get("cmd")).is_equal("move")


func test_subsequent_message_missing_cmd_does_not_emit() -> void:
	var r := _make_protocol_with_captures()
	var proto: ServerProtocol = r[0]
	var commands: Array = r[2]

	proto._handle_message(42, '{"type":"viewer"}')
	proto._handle_message(42, '{"not_a_cmd":"oops"}')

	assert_that(commands.size()).is_equal(0)


# ---------------------------------------------------------------------------
# Malformed input — neither signal should fire.
# ---------------------------------------------------------------------------


func test_invalid_json_emits_nothing() -> void:
	var r := _make_protocol_with_captures()
	var proto: ServerProtocol = r[0]
	var connected: Array = r[1]
	var commands: Array = r[2]

	proto._handle_message(42, "not-json-at-all")

	assert_that(connected.size()).is_equal(0)
	assert_that(commands.size()).is_equal(0)


func test_non_dictionary_json_emits_nothing() -> void:
	var r := _make_protocol_with_captures()
	var proto: ServerProtocol = r[0]
	var connected: Array = r[1]
	var commands: Array = r[2]

	proto._handle_message(42, "[1,2,3]")

	assert_that(connected.size()).is_equal(0)
	assert_that(commands.size()).is_equal(0)
