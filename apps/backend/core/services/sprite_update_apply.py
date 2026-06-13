"""Backend-local effects of a sprite change made by the sprite worker.

The worker owns all DynamoDB writes (spriteStatus / spriteUrl /
spriteAnimations / onboardingPhase). What it CANNOT touch is backend-process
state: the per-agent game-server WebSocket connections, the in-memory
ws_manager registry, and the phase-lattice cache mirror. This module is the
single place those effects happen — called by the internal
``/town/internal/sprite-event`` endpoint (worker → backend) and usable
in-process by pipeline code that still runs inside the backend.

Version-helper locations
------------------------
* ``versioned_sprite_url``    — ``core.services.town_service``
* ``_version_animation_urls`` — ``core.services.agent_connect_service``

Neither helper creates an import cycle from this module:

    sprite_update_apply → agent_connect_service (no route/router import)
    sprite_update_apply → town_service           (no route/router import)

Versioned-vs-unversioned divergence note
-----------------------------------------
``sprite_pipeline.py`` Phase C currently passes **bare (unversioned)** URLs
to ``gs_client.update_agent_sprites`` (the URL is built directly as
``f"{settings.SPRITE_CDN_URL}/{s3_key}"`` with no ``?v=`` suffix).
This module applies versioning (via ``?v=<spriteUpdatedAt>``) for
cache-busting consistency with the ``agent_connect_service`` path — both
``connect_agent`` and ``update_agent_sprites`` receive versioned URLs here.
The sprite_pipeline.py divergence is a known inconsistency; the next
pipeline sweep should migrate it to use this module for all gs pushes.
"""

from __future__ import annotations

import logging

from core.services.agent_connect_service import _version_animation_urls
from core.services.daemon_bridge import _make_agent_event_callback, _send_to_daemon
from core.services.game_server_client import get_game_server_client
from core.services.phase_lattice import restore_phase_from_db
from core.services.town_agent_ws import get_town_agent_ws_manager
from core.services.town_service import TownService, versioned_sprite_url

logger = logging.getLogger(__name__)


async def apply_sprite_update(agent_id: str, kind: str) -> None:
    """Refresh local caches and push the agent's current sprites to the game server.

    Parameters
    ----------
    agent_id:
        UUID of the agent whose sprites have changed.
    kind:
        ``"static_ready"``      — Phase B done; static walk spritesheet is ready.
        ``"animations_updated"``— Walk or a lazy animation landed (Phase C).

    Both kinds re-read DynamoDB as the source of truth for sprite URLs and
    animations; the worker is assumed to have already written the final values.

    Side-effects
    ------------
    * Calls ``restore_phase_from_db`` to sync the in-memory phase-lattice cache.
    * If the agent is already connected to the game server → calls
      ``update_agent_sprites`` (versioned URLs).
    * If the agent is NOT in the game server but has a live daemon connection →
      calls ``connect_agent`` to (re-)spawn the agent with the fresh sprites.
    * If ``kind == "animations_updated"`` and the agent has a daemon connection
      → sends a ``sprite_ready`` event to the daemon so it can update
      ``TOWN_STATUS.md``.
    * Game-server errors are caught and logged; they do NOT propagate so that a
      transient GS hiccup never fails the worker's HTTP call.
    """
    service = TownService()
    agent = service.get_town_agent_by_id(agent_id)
    if not agent:
        logger.warning("apply_sprite_update: agent %s not found in DynamoDB", agent_id[:8])
        return

    # Sync the in-memory phase-lattice mirror from the DB row.
    restore_phase_from_db(agent_id, agent.get("onboardingPhase") or "alive")

    ws_manager = get_town_agent_ws_manager()
    conn = ws_manager.get_by_agent_id(agent_id)
    gs_client = get_game_server_client()

    # Build cache-busted URLs for the game server (consistent with agent_connect_service).
    sprite_url = versioned_sprite_url(agent.get("spriteUrl") or "", agent)
    animations = _version_animation_urls(agent.get("spriteAnimations") or {}, agent)

    try:
        if agent_id in gs_client._agent_connections:
            # Agent is already on the game server — push the new sprite data.
            await gs_client.update_agent_sprites(agent_id, animations)
        elif conn:
            # Agent has a daemon connection but is not yet on the game server
            # (e.g. the backend was restarted, or the static sprite just landed
            # and the agent has never been spawned).  Spawn it now.
            await gs_client.connect_agent(
                agent_id=agent_id,
                display_name=agent.get("displayName", ""),
                sprite_url=sprite_url,
                location=agent.get("currentLocation", "plaza"),
                context=conn.location_context or "town",
                user_id=conn.user_id,
                sprite_animations=animations,
                on_event=_make_agent_event_callback(),
            )
    except Exception:
        logger.exception("apply_sprite_update: game-server push failed for agent %s", agent_id[:8])

    # For animation updates, notify the daemon so it can refresh TOWN_STATUS.md.
    if kind == "animations_updated" and conn:
        _send_to_daemon(
            agent_id,
            {
                "event": "sprite_ready",
                "agent_id": agent_id,
                "sprite_url": agent.get("spriteUrl", ""),
            },
        )
