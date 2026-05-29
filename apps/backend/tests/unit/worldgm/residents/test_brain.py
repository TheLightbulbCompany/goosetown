"""Tests for worldgm.residents.brain: read_status + step_resident_brain."""

from unittest.mock import AsyncMock

from worldgm.residents.behavior import ResidentRuntime
from worldgm.residents.brain import read_status, step_resident_brain


def _rt(*, name="tobias", location="cafe", state="idle", idle_ticks=3, hangout="plaza"):
    return ResidentRuntime(
        name=name, location=location, state=state, idle_ticks=idle_ticks, hangout=hangout
    )


# ---------------------------------------------------------------------------
# read_status
# ---------------------------------------------------------------------------


def test_read_status_extracts_from_daemon_state():
    class D:
        state = {
            "agent": {"location": "library", "activity": "reading"},
            "active_conversation": {"conv_id": "c2"},
        }

    out = read_status(D())
    assert out == {
        "location": "library",
        "activity": "reading",
        "active_conversation": {"conv_id": "c2"},
    }


def test_read_status_empty_daemon_state_returns_unknown():
    class D:
        state = {}

    out = read_status(D())
    assert out["location"] == "unknown"
    assert out["activity"] == "idle"
    assert out["active_conversation"] is None


def test_read_status_none_daemon_state_returns_unknown():
    class D:
        state = None

    out = read_status(D())
    assert out["location"] == "unknown"


def test_read_status_missing_state_attr_returns_unknown():
    class D:
        pass

    out = read_status(D())
    assert out["location"] == "unknown"
    assert out["active_conversation"] is None


def test_read_status_no_active_conversation_key():
    class D:
        state = {"agent": {"location": "cafe", "activity": "idle"}}

    out = read_status(D())
    assert out["active_conversation"] is None


# ---------------------------------------------------------------------------
# step_resident_brain — no-op during conversation
# ---------------------------------------------------------------------------


async def test_brain_no_action_during_conversation():
    daemon = AsyncMock()
    rt = _rt(location="cafe", state="idle", idle_ticks=3, hangout="plaza")
    status = {"location": "cafe", "activity": "idle", "active_conversation": {"conv_id": "c1"}}
    await step_resident_brain(daemon, rt, status)
    daemon.handle_tool_command.assert_not_awaited()


async def test_brain_no_action_during_conversation_even_high_idle_ticks():
    """Even with idle_ticks >> threshold, conversation blocks all action."""
    daemon = AsyncMock()
    rt = _rt(location="library", state="idle", idle_ticks=100, hangout="plaza")
    status = {
        "location": "library",
        "activity": "idle",
        "active_conversation": {"conv_id": "x"},
    }
    await step_resident_brain(daemon, rt, status)
    daemon.handle_tool_command.assert_not_awaited()


# ---------------------------------------------------------------------------
# step_resident_brain — issues move when idle and not at hangout
# ---------------------------------------------------------------------------


async def test_brain_issues_move_when_idle():
    daemon = AsyncMock()
    rt = _rt(location="cafe", state="idle", idle_ticks=3, hangout="plaza")
    status = {"location": "cafe", "activity": "idle", "active_conversation": None}
    await step_resident_brain(daemon, rt, status, rng=lambda: 0.0)
    daemon.handle_tool_command.assert_awaited_once()
    sent = daemon.handle_tool_command.call_args[0][0]
    assert sent["action"] == "act"
    assert sent["payload"]["action"] == "move"


async def test_brain_move_cmd_destination_is_hangout_when_away():
    daemon = AsyncMock()
    rt = _rt(location="cafe", state="idle", idle_ticks=3, hangout="plaza")
    status = {"location": "cafe", "activity": "idle", "active_conversation": None}
    await step_resident_brain(daemon, rt, status, rng=lambda: 0.0)
    sent = daemon.handle_tool_command.call_args[0][0]
    assert sent["payload"]["destination"] == "plaza"


# ---------------------------------------------------------------------------
# step_resident_brain — runtime state transitions
# ---------------------------------------------------------------------------


async def test_brain_marks_runtime_moving_after_issuing_move():
    """After a move command is issued, runtime.state must be 'moving'."""
    daemon = AsyncMock()
    rt = _rt(location="cafe", state="idle", idle_ticks=3, hangout="plaza")
    status = {"location": "cafe", "activity": "idle", "active_conversation": None}
    await step_resident_brain(daemon, rt, status, rng=lambda: 0.0)
    assert rt.state == "moving"
    assert rt.idle_ticks == 0


async def test_brain_ignores_unreliable_activity_field():
    """The daemon never reports activity=='moving' (movement isn't surfaced in
    daemon.state), so the brain must NOT treat the activity field as a movement
    gate — it decides purely on dwell time + location. Here, idle and away from
    the hangout, it issues a move even though activity says 'moving'."""
    daemon = AsyncMock()
    rt = _rt(location="cafe", state="idle", idle_ticks=3, hangout="plaza")
    status = {"location": "cafe", "activity": "moving", "active_conversation": None}
    await step_resident_brain(daemon, rt, status, rng=lambda: 0.0)
    daemon.handle_tool_command.assert_awaited_once()
    sent = daemon.handle_tool_command.call_args[0][0]
    assert sent["payload"]["destination"] == "plaza"
    assert rt.state == "moving"


async def test_brain_resets_idle_ticks_on_location_change():
    """When daemon reports a new location, idle_ticks should reset to 0+1."""
    daemon = AsyncMock()
    rt = _rt(location="cafe", state="idle", idle_ticks=5, hangout="library")
    # Daemon says we're now at library — just arrived
    status = {"location": "library", "activity": "idle", "active_conversation": None}
    # rng=0.9 → no wander; settled at hangout
    await step_resident_brain(daemon, rt, status, rng=lambda: 0.9)
    # No move issued (settled at hangout, rng=0.9 >= 0.25)
    daemon.handle_tool_command.assert_not_awaited()
    # idle_ticks reset to 0 then incremented to 1
    assert rt.idle_ticks == 1
    assert rt.location == "library"


async def test_brain_increments_idle_ticks_when_stable():
    """Each tick where nothing moves should increment idle_ticks."""
    daemon = AsyncMock()
    rt = _rt(location="plaza", state="idle", idle_ticks=1, hangout="plaza")
    status = {"location": "plaza", "activity": "idle", "active_conversation": None}
    # rng=0.9 → no wander
    await step_resident_brain(daemon, rt, status, rng=lambda: 0.9)
    daemon.handle_tool_command.assert_not_awaited()
    assert rt.idle_ticks == 2
