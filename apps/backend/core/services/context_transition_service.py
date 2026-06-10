"""Town ↔ apartment ↔ library context transitions.

Two kinds of movement exist in GooseTown:

* **Intra-context moves** — within town, within the apartment, or
  within the library. The game server's NavigationAgent2D pathfinds
  between markers.
* **Cross-context transitions** — town → apartment (``residence`` door),
  apartment → town (``exit`` door), town → library (``library`` door),
  or library → town (``exit`` door). These are expensive: the agent has
  to be removed from one map's physics/navmesh and spawned into the
  other atomically, DynamoDB has to flip the ``locationContext``, and
  the ws manager's in-memory cache has to match. This module owns the
  whole protocol.

Extracted from ``routers/town.py`` as sub-project #3's sixth slice
(2026-04-23). Library context added 2026-04-29.

Three pieces live here:

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

from core.services.daemon_mailbox import get_daemon_mailbox
from core.services.game_server_client import get_game_server_client
from core.services.town_agent_ws import get_town_agent_ws_manager
from core.services.town_service import TownService

logger = logging.getLogger(__name__)


# Cross-context transition table.
# Keyed by (source_context, destination_marker). The destination marker is
# the Marker2D name on the source map that triggers the transition. The
# resulting dest_location is the marker name in the destination map where
# the agent will spawn.
#
# Note that "exit" is overloaded — it's the apartment-exit AND the
# library-exit marker. Using (source, marker) as the key disambiguates.
_TRANSITION_TABLE: dict[tuple[str, str], tuple[str, str]] = {
    # (source_context, destination_marker): (dest_context, dest_location)
    ("town", "residence"): ("apartment", "exit"),
    ("apartment", "exit"): ("town", "residence"),
    ("town", "library"): ("library", "spawn"),
    ("library", "exit"): ("town", "library"),
}

# Backward-compatible flat lookup: destination marker name → True.
# Used by router code to quickly check "is this a transition trigger?"
# without knowing the source context. Source-context disambiguation
# happens inside ``_handle_context_transition``.
_TRANSITION_MAP: frozenset[str] = frozenset({marker for (_src, marker) in _TRANSITION_TABLE.keys()})

# Entry markers can only be entered from town. Maps the marker to the interior
# context it lands you in, so we can tell "genuinely already there" apart from
# "in a *different* interior" (which must exit to town first).
_ENTRY_MARKER_CONTEXT: dict[str, str] = {"library": "library", "residence": "apartment"}


def _transition_path(current_context: str, destination: str) -> list[str]:
    """Return the sequence of transition markers to reach ``destination``.

    Entering an interior (``library``/``residence``) is only valid from town, so
    if the agent is in a *different* interior it must exit first — this returns
    ``["exit", destination]`` (two-hop). Entering from town, being genuinely in
    the target interior already, or an ``exit`` itself are each a single step
    (the genuine already-there case then rejects with ``already_in_X``). This is
    what makes a single ``move <interior>`` verb work from anywhere, matching how
    town-location moves already auto-exit the current interior.
    """
    target_ctx = _ENTRY_MARKER_CONTEXT.get(destination)
    if target_ctx is not None and current_context not in ("town", target_ctx):
        return ["exit", destination]
    return [destination]


# Town-map markers that are NOT cross-context transition triggers — i.e.,
# regular intra-town navigation targets. Used by the move handler to decide
# whether a cross-context transition needs to happen before the agent can
# navigate to a given location.
_TOWN_LOCATIONS: set[str] = {
    "plaza",
    "cafe",
    "activity_center",
    "bank",
    "residence",
    "player_spawn",
}


def _context_for_location(location: str) -> str | None:
    """Return the context (``"town"``, ``"apartment"``, ``"library"``) a destination lives in.

    Used by the move handler to decide whether a cross-context transition
    is needed. ``None`` is returned for unknown destinations.

    Note: ``"exit"`` and ``"bookshelf"`` are ambiguous (they exist in both
    apartment and library). This function returns the most common
    interpretation; callers that need disambiguation by source context
    should use ``_TRANSITION_TABLE`` directly.
    """
    if location in _TOWN_LOCATIONS:
        return "town"
    # Library spots first (more specific names like reading_chair_1 won't
    # collide with apartment).
    try:
        from core.services.activity_registry import LIBRARY_SPOT_NAMES
    except Exception:
        LIBRARY_SPOT_NAMES = frozenset()
    if location in LIBRARY_SPOT_NAMES:
        return "library"
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
    mailbox = get_daemon_mailbox()
    service = TownService()
    ws_manager = get_town_agent_ws_manager()
    gs_client = get_game_server_client()
    agent_id = agent_conn.agent_id

    logger.info("[agent:%s] Transition START: %s (conn=%s)", agent_id[:8], destination, connection_id[:12])

    def _reject(reason: str) -> None:
        logger.warning("[agent:%s] Transition REJECTED: %s → %s", agent_id[:8], destination, reason)
        mailbox.reject(connection_id, "move_rejected", reason, destination=destination)

    # --- Load agent from DB ---
    agent = service.get_town_agent_by_id(agent_id)
    if not agent:
        _reject("agent_not_found")
        return False

    # --- Validate context: lookup the (source, destination) transition ---
    current_context = agent.get("locationContext", "town")
    key = (current_context, destination)
    if key not in _TRANSITION_TABLE:
        # No transition for (current_context, destination). Pick a reason that
        # reflects the ACTUAL context — only claim ``already_in_X`` when the
        # agent is genuinely in that interior; otherwise it's an impossible hop
        # (e.g. apartment → library, which the router resolves via a two-step
        # exit-then-enter, so reaching here means a genuine dead-end).
        target_ctx = _ENTRY_MARKER_CONTEXT.get(destination)
        if target_ctx is not None and current_context == target_ctx:
            reason = f"already_in_{target_ctx}"
        elif destination == "exit":
            reason = "not_in_interior"
        else:
            reason = f"cannot_transition_{current_context}_to_{destination}"
        _reject(reason)
        return False
    dest_context, dest_location = _TRANSITION_TABLE[key]

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
    mailbox.notify_connection(
        connection_id,
        {
            "event": "context_changed",
            "context": dest_context,
            "location": dest_location,
        },
    )

    # Notify browser viewers so they switch scenes
    try:
        from core.services.viewer_relay import get_viewer_relay

        relay = get_viewer_relay()
        for viewer_conn_id in list(relay._viewer_connections):
            mailbox.notify_connection(
                viewer_conn_id,
                {
                    "event": "agent_context_changed",
                    "agent_id": agent_id,
                    "context": dest_context,
                    "userId": agent_conn.user_id,
                },
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
