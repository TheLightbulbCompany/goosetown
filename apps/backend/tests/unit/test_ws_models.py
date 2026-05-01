"""Tests for core.ws_models — Pydantic discriminated union validating
incoming WebSocket message bodies forwarded from API Gateway.

The wire format is flat: {type, action, ...action-fields}. These tests
verify validation catches every way a message can be malformed.
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from core.ws_models import WsMessage

_adapter: TypeAdapter[WsMessage] = TypeAdapter(WsMessage)


class TestTownAgentConnect:
    def test_accepts_minimal_valid_payload(self) -> None:
        msg = _adapter.validate_python({"type": "town_agent_connect", "token": "abc"})  # nosec B105: test token literal, not a credential
        assert msg.type == "town_agent_connect"
        assert msg.token == "abc"  # nosec B105: test assertion on token field value, not a secret

    def test_accepts_with_agent_name(self) -> None:
        msg = _adapter.validate_python({"type": "town_agent_connect", "token": "abc", "agent_name": "Goose"})  # nosec B105: test token literal, not a credential
        assert msg.agent_name == "Goose"

    def test_rejects_missing_token(self) -> None:
        with pytest.raises(ValidationError):
            _adapter.validate_python({"type": "town_agent_connect"})


class TestTownAgentAct:
    def test_accepts_move_action(self) -> None:
        msg = _adapter.validate_python({"type": "town_agent_act", "action": "move", "destination": "plaza"})
        assert msg.action == "move"
        assert msg.destination == "plaza"

    def test_rejects_move_without_destination(self) -> None:
        with pytest.raises(ValidationError):
            _adapter.validate_python({"type": "town_agent_act", "action": "move"})

    def test_accepts_chat_action_with_target(self) -> None:
        msg = _adapter.validate_python({"type": "town_agent_act", "action": "chat", "target": "agent_b"})
        assert msg.target == "agent_b"

    def test_accepts_chat_action_with_opening_message(self) -> None:
        msg = _adapter.validate_python(
            {
                "type": "town_agent_act",
                "action": "chat",
                "target": "agent_b",
                "message": "Hello!",
            }
        )
        assert msg.message == "Hello!"

    def test_rejects_chat_without_target(self) -> None:
        with pytest.raises(ValidationError):
            _adapter.validate_python({"type": "town_agent_act", "action": "chat"})

    def test_accepts_say_action(self) -> None:
        msg = _adapter.validate_python(
            {
                "type": "town_agent_act",
                "action": "say",
                "conv_id": "c1",
                "message": "hi",
            }
        )
        assert msg.conv_id == "c1"
        assert msg.message == "hi"

    def test_accepts_say_without_conv_id(self) -> None:
        """Handler tolerates empty/missing conv_id (defaults to ''). Schema
        matches that behavior rather than rejecting the message here."""
        msg = _adapter.validate_python({"type": "town_agent_act", "action": "say", "message": "hi"})
        assert msg.conv_id is None
        assert msg.message == "hi"

    def test_rejects_say_without_message(self) -> None:
        with pytest.raises(ValidationError):
            _adapter.validate_python({"type": "town_agent_act", "action": "say", "conv_id": "c1"})

    def test_rejects_say_message_over_max_length(self) -> None:
        with pytest.raises(ValidationError):
            _adapter.validate_python(
                {
                    "type": "town_agent_act",
                    "action": "say",
                    "conv_id": "c1",
                    "message": "x" * 501,
                }
            )

    def test_accepts_idle_action(self) -> None:
        msg = _adapter.validate_python({"type": "town_agent_act", "action": "idle"})
        assert msg.action == "idle"

    def test_accepts_end_conversation_action(self) -> None:
        msg = _adapter.validate_python({"type": "town_agent_act", "action": "end_conversation", "conv_id": "c1"})
        assert msg.action == "end_conversation"
        assert msg.conv_id == "c1"

    def test_accepts_end_conversation_without_conv_id(self) -> None:
        """Like 'say', the handler tolerates missing conv_id (defaults to '').
        Schema matches: no model_validator enforcement."""
        msg = _adapter.validate_python({"type": "town_agent_act", "action": "end_conversation"})
        assert msg.action == "end_conversation"
        assert msg.conv_id is None

    def test_accepts_join_conversation_action(self) -> None:
        """F9 also added join_conversation — CLI uses it but the old literal
        silently 422'd it alongside end_conversation."""
        msg = _adapter.validate_python({"type": "town_agent_act", "action": "join_conversation", "conv_id": "c1"})
        assert msg.action == "join_conversation"
        assert msg.conv_id == "c1"

    def test_rejects_old_end_literal(self) -> None:
        """Regression guard: the bare 'end' literal was the F9 bug —
        CLI dispatchers match 'end_conversation' so 'end' must stay rejected."""
        with pytest.raises(ValidationError):
            _adapter.validate_python({"type": "town_agent_act", "action": "end", "conv_id": "c1"})

    def test_accepts_context_transition(self) -> None:
        msg = _adapter.validate_python(
            {
                "type": "town_agent_act",
                "action": "context_transition",
                "target_context": "apartment",
            }
        )
        assert msg.target_context == "apartment"

    def test_rejects_context_transition_with_invalid_target(self) -> None:
        with pytest.raises(ValidationError):
            _adapter.validate_python(
                {
                    "type": "town_agent_act",
                    "action": "context_transition",
                    "target_context": "moon",
                }
            )

    def test_accepts_activity_action(self) -> None:
        msg = _adapter.validate_python({"type": "town_agent_act", "action": "activity", "activity_id": "read"})
        assert msg.activity_id == "read"

    def test_rejects_unknown_action(self) -> None:
        with pytest.raises(ValidationError):
            _adapter.validate_python({"type": "town_agent_act", "action": "eat_the_pigeons"})


class TestTownDisconnect:
    def test_accepts_without_alarm(self) -> None:
        msg = _adapter.validate_python({"type": "town_disconnect"})
        assert msg.alarm is None

    def test_accepts_with_alarm(self) -> None:
        msg = _adapter.validate_python({"type": "town_disconnect", "alarm": "07:30"})
        assert msg.alarm == "07:30"


class TestTownSubscribe:
    def test_accepts_subscribe(self) -> None:
        msg = _adapter.validate_python({"type": "town_subscribe"})
        assert msg.type == "town_subscribe"


class TestTownAgentSleep:
    """Clean-leave signal must parse so the $disconnect Lambda can tell
    intentional sleep from a server-initiated drop (F12)."""

    def test_accepts_without_alarm(self) -> None:
        msg = _adapter.validate_python({"type": "town_agent_sleep"})
        assert msg.type == "town_agent_sleep"
        assert msg.wake_time is None

    def test_accepts_with_alarm(self) -> None:
        msg = _adapter.validate_python(
            {"type": "town_agent_sleep", "wake_time": "07:30", "timezone": "America/New_York"}
        )
        assert msg.wake_time == "07:30"
        assert msg.timezone == "America/New_York"


class TestTownAgentKeepalive:
    """Daemon keepalive pings must not 422 under strict validation (F12)."""

    def test_accepts_ping(self) -> None:
        msg = _adapter.validate_python({"type": "ping"})
        assert msg.type == "ping"


class TestTopLevelValidation:
    def test_rejects_unknown_type(self) -> None:
        with pytest.raises(ValidationError):
            _adapter.validate_python({"type": "town_eat_sandwich"})

    def test_rejects_missing_type(self) -> None:
        with pytest.raises(ValidationError):
            _adapter.validate_python({})

    def test_cve_regression_extra_keys_are_ignored(self) -> None:
        """CRITICAL regression: arbitrary extra keys must not surface in
        model_dump(). Pydantic's default extra='ignore' gives us this, but
        we assert it explicitly so a config change won't reintroduce the bug."""
        msg = _adapter.validate_python(
            {
                "type": "town_agent_act",
                "action": "idle",
                "malicious_extra": "drop_table",
            }
        )
        assert "malicious_extra" not in msg.model_dump()
