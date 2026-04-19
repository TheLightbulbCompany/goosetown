"""Tests for core.ws_models — Pydantic discriminated union validating
incoming WebSocket messages forwarded from API Gateway."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from core.ws_models import WsMessage

_adapter: TypeAdapter[WsMessage] = TypeAdapter(WsMessage)


class TestTownAgentConnect:
    def test_accepts_minimal_valid_payload(self) -> None:
        msg = _adapter.validate_python(
            {
                "type": "town_agent_connect",
                "token": "abc",
                "connection_id": "c1",
            }
        )
        assert msg.type == "town_agent_connect"
        assert msg.token == "abc"

    def test_rejects_missing_token(self) -> None:
        with pytest.raises(ValidationError):
            _adapter.validate_python(
                {
                    "type": "town_agent_connect",
                    "connection_id": "c1",
                }
            )


class TestTownAgentAct:
    def test_accepts_move_action(self) -> None:
        msg = _adapter.validate_python(
            {
                "type": "town_agent_act",
                "connection_id": "c1",
                "body": {"action": "move", "target": "plaza"},
            }
        )
        assert msg.type == "town_agent_act"
        assert msg.body.action == "move"
        assert msg.body.target == "plaza"

    def test_accepts_say_action_with_text(self) -> None:
        msg = _adapter.validate_python(
            {
                "type": "town_agent_act",
                "connection_id": "c1",
                "body": {"action": "say", "text": "hello"},
            }
        )
        assert msg.body.action == "say"
        assert msg.body.text == "hello"

    def test_rejects_say_text_over_max_length(self) -> None:
        with pytest.raises(ValidationError):
            _adapter.validate_python(
                {
                    "type": "town_agent_act",
                    "connection_id": "c1",
                    "body": {"action": "say", "text": "x" * 501},
                }
            )

    def test_rejects_unknown_action(self) -> None:
        with pytest.raises(ValidationError):
            _adapter.validate_python(
                {
                    "type": "town_agent_act",
                    "connection_id": "c1",
                    "body": {"action": "eat_the_pigeons"},
                }
            )

    def test_rejects_move_without_target(self) -> None:
        with pytest.raises(ValidationError):
            _adapter.validate_python(
                {
                    "type": "town_agent_act",
                    "connection_id": "c1",
                    "body": {"action": "move"},
                }
            )


class TestTownDisconnect:
    def test_accepts_without_alarm(self) -> None:
        msg = _adapter.validate_python(
            {
                "type": "town_disconnect",
                "connection_id": "c1",
            }
        )
        assert msg.alarm is None

    def test_accepts_with_alarm_string(self) -> None:
        msg = _adapter.validate_python(
            {
                "type": "town_disconnect",
                "connection_id": "c1",
                "alarm": "07:30",
            }
        )
        assert msg.alarm == "07:30"


class TestTopLevelValidation:
    def test_rejects_unknown_type(self) -> None:
        with pytest.raises(ValidationError):
            _adapter.validate_python(
                {
                    "type": "town_eat_sandwich",
                    "connection_id": "c1",
                }
            )

    def test_rejects_missing_type(self) -> None:
        with pytest.raises(ValidationError):
            _adapter.validate_python({"connection_id": "c1"})

    def test_cve_regression_extra_keys_do_not_pass_through(self) -> None:
        """Regression guard for the 2026-04-19 CRITICAL finding: arbitrary
        extra keys in body must not be silently forwarded to the game server.
        Pydantic's default is to ignore extras, but we verify it explicitly
        here so a future config change doesn't reintroduce the bug."""
        msg = _adapter.validate_python(
            {
                "type": "town_agent_act",
                "connection_id": "c1",
                "body": {"action": "idle", "malicious_extra": "drop_table"},
            }
        )
        body_dict = msg.body.model_dump()
        assert "malicious_extra" not in body_dict
        assert body_dict == {"action": "idle"}
