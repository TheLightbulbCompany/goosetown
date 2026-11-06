"""Agent connect lifecycle: token verification, connection handoff, sleep grace.

Extracted from ``routers/town.py`` as the fifth sub-project #3 slice. Owns
the end-to-end connect/disconnect state machine for agent daemons:

* ``_handle_agent_connect`` — the business logic behind the
  ``town_agent_connect`` WebSocket message. Verifies the town_token, kicks
  any stale connection for the same agent, registers the new connection
  in ``ws_manager``, persists ``daemonConnectionId`` to DynamoDB, decides
  where to spawn the agent (sleep-grace reconnect vs fresh wake-in-bed),
  connects to the game server, and sends the ``connected`` event back to
  the daemon via the API Gateway Management API.

* Sleep-grace window (``_SLEEP_GRACE_SECONDS``, ``_pending_sleep_tasks``,
  ``_delayed_sleep``, ``_schedule_sleep_grace``, ``_cancel_sleep_grace``).
  A daemon disconnect doesn't always mean "agent went to sleep" — it
  might be a brief network flap with an auto-reconnect. On non-clean
  disconnects, ``handle_ws_disconnect`` (in ``routers/town.py``) schedules
  a delayed move-to-bed; if the daemon reconnects within the grace
  window, ``_handle_agent_connect`` cancels the task and resumes the
  agent at their last-known location.

The FastAPI route handler ``handle_ws_disconnect`` stays in
``routers/town.py`` (HTTP binding is a router concern) but delegates to
``_schedule_sleep_grace`` imported from this module.

In-memory grace state only — a backend restart during a pending grace
drops the task. The window is narrow enough (seconds, not minutes) that
durable scheduling hasn't been needed.
"""

from __future__ import annotations

import asyncio
import logging

from core.services.daemon_bridge import _make_agent_event_callback
from core.services.game_server_client import get_game_server_client
from core.services.town_service import TownService, versioned_sprite_url

logger = logging.getLogger(__name__)


def _version_animation_urls(sprite_animations: dict, agent: dict) -> dict:
    """Return a copy of ``spriteAnimations`` with each entry's ``url`` cache-busted.

    ``spriteAnimations`` maps animation name -> {"url", "frameCount", "fps"}.
    The stored URLs are bare CDN URLs (1-year cache, stable key), so a
    regenerated animation sheet (e.g. a re-stripped talking.png) is invisible
    until the URL string changes. Append the same ``?v=<spriteUpdatedAt>`` the
    walk URL gets so the town viewer fetches the fresh bytes. Non-dict entries
    or entries without a ``url`` pass through untouched.
    """
    if not sprite_animations:
        return sprite_animations
    versioned: dict = {}
    for name, meta in sprite_animations.items():
        if isinstance(meta, dict) and meta.get("url"):
            versioned[name] = {**meta, "url": versioned_sprite_url(meta["url"], agent)}
        else:
            versioned[name] = meta
    return versioned


# ---------------------------------------------------------------------------
# Sleep grace window
# ---------------------------------------------------------------------------

_SLEEP_GRACE_SECONDS: float = 90.0
_pending_sleep_tasks: dict[str, asyncio.Task] = {}


async def _delayed_sleep(agent_id: str) -> None:
    """Wait the grace window, then move the agent to apartment/bed."""
    try:
        await asyncio.sleep(_SLEEP_GRACE_SECONDS)
    except asyncio.CancelledError:
        logger.info("[agent:%s] Sleep grace cancelled (daemon reconnected)", agent_id[:8])
        raise

    try:
        service = TownService()
        agent = service.get_town_agent_by_id(agent_id)
        if not agent:
            return
        # Defensive: a reconnect that didn't go through _cancel_sleep_grace
        # (e.g. raced past the await) could leave a populated connection ID.
        # In that case, the agent is back — don't move them.
        if agent.get("daemonConnectionId"):
            logger.info(
                "[agent:%s] Sleep grace expired but agent is reconnected — skipping move-to-bed",
                agent_id[:8],
            )
            return
        logger.info("[agent:%s] Sleep grace expired — moving to apartment/bed", agent_id[:8])
        service.update_agent_context(agent_id, "apartment", "bed")
        # Free the per-user quota slot — see the matching comment in
        # ``handle_ws_disconnect`` (clean-leave path). Re-register flips
        # this back to "true" via ``opt_in``.
        service.set_agent_active(agent_id, False)
    finally:
        # Clear the registry entry in the SAME task that owns it, whether we
        # moved the agent or not. Popping on cancel happens in the caller.
        _pending_sleep_tasks.pop(agent_id, None)


def _schedule_sleep_grace(agent_id: str) -> None:
    """Schedule a delayed move-to-bed for a disconnected agent.
    Cancels any existing pending task for the same agent first (last-writer-wins)."""
    existing = _pending_sleep_tasks.get(agent_id)
    if existing and not existing.done():
        existing.cancel()
    _pending_sleep_tasks[agent_id] = asyncio.create_task(_delayed_sleep(agent_id))


def _cancel_sleep_grace(agent_id: str) -> bool:
    """Cancel a pending sleep grace task. Returns True if one was active.

    A True return means "this reconnect happened within the grace window";
    the caller should preserve the agent's current location instead of
    re-initializing to apartment/bed.
    """
    task = _pending_sleep_tasks.pop(agent_id, None)
    if task and not task.done():
        task.cancel()
        return True
    return False


# ---------------------------------------------------------------------------
# town_agent_connect handler
# ---------------------------------------------------------------------------


async def _handle_agent_connect(request, ws_manager):
    """Process a town_agent_connect message: verify token, register connection, send connected event.

    ``request`` is a ``routers.town.WsMessageRequest`` — we keep the type
    loose here to avoid a router → service import cycle. The only fields
    read are ``body`` (dict) and ``connectionId`` (str).
    """
    from core.services.daemon_mailbox import get_daemon_mailbox
    from core.town_token import verify_town_token

    mailbox = get_daemon_mailbox()

    body = request.body
    token = body.get("token", "")
    agent_name = body.get("agent_name", "")

    if not token or not agent_name:
        logger.warning("Agent connect missing token or agent_name")
        return {"statusCode": 400, "error": "missing_token_or_agent_name"}

    # Verify town_token
    payload = verify_town_token(token)
    if not payload:
        logger.warning("Invalid town_token for agent connect: %s", agent_name)
        return {"statusCode": 401, "error": "invalid_token"}

    user_id = payload["uid"]
    instance_id = payload["iid"]

    # Look up the agent
    service = TownService()
    agent = service.get_agent_by_name(user_id, agent_name)
    if not agent:
        logger.warning("Agent not found: %s for user %s", agent_name, user_id)
        return {"statusCode": 404, "error": "agent_not_found"}

    agent_id = agent["id"]

    # Kick any existing daemon connection for this agent. When a daemon
    # reconnects, the previous WebSocket may still be open on API Gateway
    # (e.g. the old daemon is still running locally). Leaving it alive
    # causes both to compete for messages — only the latest wins, the old
    # one starves and hits the inactivity timeout.
    existing_conn = ws_manager.get_by_agent_id(agent_id)
    if existing_conn and existing_conn.connection_id != request.connectionId:
        old_conn_id = existing_conn.connection_id
        logger.info(
            "[agent:%s] Kicking old connection %s (new: %s)",
            agent_id[:8],
            old_conn_id[:12],
            request.connectionId[:12],
        )
        # DaemonMailbox.disconnect handles the GoneException + log-warn
        # pattern that used to live inline here.
        mailbox.disconnect(old_conn_id)

    # Register connection in ws_manager (replaces old registration in-memory)
    ws_manager.register(
        connection_id=request.connectionId,
        user_id=user_id,
        agent_name=agent_name,
        agent_id=agent_id,
        instance_id=instance_id,
    )

    # Persist the daemon's connection ID to DynamoDB so it survives restarts
    logger.info("[agent:%s] daemonConnectionId updated: %s", agent_id[:8], request.connectionId[:12])
    service.update_agent_state(agent_id, daemon_connection_id=request.connectionId)

    # Sleep-grace cancellation: if this daemon dropped recently and is now
    # reconnecting inside the grace window, `_cancel_sleep_grace` returns
    # True. In that case preserve the agent's last-known location instead of
    # the default "wake up at home" reset — a 9-second flap shouldn't
    # teleport them back to bed. For fresh connects or reconnects after the
    # grace expired, no task is pending and we fall through to the default.
    within_grace = _cancel_sleep_grace(agent_id)

    # Also preserve location for a reconnect of an agent that is STILL active in
    # the world (isActive + phase "alive"). This is the backend-drop recovery
    # path: a new backend task lost its in-memory ws_manager entry (e.g. after a
    # deploy) and told the daemon to reconnect — there's no sleep-grace task, but
    # the agent never slept, so it must NOT be teleported home from wherever it
    # was (e.g. the bank). Fresh connects (phase != "alive") and genuine wakes
    # (isActive == "false") still fall through to the wake-in-bed reset.
    active_in_world = str(agent.get("isActive", "")).lower() == "true" and agent.get("onboardingPhase") == "alive"

    if within_grace or active_in_world:
        current_location = agent.get("currentLocation") or "bed"
        location_context = agent.get("locationContext") or "apartment"
        logger.info(
            "[agent:%s] Reconnect (%s) — resuming at %s/%s",
            agent_id[:8],
            "sleep grace" if within_grace else "still active",
            location_context,
            current_location,
        )
    else:
        # Fresh connect or grace expired — wake up in bed, walk themselves
        # out from there.
        location_context = "apartment"
        current_location = "bed"
        service.update_agent_context(agent_id, location_context, current_location)

    # Set in-memory context on the connection record
    ws_manager.update_context(agent_id, location_context)

    # Seed the hot-path cache on AgentConnection so `_send_to_daemon` doesn't
    # re-read these fields from DynamoDB every 2s agent_state broadcast.
    # Source of truth is still DynamoDB; this mirror is maintained at the
    # handful of lifecycle events that change either field. The phase
    # mirror goes through ``restore_phase_from_db`` (NOT the monotonic
    # ``advance_phase``) — DDB is the source of truth on connect, even if
    # the cache happened to be warm with a different value from a prior
    # session.
    from core.services.phase_lattice import restore_phase_from_db

    restore_phase_from_db(agent_id, agent.get("onboardingPhase") or "alive")
    ws_manager.update_conversation(agent_id, agent.get("currentConversationId") or "")

    # Connect agent to game server (shadow sprite fallback if no custom sprite).
    # Cache-bust both the walk URL and every animation URL with ?v=<spriteUpdatedAt>
    # (same helper the /apartment path uses). Sprite objects carry a 1-year
    # Cache-Control at a stable key, so a regenerated sprite is invisible in town
    # until the URL string changes — versioning makes regen (and any future
    # sprite update) show up immediately, with no CloudFront invalidation.
    gs_client = get_game_server_client()
    try:
        await gs_client.connect_agent(
            agent_id=agent_id,
            display_name=agent.get("displayName", agent_name),
            sprite_url=versioned_sprite_url(agent.get("spriteUrl") or "", agent),
            location=current_location,
            context=location_context,
            user_id=user_id,
            sprite_animations=_version_animation_urls(agent.get("spriteAnimations", {}), agent),
            on_event=_make_agent_event_callback(),
        )
        logger.info(
            "Agent %s connected to game server (context=%s, location=%s, userId=%s)",
            agent_name,
            location_context,
            current_location,
            user_id[:12],
        )

    except Exception as e:
        logger.warning("Failed to connect agent %s to game server: %s", agent_name, e)

    # ── Auto-kick the sprite pipeline for agents that need one ─────────
    # The /town/agent/register HTTP path enqueues sprite generation right
    # after service.opt_in writes the row. Agents whose rows were created
    # out-of-band — today that's residents via town_service.create_resident
    # called from the WorldGM service — never went through that endpoint,
    # so without this fallback they'd connect with no sprite and stay
    # permanent placeholders. Durability and dedup now come from SQS +
    # the DynamoDB conditional claim in sprite_jobs.enqueue_signup. The
    # dedicated SQS sprite worker drains the queue; the backend only produces.
    appearance = agent.get("appearance")
    if (
        appearance
        and not agent.get("spriteReady")
        and agent.get("spriteStatus") is None
        and not agent.get("pixellabCharacterId")
    ):
        from core.services import sprite_jobs

        queued = sprite_jobs.enqueue_signup(agent_id, agent.get("displayName", agent_name), appearance)
        logger.info("[agent:%s] auto-enqueued sprite pipeline (queued=%s)", agent_id[:8], queued)

    # Send connected event back to the agent. Mailbox owns the
    # try/except + log-warn pattern that used to wrap this block.
    delivered = mailbox.notify_connection(
        request.connectionId,
        {
            "event": "connected",
            "agent": {
                "id": agent_id,
                "name": agent.get("displayName", agent_name),
                "location": current_location,
                "locationContext": location_context,
                "mood": agent.get("mood", 0),
                "energy": agent.get("energy", 100),
                "spriteReady": agent.get("spriteReady", False),
            },
        },
    )
    if delivered:
        logger.info("Sent connected event to agent %s", agent_name)
    else:
        # Mailbox already logged the WARNING with the underlying cause; this
        # ERROR-level line preserves the original log signal for the
        # successful-but-undelivered case (gone connection mid-handshake).
        logger.error("Connected event to %s was not delivered", agent_name)

    # Exchange resync: re-send any AWARDED (this agent is the provider) or
    # DELIVERED (this agent is the consumer) job events that were lost while
    # the daemon was offline or its connection was stale. Award/deliver
    # notifies are fire-and-forget, and AWARDED jobs aren't on the public
    # board — without this, a winner that missed `job_awarded` silently eats
    # the delivery-deadline failure. Best-effort: resync_agent_jobs swallows
    # its own exceptions, and the lazy import keeps connect resilient even
    # if the exchange module itself fails to load.
    try:
        from core.services import exchange_service

        exchange_service.resync_agent_jobs(agent_id)
    except Exception as _exc:
        logger.warning(
            "[agent:%s] Exchange resync on connect failed (non-fatal): %s",
            agent_id[:8],
            _exc,
        )

    # Fire-and-forget SQS drain: deliver any WorldGM events that arrived while
    # the daemon was offline. Runs in a background Task so the connect handshake
    # is not delayed. If the queue doesn't exist (older agents registered before
    # Phase D) the drainer no-ops safely.
    try:
        from core.services import sqs_drainer

        asyncio.create_task(sqs_drainer.drain_for_agent(agent_id))
        logger.debug("[agent:%s] SQS drain task scheduled on connect", agent_id[:8])
    except Exception as _sqse:
        logger.warning(
            "[agent:%s] Failed to schedule SQS drain on connect (non-fatal): %s",
            agent_id[:8],
            _sqse,
        )

    return {"statusCode": 200}
