"""Unit tests for daemon_bridge.py — focus on nearby-agent currentGoal injection.

Task 7 of the Concordia Foundation Goals Subsystem: the backend must include
`currentGoal` in every nearby-agent dict that is forwarded to the daemon inside
an `agent_state` payload, so that TOWN_STATUS.md can render each neighbor's
active goal (Task 6).

The Godot game server only knows about position, facing, and display_name — it
has no awareness of DynamoDB goals.  The backend injects `currentGoal` during
the `agent_state` enrichment pass in `_send_to_daemon`.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

# Module-level patch targets (same as test_send_to_daemon_cache.py)
WS_MANAGER_PATH = "core.services.daemon_bridge.get_town_agent_ws_manager"
SERVICE_PATH = "core.services.daemon_bridge.TownService"
MAILBOX_FACTORY_PATH = "core.services.daemon_bridge.get_daemon_mailbox"
STANCES_PATH = "core.services.daemon_bridge.stances_service"


def _make_no_stances_mock() -> MagicMock:
    """Return a stances_service mock that reports no relationship for all pairs."""
    m = MagicMock()
    m.get_pair_stances.return_value = None
    return m


@pytest.fixture(autouse=True)
def stub_stances():
    """Auto-patch stances_service so existing currentGoal tests don't hit DynamoDB.

    Tests that need specific stance behaviour should pass ``patch(STANCES_PATH,
    ...)`` explicitly inside the test — that inner context manager takes
    precedence over this autouse fixture.
    """
    with patch(STANCES_PATH, _make_no_stances_mock()):
        yield


def _make_agent_conn(
    agent_id: str = "self-agent",
    connection_id: str = "conn-self",
    onboarding_phase: str = "alive",
    current_conversation_id: str = "",
):
    from core.services.town_agent_ws import AgentConnection

    return AgentConnection(
        connection_id=connection_id,
        user_id="user-1",
        agent_name="self-agent",
        agent_id=agent_id,
        instance_id="instance-1",
        onboarding_phase=onboarding_phase,
        current_conversation_id=current_conversation_id,
    )


def _make_ws_mock(agent_conn):
    mock = MagicMock()
    mock.get_by_agent_id.return_value = agent_conn
    return mock


def _make_mailbox_mock(sent_log: list):
    mock = MagicMock()
    mock.notify_connection.side_effect = lambda cid, msg: sent_log.append((cid, msg)) or True
    return mock


# ---------------------------------------------------------------------------
# Tests for currentGoal injection in nearby-agent dicts
# ---------------------------------------------------------------------------


def test_nearby_agent_payload_includes_current_goal():
    """currentGoal from DynamoDB is injected into the nearby-agent dict."""
    from core.services.daemon_bridge import _send_to_daemon

    sent: list = []
    svc_mock = MagicMock()
    svc_mock.get_town_agent_by_id.return_value = {
        "id": "alice-123",
        "displayName": "Alice",
        "currentLocation": "cafe",
        "currentGoal": "make a friend",
    }
    svc_mock.get_nearby_conversations.return_value = []

    agent_conn = _make_agent_conn()
    payload = {
        "event": "agent_state",
        "nearby_agents": [{"agent_id": "alice-123", "name": "Alice", "distance": 1.5}],
    }

    with (
        patch(WS_MANAGER_PATH, return_value=_make_ws_mock(agent_conn)),
        patch(SERVICE_PATH, return_value=svc_mock),
        patch(MAILBOX_FACTORY_PATH, return_value=_make_mailbox_mock(sent)),
    ):
        ok = _send_to_daemon("self-agent", payload)

    assert ok is True
    nearby_out = sent[0][1]["nearby_agents"]
    assert len(nearby_out) == 1
    assert nearby_out[0]["currentGoal"] == "make a friend"
    # Original fields are preserved
    assert nearby_out[0]["name"] == "Alice"
    assert nearby_out[0]["distance"] == 1.5


def test_nearby_agent_payload_currentGoal_none_when_unset():
    """currentGoal is None when the agent record has no currentGoal field."""
    from core.services.daemon_bridge import _send_to_daemon

    sent: list = []
    svc_mock = MagicMock()
    # Agent record with no currentGoal key
    svc_mock.get_town_agent_by_id.return_value = {
        "id": "alice-123",
        "displayName": "Alice",
        "currentLocation": "cafe",
    }
    svc_mock.get_nearby_conversations.return_value = []

    agent_conn = _make_agent_conn()
    payload = {
        "event": "agent_state",
        "nearby_agents": [{"agent_id": "alice-123", "name": "Alice", "distance": 1.5}],
    }

    with (
        patch(WS_MANAGER_PATH, return_value=_make_ws_mock(agent_conn)),
        patch(SERVICE_PATH, return_value=svc_mock),
        patch(MAILBOX_FACTORY_PATH, return_value=_make_mailbox_mock(sent)),
    ):
        ok = _send_to_daemon("self-agent", payload)

    assert ok is True
    nearby_out = sent[0][1]["nearby_agents"]
    assert nearby_out[0].get("currentGoal") is None


def test_nearby_agent_payload_currentGoal_none_when_empty_string():
    """Empty-string currentGoal (stale DDB write) normalizes to None."""
    from core.services.daemon_bridge import _send_to_daemon

    sent: list = []
    svc_mock = MagicMock()
    svc_mock.get_town_agent_by_id.return_value = {
        "id": "bob-456",
        "displayName": "Bob",
        "currentGoal": "",  # empty string → should normalize to None
    }
    svc_mock.get_nearby_conversations.return_value = []

    agent_conn = _make_agent_conn()
    payload = {
        "event": "agent_state",
        "nearby_agents": [{"agent_id": "bob-456", "name": "Bob", "distance": 2.0}],
    }

    with (
        patch(WS_MANAGER_PATH, return_value=_make_ws_mock(agent_conn)),
        patch(SERVICE_PATH, return_value=svc_mock),
        patch(MAILBOX_FACTORY_PATH, return_value=_make_mailbox_mock(sent)),
    ):
        ok = _send_to_daemon("self-agent", payload)

    assert ok is True
    nearby_out = sent[0][1]["nearby_agents"]
    assert nearby_out[0].get("currentGoal") is None


def test_nearby_agent_payload_currentGoal_none_when_agent_not_found():
    """If the nearby agent's DDB record is missing, currentGoal is None (not an error)."""
    from core.services.daemon_bridge import _send_to_daemon

    sent: list = []
    svc_mock = MagicMock()
    svc_mock.get_town_agent_by_id.return_value = None  # agent not found
    svc_mock.get_nearby_conversations.return_value = []

    agent_conn = _make_agent_conn()
    payload = {
        "event": "agent_state",
        "nearby_agents": [{"agent_id": "ghost-999", "name": "Ghost", "distance": 1.0}],
    }

    with (
        patch(WS_MANAGER_PATH, return_value=_make_ws_mock(agent_conn)),
        patch(SERVICE_PATH, return_value=svc_mock),
        patch(MAILBOX_FACTORY_PATH, return_value=_make_mailbox_mock(sent)),
    ):
        ok = _send_to_daemon("self-agent", payload)

    assert ok is True
    nearby_out = sent[0][1]["nearby_agents"]
    assert nearby_out[0].get("currentGoal") is None


def test_multiple_nearby_agents_all_get_current_goal():
    """Each agent in nearby_agents gets its own currentGoal injected."""
    from core.services.daemon_bridge import _send_to_daemon

    sent: list = []
    svc_mock = MagicMock()
    svc_mock.get_town_agent_by_id.side_effect = lambda aid: {
        "alice-1": {"id": "alice-1", "currentGoal": "read a book"},
        "bob-2": {"id": "bob-2", "currentGoal": None},
        "carol-3": {"id": "carol-3", "currentGoal": "find the cafe"},
    }.get(aid)
    svc_mock.get_nearby_conversations.return_value = []

    agent_conn = _make_agent_conn()
    payload = {
        "event": "agent_state",
        "nearby_agents": [
            {"agent_id": "alice-1", "name": "Alice", "distance": 1.0},
            {"agent_id": "bob-2", "name": "Bob", "distance": 2.0},
            {"agent_id": "carol-3", "name": "Carol", "distance": 2.5},
        ],
    }

    with (
        patch(WS_MANAGER_PATH, return_value=_make_ws_mock(agent_conn)),
        patch(SERVICE_PATH, return_value=svc_mock),
        patch(MAILBOX_FACTORY_PATH, return_value=_make_mailbox_mock(sent)),
    ):
        ok = _send_to_daemon("self-agent", payload)

    assert ok is True
    nearby_out = {a["agent_id"]: a for a in sent[0][1]["nearby_agents"]}
    assert nearby_out["alice-1"]["currentGoal"] == "read a book"
    assert nearby_out["bob-2"]["currentGoal"] is None
    assert nearby_out["carol-3"]["currentGoal"] == "find the cafe"
    # Exactly 3 DDB reads — one per nearby agent
    assert svc_mock.get_town_agent_by_id.call_count == 3
    svc_mock.get_town_agent_by_id.assert_has_calls([call("alice-1"), call("bob-2"), call("carol-3")], any_order=True)


def test_no_nearby_agents_no_goal_lookup():
    """When nearby_agents is empty, no DDB reads for goals."""
    from core.services.daemon_bridge import _send_to_daemon

    sent: list = []
    svc_mock = MagicMock()
    agent_conn = _make_agent_conn()

    payload = {
        "event": "agent_state",
        "nearby_agents": [],
    }

    with (
        patch(WS_MANAGER_PATH, return_value=_make_ws_mock(agent_conn)),
        patch(SERVICE_PATH, return_value=svc_mock),
        patch(MAILBOX_FACTORY_PATH, return_value=_make_mailbox_mock(sent)),
    ):
        ok = _send_to_daemon("self-agent", payload)

    assert ok is True
    svc_mock.get_town_agent_by_id.assert_not_called()
    svc_mock.get_nearby_conversations.assert_not_called()


def test_non_agent_state_event_no_goal_lookup():
    """Non-agent_state events (sprite_progress, chat_rejected, etc.) do not
    trigger any DDB reads — the goals enrichment path is guarded by event type."""
    from core.services.daemon_bridge import _send_to_daemon

    sent: list = []
    svc_mock = MagicMock()
    agent_conn = _make_agent_conn()

    with (
        patch(WS_MANAGER_PATH, return_value=_make_ws_mock(agent_conn)),
        patch(SERVICE_PATH, return_value=svc_mock),
        patch(MAILBOX_FACTORY_PATH, return_value=_make_mailbox_mock(sent)),
    ):
        ok = _send_to_daemon("self-agent", {"event": "sprite_progress", "status": "phase_b"})

    assert ok is True
    svc_mock.get_town_agent_by_id.assert_not_called()


def test_currentGoal_injected_even_when_in_conversation():
    """Goals are injected regardless of whether the sending agent is in a
    conversation — only nearby_conversations is skipped in that case."""
    from core.services.daemon_bridge import _send_to_daemon

    sent: list = []
    svc_mock = MagicMock()
    svc_mock.get_town_agent_by_id.return_value = {
        "id": "alice-1",
        "currentGoal": "enjoy the cafe",
    }
    # Agent is in a conversation — nearby_conversations should NOT be fetched
    agent_conn = _make_agent_conn(current_conversation_id="conv-active")

    payload = {
        "event": "agent_state",
        "nearby_agents": [{"agent_id": "alice-1", "name": "Alice", "distance": 1.0}],
    }

    with (
        patch(WS_MANAGER_PATH, return_value=_make_ws_mock(agent_conn)),
        patch(SERVICE_PATH, return_value=svc_mock),
        patch(MAILBOX_FACTORY_PATH, return_value=_make_mailbox_mock(sent)),
    ):
        ok = _send_to_daemon("self-agent", payload)

    assert ok is True
    # Goal was injected
    nearby_out = sent[0][1]["nearby_agents"]
    assert nearby_out[0]["currentGoal"] == "enjoy the cafe"
    # But nearby_conversations was NOT fetched (agent is in a conv)
    svc_mock.get_nearby_conversations.assert_not_called()


# ---------------------------------------------------------------------------
# Tests for stanceFromMe / stanceFromThem injection in nearby-agent dicts
# ---------------------------------------------------------------------------

STANCES_PATH = "core.services.daemon_bridge.stances_service"


def test_nearby_agent_payload_includes_stances_when_relationship_has_stances():
    """stanceFromMe and stanceFromThem are injected when a relationship with stances exists."""
    from core.services.daemon_bridge import _send_to_daemon

    sent: list = []
    svc_mock = MagicMock()
    svc_mock.get_town_agent_by_id.return_value = {"id": "alice-123", "currentGoal": None}
    svc_mock.get_nearby_conversations.return_value = []

    agent_conn = _make_agent_conn(agent_id="self-agent")
    payload = {
        "event": "agent_state",
        "nearby_agents": [{"agent_id": "alice-123", "name": "Alice", "distance": 1.5}],
    }

    stances_mock = MagicMock()
    stances_mock.get_pair_stances.return_value = {
        "stance_a_to_b": "warm and curious",
        "stance_b_to_a": "reserved but friendly",
        "updated_at": 1234567890000,
    }
    stances_mock.get_stance.side_effect = lambda viewer, subject: (
        "warm and curious" if viewer == "self-agent" else "reserved but friendly"
    )

    with (
        patch(WS_MANAGER_PATH, return_value=_make_ws_mock(agent_conn)),
        patch(SERVICE_PATH, return_value=svc_mock),
        patch(MAILBOX_FACTORY_PATH, return_value=_make_mailbox_mock(sent)),
        patch(STANCES_PATH, stances_mock),
    ):
        ok = _send_to_daemon("self-agent", payload)

    assert ok is True
    nearby_out = sent[0][1]["nearby_agents"]
    assert nearby_out[0]["stanceFromMe"] == "warm and curious"
    assert nearby_out[0]["stanceFromThem"] == "reserved but friendly"


def test_nearby_agent_stance_fields_none_when_no_relationship():
    """stanceFromMe and stanceFromThem are None when no relationship row exists."""
    from core.services.daemon_bridge import _send_to_daemon

    sent: list = []
    svc_mock = MagicMock()
    svc_mock.get_town_agent_by_id.return_value = {"id": "bob-456", "currentGoal": None}
    svc_mock.get_nearby_conversations.return_value = []

    agent_conn = _make_agent_conn(agent_id="self-agent")
    payload = {
        "event": "agent_state",
        "nearby_agents": [{"agent_id": "bob-456", "name": "Bob", "distance": 2.0}],
    }

    stances_mock = MagicMock()
    stances_mock.get_pair_stances.return_value = None  # no relationship

    with (
        patch(WS_MANAGER_PATH, return_value=_make_ws_mock(agent_conn)),
        patch(SERVICE_PATH, return_value=svc_mock),
        patch(MAILBOX_FACTORY_PATH, return_value=_make_mailbox_mock(sent)),
        patch(STANCES_PATH, stances_mock),
    ):
        ok = _send_to_daemon("self-agent", payload)

    assert ok is True
    nearby_out = sent[0][1]["nearby_agents"]
    assert nearby_out[0]["stanceFromMe"] is None
    assert nearby_out[0]["stanceFromThem"] is None


def test_nearby_agent_stance_fields_none_when_relationship_exists_but_stances_unset():
    """Both stance fields are None when a relationship exists but neither stance is set."""
    from core.services.daemon_bridge import _send_to_daemon

    sent: list = []
    svc_mock = MagicMock()
    svc_mock.get_town_agent_by_id.return_value = {"id": "carol-789", "currentGoal": None}
    svc_mock.get_nearby_conversations.return_value = []

    agent_conn = _make_agent_conn(agent_id="self-agent")
    payload = {
        "event": "agent_state",
        "nearby_agents": [{"agent_id": "carol-789", "name": "Carol", "distance": 2.5}],
    }

    stances_mock = MagicMock()
    # Relationship exists but neither stance column is set
    stances_mock.get_pair_stances.return_value = {
        "stance_a_to_b": None,
        "stance_b_to_a": None,
        "updated_at": None,
    }
    stances_mock.get_stance.return_value = None

    with (
        patch(WS_MANAGER_PATH, return_value=_make_ws_mock(agent_conn)),
        patch(SERVICE_PATH, return_value=svc_mock),
        patch(MAILBOX_FACTORY_PATH, return_value=_make_mailbox_mock(sent)),
        patch(STANCES_PATH, stances_mock),
    ):
        ok = _send_to_daemon("self-agent", payload)

    assert ok is True
    nearby_out = sent[0][1]["nearby_agents"]
    assert nearby_out[0]["stanceFromMe"] is None
    assert nearby_out[0]["stanceFromThem"] is None
