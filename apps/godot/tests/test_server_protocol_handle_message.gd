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
extends GdUnitTestSuite

const ServerProtocolScript := preload("res://scripts/server/server_protocol.gd")


func _make_protocol() -> ServerProtocol:
	var proto: ServerProtocol = ServerProtocolScript.new()
	add_child(auto_free(proto))
	return proto


# ---------------------------------------------------------------------------
# First-message path (client_connected emission)
# ---------------------------------------------------------------------------


func test_first_message_with_type_emits_client_connected() -> void:
	var proto := monitor_signals(_make_protocol()) as ServerProtocol
	proto._handle_message(42, '{"type":"viewer"}')

	await assert_signal(proto).is_emitted("client_connected")


func test_first_message_marks_peer_identified() -> void:
	var proto := _make_protocol()
	proto._handle_message(42, '{"type":"agent"}')

	# Internal state: peer now identified with the declared type.
	assert_that(proto._identified[42]).is_true()
	assert_that(proto._peer_type[42]).is_equal("agent")


func test_first_message_without_type_does_not_identify() -> void:
	var proto := monitor_signals(_make_protocol()) as ServerProtocol
	proto._handle_message(42, '{"cmd":"hello"}')

	# No client_connected emitted — peer still not identified.
	await assert_signal(proto).is_not_emitted("client_connected")
	assert_that(proto._identified.get(42, false)).is_false()


# ---------------------------------------------------------------------------
# Subsequent-message path (command_received emission)
# ---------------------------------------------------------------------------


func test_subsequent_message_with_cmd_emits_command_received() -> void:
	var proto := monitor_signals(_make_protocol()) as ServerProtocol
	# Identify the peer first.
	proto._handle_message(42, '{"type":"viewer"}')
	# Then send a command.
	proto._handle_message(42, '{"cmd":"move","target":"plaza"}')

	await assert_signal(proto).is_emitted("command_received")


func test_subsequent_message_missing_cmd_does_not_emit() -> void:
	var proto := monitor_signals(_make_protocol()) as ServerProtocol
	proto._handle_message(42, '{"type":"viewer"}')
	proto._handle_message(42, '{"not_a_cmd":"oops"}')

	await assert_signal(proto).is_not_emitted("command_received")


# ---------------------------------------------------------------------------
# Malformed input — neither signal should fire.
# ---------------------------------------------------------------------------


func test_invalid_json_emits_nothing() -> void:
	var proto := monitor_signals(_make_protocol()) as ServerProtocol
	proto._handle_message(42, "not-json-at-all")

	await assert_signal(proto).is_not_emitted("client_connected")
	await assert_signal(proto).is_not_emitted("command_received")


func test_non_dictionary_json_emits_nothing() -> void:
	var proto := monitor_signals(_make_protocol()) as ServerProtocol
	proto._handle_message(42, "[1,2,3]")

	await assert_signal(proto).is_not_emitted("client_connected")
	await assert_signal(proto).is_not_emitted("command_received")
