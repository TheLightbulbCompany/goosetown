"""Regression tests for the `_send_to_daemon` hot-path cache.

Before this cache, every call hit DynamoDB via
``TownService.get_town_agent_by_id`` — once every 2 seconds per agent
because the game server pushes ``agent_state`` on that cadence. These
tests assert the new behavior:

* Non-``agent_state`` events do zero DB reads (everything needed lives
  on the cached ``AgentConnection``).
* ``agent_state`` events with the sender already in a conversation
  do zero DB reads (``nearby_conversations`` is gated on the cached
  ``current_conversation_id``).
* ``agent_state`` events with nearby agents and no own conversation
  make exactly one DB call — to ``get_nearby_conversations`` — not
  an extra ``get_town_agent_by_id`` lookup on top.
* Agents absent from ``ws_manager`` short-circuit to ``False`` without
  falling back to DynamoDB.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# `_send_to_daemon` was extracted to core.services.daemon_bridge
# (sub-project #3 fourth slice, 2026-04-22). Patch the module-top bindings
# there, not the routers.town re-export.
WS_MANAGER_PATH = "core.services.daemon_bridge.get_town_agent_ws_manager"
SERVICE_PATH = "core.services.daemon_bridge.TownService"
MAILBOX_FACTORY_PATH = "core.services.daemon_bridge.get_daemon_mailbox"


def _make_agent_conn(
    agent_id: str = "agent-1",
    connection_id: str = "conn-abc",
    onboarding_phase: str = "alive",
    current_conversation_id: str = "",
):
    from core.services.town_agent_ws import AgentConnection

    return AgentConnection(
        connection_id=connection_id,
        user_id="user-1",
        agent_name="test-agent",
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


def test_non_agent_state_does_no_db_reads():
    """sprite_progress, chat_rejected, say_rejected etc. should NOT touch DynamoDB."""
    from routers.town import _send_to_daemon

    sent: list = []
    svc_mock = MagicMock()
    agent_conn = _make_agent_conn()

    with (
        patch(WS_MANAGER_PATH, return_value=_make_ws_mock(agent_conn)),
        patch(SERVICE_PATH, return_value=svc_mock),
        patch(MAILBOX_FACTORY_PATH, return_value=_make_mailbox_mock(sent)),
    ):
        ok = _send_to_daemon("agent-1", {"event": "sprite_progress", "status": "..."})

    assert ok is True
    svc_mock.get_town_agent_by_id.assert_not_called()
    svc_mock.get_nearby_conversations.assert_not_called()
    assert sent and sent[0][0] == "conn-abc"


def test_agent_state_while_in_conversation_skips_nearby_conv_fetch():
    """When the agent already has a current_conversation_id, nearby_conversations
    is irrelevant — skip that DB call.  However, get_town_agent_by_id IS called
    per-nearby-agent to inject currentGoal."""
    from routers.town import _send_to_daemon

    sent: list = []
    svc_mock = MagicMock()
    svc_mock.get_town_agent_by_id.return_value = {"id": "agent-2", "currentGoal": None}
    agent_conn = _make_agent_conn(current_conversation_id="conv-xyz")

    payload = {
        "event": "agent_state",
        "nearby_agents": [{"agent_id": "agent-2"}, {"agent_id": "agent-3"}],
    }
    with (
        patch(WS_MANAGER_PATH, return_value=_make_ws_mock(agent_conn)),
        patch(SERVICE_PATH, return_value=svc_mock),
        patch(MAILBOX_FACTORY_PATH, return_value=_make_mailbox_mock(sent)),
    ):
        ok = _send_to_daemon("agent-1", payload)

    assert ok is True
    # get_town_agent_by_id is called once per nearby agent for currentGoal injection
    assert svc_mock.get_town_agent_by_id.call_count == 2
    # nearby_conversations is skipped because agent is in a conversation
    svc_mock.get_nearby_conversations.assert_not_called()


def test_agent_state_no_own_conv_with_nearby_agents_enriches_goals_and_convs():
    """Sender not in a conv + has nearby agents → get_town_agent_by_id called
    per-nearby-agent (for currentGoal), plus one get_nearby_conversations call."""
    from routers.town import _send_to_daemon

    sent: list = []
    svc_mock = MagicMock()
    svc_mock.get_town_agent_by_id.side_effect = lambda aid: {
        "agent-2": {"id": "agent-2", "currentGoal": "make a friend"},
        "agent-3": {"id": "agent-3", "currentGoal": None},
    }.get(aid)
    svc_mock.get_nearby_conversations.return_value = [
        {"conv_id": "c1", "participants": ["agent-2"], "last_message": {}, "location": "plaza"}
    ]
    agent_conn = _make_agent_conn(onboarding_phase="alive")

    payload = {
        "event": "agent_state",
        "nearby_agents": [{"agent_id": "agent-2"}, {"agent_id": "agent-3"}],
    }
    with (
        patch(WS_MANAGER_PATH, return_value=_make_ws_mock(agent_conn)),
        patch(SERVICE_PATH, return_value=svc_mock),
        patch(MAILBOX_FACTORY_PATH, return_value=_make_mailbox_mock(sent)),
    ):
        ok = _send_to_daemon("agent-1", payload)

    assert ok is True
    # get_town_agent_by_id called once per nearby agent for currentGoal
    assert svc_mock.get_town_agent_by_id.call_count == 2
    svc_mock.get_nearby_conversations.assert_called_once_with(["agent-2", "agent-3"])
    # Enrichment landed on the outgoing payload
    assert sent[0][1]["onboardingPhase"] == "alive"
    assert sent[0][1]["nearby_conversations"][0]["conv_id"] == "c1"
    # currentGoal injected into nearby_agents
    nearby_out = sent[0][1]["nearby_agents"]
    agent2 = next(a for a in nearby_out if a["agent_id"] == "agent-2")
    agent3 = next(a for a in nearby_out if a["agent_id"] == "agent-3")
    assert agent2["currentGoal"] == "make a friend"
    assert agent3["currentGoal"] is None


def test_missing_from_ws_manager_returns_false_without_db():
    """If the agent isn't in ws_manager there's no daemon connection to
    send to — short-circuit with False, do NOT fall back to DynamoDB."""
    from routers.town import _send_to_daemon

    ws_mock = MagicMock()
    ws_mock.get_by_agent_id.return_value = None
    svc_mock = MagicMock()

    with (
        patch(WS_MANAGER_PATH, return_value=ws_mock),
        patch(SERVICE_PATH, return_value=svc_mock),
        patch(MAILBOX_FACTORY_PATH, return_value=MagicMock()),
    ):
        ok = _send_to_daemon("agent-ghost", {"event": "sprite_progress"})

    assert ok is False
    svc_mock.get_town_agent_by_id.assert_not_called()


def test_cache_fields_default_to_safe_values():
    """Fresh `AgentConnection` has `onboarding_phase='alive'` and
    `current_conversation_id=''` — so `_send_to_daemon` against an
    agent that was registered but never had its cache explicitly
    seeded still makes sensible decisions."""
    from core.services.town_agent_ws import AgentConnection

    conn = AgentConnection(
        connection_id="c",
        user_id="u",
        agent_name="a",
        agent_id="id",
        instance_id="i",
    )
    assert conn.onboarding_phase == "alive"
    assert conn.current_conversation_id == ""


@pytest.mark.parametrize(
    "updater,arg,expected_field",
    [
        ("update_onboarding_phase", "static", "onboarding_phase"),
        ("update_conversation", "conv-xyz", "current_conversation_id"),
    ],
)
def test_ws_manager_mutators_update_registered_agents(updater, arg, expected_field):
    """`TownAgentWsManager.update_onboarding_phase` and `update_conversation`
    should write the cached fields on the registered AgentConnection."""
    from core.services.town_agent_ws import TownAgentWsManager

    mgr = TownAgentWsManager()
    mgr.register(
        connection_id="c-1",
        user_id="u",
        agent_name="agent-name",
        agent_id="agent-id",
        instance_id="i",
    )
    getattr(mgr, updater)("agent-id", arg)
    conn = mgr.get_by_agent_id("agent-id")
    assert getattr(conn, expected_field) == arg


def test_ws_manager_mutators_are_noop_for_unknown_agents():
    """Updater methods should not crash when the agent isn't registered."""
    from core.services.town_agent_ws import TownAgentWsManager

    mgr = TownAgentWsManager()
    mgr.update_onboarding_phase("ghost", "static")  # no-op
    mgr.update_conversation("ghost", "conv-1")  # no-op
    assert mgr.get_by_agent_id("ghost") is None
