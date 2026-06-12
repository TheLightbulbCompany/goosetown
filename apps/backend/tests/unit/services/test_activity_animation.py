"""Activity animation lazy-gen: SQS enqueue replaces in-process generation.

The old ``_generate_activity_animation`` coroutine was deleted in Task 7;
activity animation requests now call ``sprite_jobs.enqueue_animation`` so
the SQS sprite worker handles the PixelLab call outside the backend process.

These tests confirm:
* ``enqueue_animation`` is called when the flag is on and the animation is
  in the ANIMATION_REGISTRY with a pixellabCharacterId present.
* When ``ACTIVITY_ANIMATION_GENERATION`` is False, enqueue is skipped.
* When pixellabCharacterId is absent, the warning path fires (no enqueue).
* Dedup via ``_custom_anim_in_progress`` prevents double-enqueues.
"""

from unittest.mock import MagicMock, patch

import pytest


def _make_agent(*, char_id="char-xyz", animations=None):
    return {
        "id": "agent-abc",
        "pixellabCharacterId": char_id,
        "spriteAnimations": animations or {},
        "currentLocation": "library",
        "locationContext": "town",
        "currentActivity": None,
        "currentConversationId": None,
    }


def _make_activity(name="read", animation="reading"):
    from types import SimpleNamespace

    return SimpleNamespace(
        name=name,
        animation=animation,
        location="library",
        location_context="town",
        duration_seconds=15,
        spot=None,
    )


@pytest.mark.asyncio
async def test_activity_start_enqueues_animation_when_flag_on():
    """Flag on, animation missing, char_id present → enqueue_animation called."""
    import core.services.activity_service as act

    activity = _make_activity()
    agent = _make_agent()
    # Clear in-progress set to avoid cross-test pollution
    act._custom_anim_in_progress.clear()

    with (
        patch("core.services.activity_service.get_activity", return_value=activity),
        patch("core.services.activity_service.TownService") as SvcCls,
        patch("core.services.activity_service.get_game_server_client") as gs_f,
        patch("core.services.activity_service.get_daemon_mailbox") as mb_f,
        patch("core.services.activity_service.ANIMATION_REGISTRY", {"reading": {"fps": 8}}),
        patch("core.services.sprite_jobs.enqueue_animation") as mock_enqueue,
    ):
        svc = MagicMock()
        svc.get_town_agent_by_id.return_value = agent
        SvcCls.return_value = svc

        gs = MagicMock()
        gs.send_command = MagicMock(return_value=None)
        gs_f.return_value = gs

        mb = MagicMock()
        mb_f.return_value = mb

        with (
            patch("core.config.settings") as mock_settings,
            patch("core.services.activity_service.asyncio.create_task"),
        ):
            mock_settings.ACTIVITY_ANIMATION_GENERATION = True
            from types import SimpleNamespace

            conn = SimpleNamespace(agent_id="agent-abc", agent_name="test")
            await act._handle_agent_activity("conn-1", "read", conn)

    mock_enqueue.assert_called_once_with("agent-abc", "reading")


@pytest.mark.asyncio
async def test_activity_start_skips_animation_when_flag_off():
    """Flag off → enqueue_animation must NOT be called."""
    import core.services.activity_service as act

    activity = _make_activity()
    agent = _make_agent()
    act._custom_anim_in_progress.clear()

    with (
        patch("core.services.activity_service.get_activity", return_value=activity),
        patch("core.services.activity_service.TownService") as SvcCls,
        patch("core.services.activity_service.get_game_server_client") as gs_f,
        patch("core.services.activity_service.get_daemon_mailbox") as mb_f,
        patch("core.services.activity_service.ANIMATION_REGISTRY", {"reading": {"fps": 8}}),
        patch("core.services.sprite_jobs.enqueue_animation") as mock_enqueue,
    ):
        svc = MagicMock()
        svc.get_town_agent_by_id.return_value = agent
        SvcCls.return_value = svc

        gs = MagicMock()
        gs.send_command = MagicMock(return_value=None)
        gs_f.return_value = gs

        mb = MagicMock()
        mb_f.return_value = mb

        with (
            patch("core.config.settings") as mock_settings,
            patch("core.services.activity_service.asyncio.create_task"),
        ):
            mock_settings.ACTIVITY_ANIMATION_GENERATION = False
            from types import SimpleNamespace

            conn = SimpleNamespace(agent_id="agent-abc", agent_name="test")
            await act._handle_agent_activity("conn-1", "read", conn)

    mock_enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_activity_start_skips_animation_when_no_char_id():
    """No pixellabCharacterId → warning path, no enqueue."""
    import core.services.activity_service as act

    activity = _make_activity()
    agent = _make_agent(char_id=None)
    # Remove pixellabCharacterId entirely to test the missing-key path
    agent.pop("pixellabCharacterId", None)
    act._custom_anim_in_progress.clear()

    with (
        patch("core.services.activity_service.get_activity", return_value=activity),
        patch("core.services.activity_service.TownService") as SvcCls,
        patch("core.services.activity_service.get_game_server_client") as gs_f,
        patch("core.services.activity_service.get_daemon_mailbox") as mb_f,
        patch("core.services.activity_service.ANIMATION_REGISTRY", {"reading": {"fps": 8}}),
        patch("core.services.sprite_jobs.enqueue_animation") as mock_enqueue,
    ):
        svc = MagicMock()
        svc.get_town_agent_by_id.return_value = agent
        SvcCls.return_value = svc

        gs = MagicMock()
        gs.send_command = MagicMock(return_value=None)
        gs_f.return_value = gs

        mb = MagicMock()
        mb_f.return_value = mb

        with (
            patch("core.config.settings") as mock_settings,
            patch("core.services.activity_service.asyncio.create_task"),
        ):
            mock_settings.ACTIVITY_ANIMATION_GENERATION = True
            from types import SimpleNamespace

            conn = SimpleNamespace(agent_id="agent-abc", agent_name="test")
            await act._handle_agent_activity("conn-1", "read", conn)

    mock_enqueue.assert_not_called()
