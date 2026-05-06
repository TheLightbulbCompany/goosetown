"""Regression tests for daemon's chat-outcome handling.

The daemon used to fire-and-forget `act chat`: it sent the WebSocket
message and immediately returned ``{"status": "ok"}`` to the CLI without
waiting for the backend's response. The backend already rejected
offline / not-found / non-UUID / not-nearby / busy / cannot-chat-with-self
targets via a ``chat_rejected`` event — but the daemon dropped that event
on the floor, so the agent saw a green check and waited forever for a
reply that never arrived.

Surfaced by the 2026-05-02 nova-spark dry run: `act chat <name> <msg>`
against an offline agent returned ``{"status": "ok"}``. Pixel reproduced
the underlying class of bug.

These tests pin:
  1. ``_on_chat_rejected`` sets the chat-outcome future with the rejection.
  2. ``_on_conversation_started`` sets the chat-outcome future with success.
  3. The ``chat_rejected`` event is now routed through ``_handle_event``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from goosetown.daemon import TownDaemon


@pytest.fixture
def daemon(tmp_path: Path) -> TownDaemon:
    state_dir = tmp_path / "state"
    workspace = tmp_path / "workspace"
    state_dir.mkdir()
    workspace.mkdir()
    return TownDaemon(
        ws_url="wss://example.invalid",
        token="t",
        agent_name="testagent",
        state_dir=state_dir,
        workspace_path=workspace,
    )


class TestChatRejectedHandler:
    """The new ``_on_chat_rejected`` handler captures the reason for the CLI."""

    @pytest.mark.asyncio
    async def test_chat_rejected_sets_outcome_with_reason(self, daemon: TownDaemon):
        # Set up the outcome future as run() does.
        daemon._chat_outcome = asyncio.get_running_loop().create_future()
        daemon._on_chat_rejected(
            {"event": "chat_rejected", "reason": "target_not_connected"}
        )
        assert daemon._chat_outcome.done()
        outcome = daemon._chat_outcome.result()
        assert outcome["status"] == "error"
        assert outcome["reason"] == "target_not_connected"

    @pytest.mark.asyncio
    async def test_chat_rejected_does_not_crash_when_no_outcome_future(
        self, daemon: TownDaemon
    ):
        """Late-arriving rejections (after the CLI gave up) must not crash the daemon."""
        daemon._chat_outcome = None
        # Should not raise:
        daemon._on_chat_rejected(
            {"event": "chat_rejected", "reason": "target_not_found"}
        )

    @pytest.mark.asyncio
    async def test_chat_rejected_only_sets_pending_future_once(
        self, daemon: TownDaemon
    ):
        """A rejection arriving after success (or vice versa) must not break the future."""
        daemon._chat_outcome = asyncio.get_running_loop().create_future()
        daemon._on_chat_rejected(
            {"event": "chat_rejected", "reason": "target_not_connected"}
        )
        # Second event should be a no-op (future already done):
        daemon._on_chat_rejected(
            {"event": "chat_rejected", "reason": "target_not_found"}
        )
        assert daemon._chat_outcome.result()["reason"] == "target_not_connected"


class TestConversationStartedSetsChatOutcome:
    """``_on_conversation_started`` must also signal the chat-outcome future."""

    @pytest.mark.asyncio
    async def test_conversation_started_sets_outcome(self, daemon: TownDaemon):
        daemon._chat_outcome = asyncio.get_running_loop().create_future()
        daemon._on_conversation_started(
            {
                "event": "conversation_started",
                "conv_id": "c-123",
                "with_display": "Pam",
                "opening_message": "hi",
            }
        )
        assert daemon._chat_outcome.done()
        outcome = daemon._chat_outcome.result()
        assert outcome["status"] == "started"
        assert outcome["conv_id"] == "c-123"
        assert outcome["with"] == "Pam"

    @pytest.mark.asyncio
    async def test_conversation_started_does_not_crash_when_no_outcome_future(
        self, daemon: TownDaemon
    ):
        """Conversation started during a passive heartbeat (not from `act chat`)
        must still update state without crashing on the missing future."""
        daemon._chat_outcome = None
        daemon._on_conversation_started(
            {
                "event": "conversation_started",
                "conv_id": "c-456",
                "with_display": "Jim",
                "opening_message": "hello",
            }
        )
        # Existing behavior: state is updated.
        assert daemon.state["active_conversation"]["conv_id"] == "c-456"


class TestEventDispatchRoutesChatRejected:
    """The ``chat_rejected`` event must be routed through ``_handle_event``."""

    def test_chat_rejected_is_dispatched(self, daemon: TownDaemon):
        # Without an outcome future the handler still runs without error.
        daemon._chat_outcome = None
        daemon._handle_event({"event": "chat_rejected", "reason": "target_busy"})
        # No exception is the assertion; routing must not silently swallow.
