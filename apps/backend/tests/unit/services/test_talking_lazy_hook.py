"""Unit tests for ``_maybe_enqueue_talking`` in conversation_service.

Spec 2026-06-11: first conversation triggers lazy 'talking' animation enqueue
if: flag on, agent row exists, pixellabCharacterId present, and no existing
'talking' animation. All guard conditions are independently tested.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# _maybe_enqueue_talking direct unit tests
# ---------------------------------------------------------------------------


def _row(*, char_id="char-abc", talking_present=False, agent_id="agent-123"):
    """Minimal agent row for testing."""
    anims = {"talking": {"url": "https://cdn/t.png", "frameCount": 4}} if talking_present else {}
    return {
        "id": agent_id,
        "pixellabCharacterId": char_id,
        "spriteAnimations": anims,
    }


@pytest.mark.parametrize(
    "flag,char_id,talking_present,row_none,expect_enqueue",
    [
        # Normal happy path
        (True, "char-abc", False, False, True),
        # Flag off → skip
        (False, "char-abc", False, False, False),
        # Already has talking animation → skip
        (True, "char-abc", True, False, False),
        # No pixellabCharacterId → skip
        (True, None, False, False, False),
        # Row is None → skip
        (True, "char-abc", False, True, False),
    ],
)
def test_maybe_enqueue_talking_guard_conditions(flag, char_id, talking_present, row_none, expect_enqueue):
    """Each guard condition independently prevents enqueue when not met."""
    from core.services.conversation_service import _maybe_enqueue_talking

    row = None if row_none else _row(char_id=char_id, talking_present=talking_present)

    with (
        patch("core.config.settings") as mock_settings,
        patch("core.services.sprite_jobs.enqueue_animation") as mock_enqueue,
    ):
        mock_settings.ACTIVITY_ANIMATION_GENERATION = flag
        _maybe_enqueue_talking(row)

    if expect_enqueue:
        mock_enqueue.assert_called_once_with("agent-123", "talking")
    else:
        mock_enqueue.assert_not_called()


def test_maybe_enqueue_talking_calls_enqueue_with_correct_args():
    """When all guards pass, enqueue_animation is called with the agent's id and 'talking'."""
    from core.services.conversation_service import _maybe_enqueue_talking

    row = _row(agent_id="my-agent-uuid", char_id="char-xyz", talking_present=False)

    with (
        patch("core.config.settings") as mock_settings,
        patch("core.services.sprite_jobs.enqueue_animation") as mock_enqueue,
    ):
        mock_settings.ACTIVITY_ANIMATION_GENERATION = True
        _maybe_enqueue_talking(row)

    mock_enqueue.assert_called_once_with("my-agent-uuid", "talking")


def test_maybe_enqueue_talking_missing_animations_key():
    """Row with no 'spriteAnimations' key at all is treated as no talking present."""
    from core.services.conversation_service import _maybe_enqueue_talking

    row = {"id": "a1", "pixellabCharacterId": "char-1"}

    with (
        patch("core.config.settings") as mock_settings,
        patch("core.services.sprite_jobs.enqueue_animation") as mock_enqueue,
    ):
        mock_settings.ACTIVITY_ANIMATION_GENERATION = True
        _maybe_enqueue_talking(row)

    mock_enqueue.assert_called_once_with("a1", "talking")


# ---------------------------------------------------------------------------
# Integration-style: _handle_chat calls _maybe_enqueue_talking for both parties
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_chat_calls_maybe_enqueue_talking_for_both_participants():
    """After conversation is created, _maybe_enqueue_talking must be called for
    both the initiator and the target (wrapped in a try/except so it's non-fatal).
    We verify by patching _maybe_enqueue_talking at conversation_service level and
    checking both call arguments.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from core.services.conversation_service import _handle_chat

    initiator_id = "11111111-1111-1111-1111-111111111111"
    target_id = "22222222-2222-2222-2222-222222222222"

    initiator_row = {
        "id": initiator_id,
        "displayName": "Init",
        "currentLocation": "plaza",
        "currentConversationId": None,
        "currentActivity": None,
        "isActive": "true",
        "pixellabCharacterId": "char-i",
        "spriteAnimations": {},
        "agentName": "init-name",
    }
    target_row = {
        "id": target_id,
        "displayName": "Target",
        "currentLocation": "plaza",
        "currentConversationId": None,
        "currentActivity": None,
        "isActive": "true",
        "agentName": "target-name",
        "pixellabCharacterId": "char-t",
        "spriteAnimations": {},
    }
    conv_row = {
        "id": "conv-1",
        "participants": [initiator_id, target_id],
        "location": "plaza",
    }

    mock_service = MagicMock()
    mock_service.get_town_agent_by_id.side_effect = lambda aid: (
        initiator_row if aid == initiator_id else (target_row if aid == target_id else None)
    )
    mock_service.create_active_conversation.return_value = conv_row

    mock_ws_manager = MagicMock()
    mock_ws_manager.get_by_agent_id.return_value = SimpleNamespace(connection_id="target-conn")

    mock_gs_client = MagicMock()
    mock_gs_client.get_cached_agent_state.return_value = None
    mock_gs_client.send_command = AsyncMock()

    mock_mailbox = MagicMock()
    mock_mailbox.notify_connection.return_value = True
    mock_mailbox.reject = MagicMock()

    agent_conn = SimpleNamespace(
        agent_id=initiator_id,
        agent_name="init-name",
    )
    body = {
        "target": target_id,
        "message": "Hello there!",
    }

    with (
        patch("core.services.conversation_service.TownService", return_value=mock_service),
        patch("core.services.conversation_service.get_town_agent_ws_manager", return_value=mock_ws_manager),
        patch("core.services.conversation_service.get_game_server_client", return_value=mock_gs_client),
        patch(
            "core.services.daemon_mailbox.get_daemon_mailbox",
            return_value=mock_mailbox,
        ),
        patch("core.services.conversation_service._maybe_enqueue_talking") as mock_enq,
        patch("core.services.conversation_service._schedule_inactivity_timeout"),
    ):
        await _handle_chat("init-conn", body, agent_conn)

    # Both participants must have had _maybe_enqueue_talking called
    call_args_list = mock_enq.call_args_list
    called_rows = [c.args[0] for c in call_args_list]
    called_ids = {r.get("id") for r in called_rows if r}
    assert initiator_id in called_ids, f"initiator not enqueued, calls: {call_args_list}"
    assert target_id in called_ids, f"target not enqueued, calls: {call_args_list}"
