"""Town ↔ apartment context transitions.

Two kinds of movement exist in GooseTown:

* **Intra-context moves** — within town or within the apartment. The
  game server's NavigationAgent2D pathfinds between markers.
* **Cross-context transitions** — town → apartment (``residence`` door)
  or apartment → town (``exit`` door). These are expensive: the agent
  has to be removed from one map's physics/navmesh and spawned into the
  other atomically, DynamoDB has to flip the ``locationContext``, and
  the ws manager's in-memory cache has to match. This module owns the
  whole protocol.

Extracted from ``routers/town.py`` as sub-project #3's sixth slice
(2026-04-23). Three pieces live here:

* Module constants ``_TRANSITION_MAP`` and ``_TOWN_LOCATIONS`` — the
  metadata move handlers consult to decide whether a cross-context
  transition is needed.
* ``_context_for_location(location)`` — pure helper: given a
  destination name, returns ``"town"``, ``"apartment"``, or ``None``
  if the location isn't known. Used by the move dispatch ladder in
  ``routers.town`` to pick the right transition before starting
  intra-context navigation.
* ``_handle_context_transition(connection_id, destination, agent_conn)``
  — the full protocol: validate context + busy state, flip to
  ``"transitioning"`` (blocks concurrent commands), ask the game server
  to transition atomically, commit or roll back DynamoDB + ws manager,
  notify the daemon with ``context_changed`` or ``move_rejected``, and
  broadcast ``agent_context_changed`` to viewer WebSockets so they
  switch scenes.
* ``_transition_to_apartment`` — deprecated shim for older tests.
"""

from __future__ import annotations

import asyncio
import logging

from core.services.game_server_client import get_game_server_client
from core.services.town_agent_ws import get_town_agent_ws_manager
from core.services.town_service import TownService

logger = logging.getLogger(__name__)


# Destination → expected source context and resulting (dest_context, dest_location)
_TRANSITION_MAP: dict[str, tuple[str, str, str]] = {
    # destination: (required_source_context, dest_context, dest_location)
    "residence": ("town", "apartment", "exit"),
    "exit": ("apartment", "town", "residence"),
}

# Which context each known destination belongs to. Used by the move handler
# to decide whether a cross-context transition needs to happen before the
# agent can navigate to a given location.
_TOWN_LOCATIONS: set[str] = {
    "plaza",
    "library",
    "cafe",
    "activity_center",
    "bank",
    "residence",
    "player_spawn",
}


def _context_for_location(location: str) -> str | None:
    """Return ``"town"`` or ``"apartment"`` for a known destination, else ``None``."""
    if location in _TOWN_LOCATIONS:
        return "town"
    try:
        from core.apartment_constants import APARTMENT_ROOMS
    except Exception:
        return None
    if location in APARTMENT_ROOMS or location == "exit":
        return "apartment"
    return None


async def _handle_context_transition(
    connection_id: str,
    destination: str,
    agent_conn,
) -> bool:
    """Handle a context-crossing move command (``residence`` or ``exit``).

    Two-phase protocol:
    1. Validate source context and busy state.
    2. Set ``locationContext: "transitioning"`` in DynamoDB and in-memory
       ws manager to block concurrent commands.
    3. Send ``transition_agent`` to the game server (handles remove+spawn
       atomically for both directions).
    4. On success: update DynamoDB to the destination context/location,
       update ws manager, push ``context_changed`` to the daemon.
    5. On failure or timeout: roll back DynamoDB and ws manager to the
       original context, push ``move_rejected`` to the daemon.

    Args:
        connection_id: API Gateway connection ID for the acting agent.
        destination: ``"residence"`` (enter apartment) or ``"exit"`` (return
            to town).
        agent_conn: Connection record from ``TownAgentWsManager``.
    """
    from core.services.management_api_client import ManagementApiClient

    mgmt = ManagementApiClient()
    service = TownService()
    ws_manager = get_town_agent_ws_manager()
    gs_client = get_game_server_client()
    agent_id = agent_conn.agent_id

    logger.info("[agent:%s] Transition START: %s (conn=%s)", agent_id[:8], destination, connection_id[:12])

    def _reject(reason: str) -> None:
        logger.warning("[agent:%s] Transition REJECTED: %s → %s", agent_id[:8], destination, reason)
        try:
            mgmt.send_message(
                connection_id,
                {"event": "move_rejected", "destination": destination, "reason": reason},
            )
        except Exception as exc:
            logger.warning("Failed to send move_rejected to %s: %s", agent_id, exc)

    # --- Load agent from DB ---
    agent = service.get_town_agent_by_id(agent_id)
    if not agent:
        _reject("agent_not_found")
        return False

    # --- Validate context: reject if already in the destination context ---
    required_source, dest_context, dest_location = _TRANSITION_MAP[destination]
    current_context = agent.get("locationContext", "town")
    if current_context != required_source:
        reason = "already_in_apartment" if destination == "residence" else "not_in_apartment"
        _reject(reason)
        return False

    # --- Validate not busy ---
    if agent.get("currentActivity"):
        _reject("busy")
        return False

    # --- Phase 1: Enter transitioning state ---
    transitioning = service.update_agent_context(agent_id, "transitioning", current_context)
    if not transitioning:
        logger.error("Failed to set transitioning state for agent %s", agent_id)
        _reject("internal_error")
        return False
    ws_manager.update_context(agent_id, "transitioning")

    # --- Phase 2: Instruct game server to move the agent across contexts ---
    agent_data = {
        "agent_id": agent_id,
        "name": agent.get("displayName", agent_conn.agent_name),
        "sprite_url": agent.get("spriteUrl", ""),
        "location": dest_location,
    }
    try:
        await asyncio.wait_for(
            gs_client.transition_agent(
                agent_id,
                to_context=dest_context,
                user_id=agent_conn.user_id,
                agent_data=agent_data,
            ),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Game server transition timed out for agent %s (%s → %s)",
            agent_id,
            current_context,
            dest_context,
        )
        service.update_agent_context(agent_id, current_context, agent.get("currentLocation", ""))
        ws_manager.update_context(agent_id, current_context)
        _reject("navigation_timeout")
        return False
    except Exception as exc:
        logger.warning("Game server transition failed for agent %s: %s", agent_id, exc)
        service.update_agent_context(agent_id, current_context, agent.get("currentLocation", ""))
        ws_manager.update_context(agent_id, current_context)
        _reject("navigation_error")
        return False

    # --- Phase 3: Commit destination state ---
    updated = service.update_agent_context(agent_id, dest_context, dest_location)
    if not updated:
        logger.error("update_agent_context returned None for agent %s after transition", agent_id)
        service.update_agent_context(agent_id, current_context, agent.get("currentLocation", ""))
        ws_manager.update_context(agent_id, current_context)
        _reject("internal_error")
        return False

    ws_manager.update_context(agent_id, dest_context)
    logger.info(
        "Context transition complete: agent %s %s → %s (location=%s)",
        agent_id,
        current_context,
        dest_context,
        dest_location,
    )

    # Notify the daemon
    try:
        mgmt.send_message(
            connection_id,
            {
                "event": "context_changed",
                "context": dest_context,
                "location": dest_location,
            },
        )
    except Exception as exc:
        logger.warning("Failed to send context_changed to %s: %s", agent_id, exc)

    # Notify browser viewers so they switch scenes
    try:
        from core.services.viewer_relay import get_viewer_relay

        relay = get_viewer_relay()
        for viewer_conn_id in list(relay._viewer_connections):
            try:
                mgmt.send_message(
                    viewer_conn_id,
                    {
                        "event": "agent_context_changed",
                        "agent_id": agent_id,
                        "context": dest_context,
                        "userId": agent_conn.user_id,
                    },
                )
            except Exception:
                # One stale viewer connection shouldn't stop notifying the
                # rest. Logging at debug since this is expected when a
                # viewer's WS dies between the snapshot and the send.
                logger.debug(
                    "Failed to notify viewer %s of context change for %s",
                    viewer_conn_id,
                    agent_id,
                    exc_info=True,
                )
    except Exception as exc:
        logger.debug("Failed to notify viewers of context change: %s", exc)

    return True


async def _transition_to_apartment(
    connection_id: str,
    agent_id: str,
    agent_conn,
) -> None:
    """Deprecated shim kept for backward-compatibility with existing tests.

    The transition logic has been consolidated into ``_handle_context_transition``.
    New code should call that function directly.

    Args:
        connection_id: API Gateway connection ID for the acting agent.
        agent_id: Internal agent UUID.
        agent_conn: Connection record from ``TownAgentWsManager``.
    """
    await _handle_context_transition(connection_id, "residence", agent_conn)
