"""Unit tests for ``_handle_agent_activity`` in ``routers/town.py``.

Covers the validation ladder an incoming ``town_act <activity>`` command
walks through before the activity timer is scheduled:

* Unknown activity name → ``activity_rejected:unknown_activity``.
* Agent row missing from DynamoDB → ``agent_not_found``.
* Agent in wrong location context (town vs apartment) → ``wrong_context``
  with guidance message.
* Agent at a different fine-grained location → ``wrong_location``.
* Agent already busy with another activity → ``already_busy``.
* Happy path — DB flip, game-server notify, act_ok ack, timer scheduled.

The fire-and-forget timer (``_activity_timer``) is patched to a no-op so
tests don't wait on ``activity.duration_seconds``.  Custom-animation
generation is suppressed by patching ``_custom_anim_in_progress`` to a
sentinel set so the pixellab code path is not exercised.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MAILBOX_FACTORY_PATH = "core.services.activity_service.get_daemon_mailbox"
# Activity handlers moved to core.services.activity_service (sub-project #3
# third slice, 2026-04-22). Patch the module-top imports at the new home.
SERVICE_PATH = "core.services.activity_service.TownService"
GS_CLIENT_PATH = "core.services.activity_service.get_game_server_client"
GET_ACTIVITY_PATH = "core.services.activity_service.get_activity"
# Both of the below are imported DEFERRED inside `_handle_agent_activity`,
# so we patch them at the source (activity_registry) rather than at the
# activity_service namespace. Keep this in mind when adding new deferred
# imports: the patch target follows the import style.
CONTEXT_MSG_PATH = "core.services.activity_registry.get_context_mismatch_message"
CANONICAL_LOCATION_PATH = "core.services.activity_registry.canonical_location"
TIMER_PATH = "core.services.activity_service._activity_timer"


def _make_activity(
    name: str = "read",
    location: str = "library",
    duration_seconds: int = 10,
    animation: str = "reading",
    location_context: str = "town",
    spot: str | None = None,
) -> SimpleNamespace:
    """Build an Activity-shaped object without importing the dataclass.

    The handler accesses these attributes only: ``name``, ``location``,
    ``duration_seconds``, ``animation``, ``location_context``, ``spot``.
    """
    return SimpleNamespace(
        name=name,
        location=location,
        duration_seconds=duration_seconds,
        animation=animation,
        location_context=location_context,
        spot=spot,
    )


def _make_agent_conn(agent_id: str = "agent-1") -> SimpleNamespace:
    return SimpleNamespace(
        agent_id=agent_id,
        connection_id="conn-abc",
        user_id="user-1",
        agent_name="Lucky",
        location_context="town",
    )


def _make_agent(
    location_context: str = "town",
    current_location: str = "library",
    current_activity: str | None = None,
    pixellab_character_id: str | None = None,
    sprite_animations: dict | None = None,
) -> dict:
    return {
        "id": "agent-1",
        "displayName": "Lucky",
        "locationContext": location_context,
        "currentLocation": current_location,
        "currentActivity": current_activity,
        "pixellabCharacterId": pixellab_character_id,
        "spriteAnimations": sprite_animations or {},
    }


def _mailbox_mock(sent: list) -> MagicMock:
    mock = MagicMock()
    mock.notify_connection.side_effect = lambda cid, msg: sent.append((cid, msg))
    return mock


def _gs_mock() -> MagicMock:
    mock = MagicMock()
    mock.send_command = AsyncMock()
    return mock


async def _noop_timer(*args, **kwargs) -> None:
    """Replacement for _activity_timer that completes instantly."""
    return None


class TestHandleAgentActivity:
    @pytest.mark.asyncio
    async def test_unknown_activity_rejects(self):
        from routers.town import _handle_agent_activity

        sent: list = []
        svc_mock = MagicMock()

        with (
            patch(MAILBOX_FACTORY_PATH, return_value=_mailbox_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(GET_ACTIVITY_PATH, return_value=None),
        ):
            await _handle_agent_activity("conn-abc", "bogus_activity", _make_agent_conn())

        assert sent[0][1]["event"] == "activity_rejected"
        assert sent[0][1]["reason"] == "unknown_activity"
        assert sent[0][1]["activity"] == "bogus_activity"
        svc_mock.get_town_agent_by_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_agent_not_found_rejects(self):
        from routers.town import _handle_agent_activity

        sent: list = []
        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.return_value = None

        with (
            patch(MAILBOX_FACTORY_PATH, return_value=_mailbox_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(GET_ACTIVITY_PATH, return_value=_make_activity()),
        ):
            await _handle_agent_activity("conn-abc", "read", _make_agent_conn())

        assert sent[0][1]["reason"] == "agent_not_found"
        svc_mock.update_agent_activity.assert_not_called()

    @pytest.mark.asyncio
    async def test_wrong_context_rejects_with_guidance(self):
        """Apartment-only activity attempted from town gets a hint message."""
        from routers.town import _handle_agent_activity

        sent: list = []
        activity = _make_activity(name="cook", location="apartment", location_context="apartment")
        agent = _make_agent(location_context="town")
        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.return_value = agent

        with (
            patch(MAILBOX_FACTORY_PATH, return_value=_mailbox_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(GET_ACTIVITY_PATH, return_value=activity),
            patch(CONTEXT_MSG_PATH, return_value="go to your apartment first"),
        ):
            await _handle_agent_activity("conn-abc", "cook", _make_agent_conn())

        assert sent[0][1]["reason"] == "wrong_context"
        assert sent[0][1]["message"] == "go to your apartment first"
        svc_mock.update_agent_activity.assert_not_called()

    @pytest.mark.asyncio
    async def test_wrong_location_rejects(self):
        """Activity at one location when agent is standing at another."""
        from routers.town import _handle_agent_activity

        sent: list = []
        activity = _make_activity(name="read", location="library")
        # Agent is at plaza, not library
        agent = _make_agent(current_location="plaza")
        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.return_value = agent

        with (
            patch(MAILBOX_FACTORY_PATH, return_value=_mailbox_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(GET_ACTIVITY_PATH, return_value=activity),
            patch(CONTEXT_MSG_PATH, return_value=None),
            patch(CANONICAL_LOCATION_PATH, side_effect=lambda loc: loc),
        ):
            await _handle_agent_activity("conn-abc", "read", _make_agent_conn())

        assert sent[0][1]["reason"] == "wrong_location"
        svc_mock.update_agent_activity.assert_not_called()

    @pytest.mark.asyncio
    async def test_agent_fetch_uses_consistent_read(self):
        """Regression: location validation reads must be strongly-consistent.

        The ``arrived`` event from the game server writes ``currentLocation``
        immediately before notifying the daemon. An agent that fires
        ``act <activity>`` on the back of that update can hit the activity
        handler within DynamoDB's eventual-consistency window and see a
        stale ``currentLocation``, producing a spurious ``wrong_location``
        rejection. Surfaced 2026-05-06 by linus's first-attempt-rejected,
        second-attempt-succeeded pattern in the integration test.

        Fix is opt-in strong consistency at the validation read site only —
        every other ``get_town_agent_by_id`` call site stays on the cheaper
        eventually-consistent default.
        """
        from routers.town import _handle_agent_activity

        sent: list = []
        activity = _make_activity(name="read", location="library")
        agent = _make_agent(current_location="library")
        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.return_value = agent

        with (
            patch(MGMT_PATH, return_value=_mgmt_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(GET_ACTIVITY_PATH, return_value=activity),
            patch(CONTEXT_MSG_PATH, return_value=None),
            patch(CANONICAL_LOCATION_PATH, side_effect=lambda loc: loc),
            patch(TIMER_PATH, side_effect=_noop_timer),
        ):
            await _handle_agent_activity("conn-abc", "read", _make_agent_conn())

        svc_mock.get_town_agent_by_id.assert_called_once_with("agent-1", consistent_read=True)

    @pytest.mark.asyncio
    async def test_already_busy_rejects(self):
        from routers.town import _handle_agent_activity

        sent: list = []
        activity = _make_activity(name="read", location="library")
        agent = _make_agent(current_location="library", current_activity="study")
        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.return_value = agent

        with (
            patch(MAILBOX_FACTORY_PATH, return_value=_mailbox_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(GET_ACTIVITY_PATH, return_value=activity),
            patch(CONTEXT_MSG_PATH, return_value=None),
            patch(CANONICAL_LOCATION_PATH, side_effect=lambda loc: loc),
        ):
            await _handle_agent_activity("conn-abc", "read", _make_agent_conn())

        assert sent[0][1]["reason"] == "already_busy"
        svc_mock.update_agent_activity.assert_not_called()

    @pytest.mark.asyncio
    async def test_happy_path_marks_busy_notifies_and_acks(self):
        """Valid start flips DB, tells game server, acks agent, schedules timer."""
        from routers.town import _handle_agent_activity

        sent: list = []
        activity = _make_activity(name="read", location="library", duration_seconds=10, animation="reading")
        agent = _make_agent(current_location="library")
        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.return_value = agent
        gs = _gs_mock()

        with (
            patch(MAILBOX_FACTORY_PATH, return_value=_mailbox_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(GS_CLIENT_PATH, return_value=gs),
            patch(GET_ACTIVITY_PATH, return_value=activity),
            patch(CONTEXT_MSG_PATH, return_value=None),
            patch(CANONICAL_LOCATION_PATH, side_effect=lambda loc: loc),
            patch(TIMER_PATH, side_effect=_noop_timer),
        ):
            await _handle_agent_activity("conn-abc", "read", _make_agent_conn())

        # DB flip: activity name + start timestamp
        svc_mock.update_agent_activity.assert_called_once()
        call_args = svc_mock.update_agent_activity.call_args
        assert call_args.args[0] == "agent-1"
        assert call_args.args[1] == "read"
        # third arg is a float timestamp

        # Game server notified of new activity
        gs.send_command.assert_any_await("agent-1", {"cmd": "set_activity", "activity": "read"})

        # Agent got act_ok with duration
        ack = [msg for _, msg in sent if msg.get("event") == "act_ok"]
        assert len(ack) == 1
        assert ack[0]["activity"] == "read"
        assert ack[0]["duration_seconds"] == 10

    @pytest.mark.asyncio
    async def test_apartment_spot_name_canonicalised(self):
        """Agent at apartment spot ``desk`` should pass location check.

        Relies on ``canonical_location`` mapping all apartment spot names to
        the single ``"apartment"`` key. The handler accepts this as matching
        an activity with ``location="apartment"``.
        """
        from routers.town import _handle_agent_activity

        sent: list = []
        activity = _make_activity(name="work", location="apartment", location_context="apartment")
        agent = _make_agent(
            location_context="apartment",
            current_location="desk",
        )
        svc_mock = MagicMock()
        svc_mock.get_town_agent_by_id.return_value = agent
        gs = _gs_mock()

        # canonical_location("desk") → "apartment" to match activity.location
        def _canonical(loc):
            return "apartment" if loc == "desk" else loc

        with (
            patch(MAILBOX_FACTORY_PATH, return_value=_mailbox_mock(sent)),
            patch(SERVICE_PATH, return_value=svc_mock),
            patch(GS_CLIENT_PATH, return_value=gs),
            patch(GET_ACTIVITY_PATH, return_value=activity),
            patch(CONTEXT_MSG_PATH, return_value=None),
            patch(CANONICAL_LOCATION_PATH, side_effect=_canonical),
            patch(TIMER_PATH, side_effect=_noop_timer),
        ):
            await _handle_agent_activity("conn-abc", "work", _make_agent_conn())

        # No rejection — activity started
        rejects = [msg for _, msg in sent if msg.get("event") == "activity_rejected"]
        assert rejects == []
        svc_mock.update_agent_activity.assert_called_once()
