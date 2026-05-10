"""Bridge between the game server, mood engine, and agent daemons.

Three helpers that together form the backend's "push state to the daemon"
surface. Lifted out of ``routers/town.py`` so the extracted service modules
(``activity_service``, ``conversation_service``, ``sprite_pipeline``) can
depend on them without the inverted ``service -> routers`` import that the
earlier slices had to defer.

* ``_send_to_daemon`` — fan-out helper that pushes a payload to an agent's
  daemon via the in-memory ``TownAgentWsManager`` and API Gateway Management
  API. Hot-path; reads ``onboarding_phase`` and ``current_conversation_id``
  from the cached ``AgentConnection`` so the every-2s ``agent_state`` tick
  does not round-trip DynamoDB.

* ``_apply_event_with_sleep_guard`` — mood/energy event application with
  server-enforced sleep on exhaustion. Closes the WebSocket when an agent
  hits energy=0 so the LLM does not need to cooperate to sleep.

* ``_make_agent_event_callback`` — builds the per-agent callback that the
  game-server client invokes on events (``arrived``, ``nearby``,
  ``agent_state``, ...). Updates ``currentLocation`` on arrival and
  forwards every event through ``_send_to_daemon``.

No re-exports or wrappers live here — callers import directly from this
module. ``routers/town.py`` does keep an alias-import block so call sites
that still reference ``routers.town._send_to_daemon`` (integration tests)
continue to resolve to the same function object.
"""

from __future__ import annotations

import logging

from core.services import town_mood_engine
from core.services.daemon_mailbox import get_daemon_mailbox
from core.services.town_agent_ws import get_town_agent_ws_manager
from core.services.town_mood_engine import parse_mood
from core.services.town_service import TownService

logger = logging.getLogger(__name__)


def _make_agent_event_callback():
    """Create a per-agent callback that forwards game server events to the daemon.

    Uses the in-memory ws_manager for connection lookup (seeded from
    DynamoDB's ``daemonConnectionId`` at register / startup time).
    Also updates DynamoDB ``currentLocation`` on ``arrived`` events.
    """

    async def _on_game_server_event(agent_id: str, event_data: dict) -> None:
        evt_type = event_data.get("event", "")

        # Update DynamoDB location on arrival
        if evt_type == "arrived":
            location = event_data.get("location", "")
            if location:
                try:
                    TownService().update_agent_state(agent_id, current_location=location)
                except Exception:
                    logger.warning("Failed to update location for %s", agent_id)

        # Forward event to daemon via in-memory connection lookup
        _send_to_daemon(agent_id, event_data)

    return _on_game_server_event


def _send_to_daemon(agent_id: str, payload: dict) -> bool:
    """Send a message to an agent's daemon via the in-memory ws_manager.

    Pre-optimization this did ``TownService.get_town_agent_by_id`` on every
    call — roughly once every 2 seconds per connected agent, since the
    game server pushes ``agent_state`` on that cadence. That's a lot of
    wasted DynamoDB traffic for a field (``daemonConnectionId``) that only
    changes on reconnect.

    After this change:
    * ``daemonConnectionId`` comes from ``AgentConnection.connection_id``
      (tracked by ``TownAgentWsManager`` since the ``town_agent_connect``
      handler runs).
    * The two enrichment fields ``agent_state`` needs (onboarding_phase +
      currentConversationId) also live on ``AgentConnection`` and are
      maintained by the handful of lifecycle events that actually change
      them. ``get_nearby_conversations`` still needs a DB hit (conversation
      + participant records aren't cached) but we use the cached
      ``current_conversation_id`` for the "is this agent in a conv?" gate
      so it's one DB call, not N+1.

    If the agent isn't in the ws_manager registry there's no daemon
    connection to send to — return False immediately rather than falling
    back to a DynamoDB lookup that would resolve to the same answer at
    higher cost.
    """
    evt = payload.get("event", payload.get("type", "?"))

    ws_manager = get_town_agent_ws_manager()
    agent_conn = ws_manager.get_by_agent_id(agent_id)
    if not agent_conn:
        logger.warning(
            "[agent:%s] _send_to_daemon: agent not in ws_manager (event=%s)",
            agent_id[:8],
            evt,
        )
        return False

    conn_id = agent_conn.connection_id
    if not conn_id:
        logger.warning(
            "[agent:%s] _send_to_daemon: empty connection_id (event=%s)",
            agent_id[:8],
            evt,
        )
        return False

    # Enrich agent_state events with the two fields the daemon needs.
    # Both come from cached AgentConnection now; no DB read in the common case.
    if evt == "agent_state":
        payload = {
            **payload,
            "onboardingPhase": agent_conn.onboarding_phase or "alive",
        }
        nearby_agents = payload.get("nearby_agents", [])
        nearby_ids = [a.get("agent_id") for a in nearby_agents if a.get("agent_id")]

        # Inject currentGoal into each nearby-agent dict (always, regardless of
        # whether this agent is in a conversation).  Goals are fetched once per
        # 2-second agent_state tick; N is typically ≤ 3 agents in proximity.
        if nearby_ids:
            svc = TownService()
            goal_map: dict[str, str | None] = {}
            for nid in nearby_ids:
                rec = svc.get_town_agent_by_id(nid)
                goal_map[nid] = (rec.get("currentGoal") or None) if rec else None
            enriched_nearby = [{**a, "currentGoal": goal_map.get(a.get("agent_id"))} for a in nearby_agents]
            payload = {**payload, "nearby_agents": enriched_nearby}

            # Only fetch nearby conversations for agents not already in one.
            own_conv = agent_conn.current_conversation_id
            if not own_conv:
                nearby_convs = svc.get_nearby_conversations(nearby_ids)
                if nearby_convs:
                    payload = {**payload, "nearby_conversations": nearby_convs}

    sent = get_daemon_mailbox().notify_connection(conn_id, payload)
    if not sent:
        logger.warning(
            "[agent:%s] _send_to_daemon: connection gone %s (event=%s)",
            agent_id[:8],
            conn_id[:12],
            evt,
        )
    return sent


def _apply_event_with_sleep_guard(
    service: TownService,
    agent_id: str,
    agent: dict,
    event: str,
) -> tuple[int, int, bool]:
    """Apply a mood/energy event with server-enforced sleep on exhaustion.

    If the event would drop energy to 0, immediately apply the ``sleep`` event
    (energy→100), mark the agent as sleeping, and close their WebSocket. The
    agent's daemon reconnects when the user decides, waking fully rested.

    This prevents the "zombie active at energy=0" state without requiring the
    agent's LLM to cooperate — the server unilaterally sleeps them.

    Returns:
        ``(new_energy, new_mood, slept)`` — slept=True means energy was
        refilled and the WS was closed.
    """
    current_energy = int(agent.get("energy", 100) or 0)
    current_mood = parse_mood(agent.get("mood"))
    traits = agent.get("traits", "")

    new_energy, new_mood = town_mood_engine.apply_event(event, current_energy, current_mood, traits)

    if new_energy > 0:
        service.update_agent_mood_energy(agent_id, new_mood, new_energy)
        return new_energy, new_mood, False

    # Exhausted. Server-enforce sleep: energy→100, mark sleeping, close WS.
    new_energy, new_mood = town_mood_engine.apply_event("sleep", 0, new_mood, traits)
    service.update_agent_mood_energy(agent_id, new_mood, new_energy)
    try:
        service.update_agent_activity(agent_id, "sleeping", None)
    except Exception as exc:
        logger.warning("Failed to mark agent %s sleeping: %s", agent_id, exc)

    ws_manager = get_town_agent_ws_manager()
    conn = ws_manager.get_by_agent_id(agent_id)
    if conn:
        mailbox = get_daemon_mailbox()
        mailbox.notify_connection(
            conn.connection_id,
            {
                "event": "forced_sleep",
                "reason": "exhausted",
                "new_energy": new_energy,
                "message": (
                    "You exhausted yourself. The server has put you to "
                    "sleep and is closing your connection. Reconnect "
                    "whenever you want to wake up refreshed."
                ),
            },
        )
        mailbox.disconnect(conn.connection_id)
    logger.info(
        "[agent:%s] Auto-slept after %s dropped energy to 0 (reset to %d)",
        agent_id[:8],
        event,
        new_energy,
    )
    return new_energy, new_mood, True
