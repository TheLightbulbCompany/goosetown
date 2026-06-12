"""Conversation lifecycle handlers — chat / say / join / end / finalize.

Extracted from ``routers/town.py`` as part of sub-project #3's service-layer
decomposition. Six handlers (~585 lines) moved here; router now contains
only the dispatch ladder plus a thin re-export for back-compat with any
external caller still importing `routers.town._handle_chat` etc.

The two external helpers used by these handlers:
* ``_apply_event_with_sleep_guard`` — imported from
  ``core.services.daemon_bridge`` (module-top).
* ``DaemonMailbox`` — fetched per-handler via ``get_daemon_mailbox()``
  to avoid touching boto3 / Settings at module-load time. Replaces the
  pre-Phase-2 pattern of constructing ``ManagementApiClient`` inside
  each handler. Audit Candidate 5.

``_MAX_CONVERSATION_TURNS`` constant comes along as its natural home.
The facing calculation that used to live here moved to the game server
in ``agent_controller.gd`` as part of the ``layout_conversation``
command — the server computes both the positioning and the resulting
facing as a single unit so they can't drift apart.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re

from core.services.daemon_bridge import _apply_event_with_sleep_guard
from core.services.game_server_client import get_game_server_client
from core.services.town_agent_ws import get_town_agent_ws_manager
from core.services.town_service import TownService

# UUID4 pattern — the only identifier safe to use as a chat target.
# Names and display names are not globally unique; two users can register
# agents with the same name. The UUID is the canonical cross-account identity.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

logger = logging.getLogger(__name__)


_MAX_CONVERSATION_TURNS = 20


def _maybe_enqueue_talking(agent_row: dict | None) -> None:
    """Lazy 'talking' generation on first conversation (fast/slow split,
    spec 2026-06-11). Gated by the same flag as activity animations so dev
    never generates against the shared PixelLab account."""
    from core.config import settings
    from core.services import sprite_jobs

    if not settings.ACTIVITY_ANIMATION_GENERATION or not agent_row:
        return
    if "talking" in (agent_row.get("spriteAnimations") or {}):
        return
    if not agent_row.get("pixellabCharacterId"):
        return
    sprite_jobs.enqueue_animation(agent_row["id"], "talking")


# Auto-end a conversation after this many seconds of silence (no new ``say``
# turn from any participant, no ``join_conversation`` from a new joiner).
# Default 180s: a two-turn exchange between local-model agents (~30-90s per
# think cycle) easily exceeds 60s of silence between turns, which the
# previous default would auto-close mid-flight. The timer resets on every
# ``chat`` / ``say`` / ``join_conversation`` and is cancelled on
# ``end_conversation`` or any turn-limit auto-end. See
# ``_schedule_inactivity_timeout``. Tunable via the
# ``CONVERSATION_INACTIVITY_TIMEOUT_SEC`` env var so we can adjust without
# redeploy if the soak surfaces a need.
_DEFAULT_INACTIVITY_TIMEOUT_SEC: float = 180.0


def _read_inactivity_timeout() -> float:
    """Read the inactivity timeout, falling back to the default on bad input.

    Resolved once at module load (used by ``_inactivity_timeout`` and the
    info-log line). Pytest can monkeypatch ``_INACTIVITY_TIMEOUT_SEC``
    directly when needed.
    """
    raw = os.environ.get("CONVERSATION_INACTIVITY_TIMEOUT_SEC", "").strip()
    if not raw:
        return _DEFAULT_INACTIVITY_TIMEOUT_SEC
    try:
        value = float(raw)
        if value <= 0:
            raise ValueError
        return value
    except ValueError:
        logger.warning(
            "CONVERSATION_INACTIVITY_TIMEOUT_SEC=%r is not a positive number — using default %.0fs",
            raw,
            _DEFAULT_INACTIVITY_TIMEOUT_SEC,
        )
        return _DEFAULT_INACTIVITY_TIMEOUT_SEC


_INACTIVITY_TIMEOUT_SEC: float = _read_inactivity_timeout()

# conv_id → asyncio.Task that will call ``_finalize_conversation`` after
# ``_INACTIVITY_TIMEOUT_SEC`` of silence. In-memory only — a backend
# restart drops pending timers (acceptable: the existing MAX_CONVERSATION_TURNS
# cap and manual ``town_act end`` still bound conversation lifetime, and the
# idle agents will naturally drop their "talking" activity on next restart
# reconnect).
_inactivity_tasks: dict[str, asyncio.Task] = {}


async def _inactivity_timeout(conv_id: str) -> None:
    """Background coroutine: wait the silence window, then auto-end the conv.

    Cancelled (and replaced) on every new turn or join, so it only fires
    when a conversation has genuinely gone quiet.
    """
    try:
        await asyncio.sleep(_INACTIVITY_TIMEOUT_SEC)
    except asyncio.CancelledError:
        return
    finally:
        # Clear the registry entry in the SAME task that owns it, whether
        # we timed out or were cancelled. Matches the pattern in
        # ``agent_connect_service._delayed_sleep``.
        _inactivity_tasks.pop(conv_id, None)

    service = TownService()
    conv = service.get_conversation_by_id(conv_id)
    if not conv or conv.get("status") != "active":
        # Already ended by another path (end_conversation, turn limit,
        # server restart recovery). Nothing to do.
        return

    logger.info(
        "Conversation %s silent for %.0fs — auto-ending (inactivity)",
        conv_id,
        _INACTIVITY_TIMEOUT_SEC,
    )
    ws_manager = get_town_agent_ws_manager()
    await _finalize_conversation(conv_id, service, ws_manager, auto_ended=True, end_reason_override="inactivity")


def _schedule_inactivity_timeout(conv_id: str) -> None:
    """Schedule (or reset) the silence-auto-end timer for a conversation.

    Called on ``chat`` / ``say`` / ``join_conversation`` — any "activity"
    signal that proves the conversation is still ongoing. Cancels any
    previous timer for the same ``conv_id`` so the window is always
    measured from the most recent activity.
    """
    existing = _inactivity_tasks.get(conv_id)
    if existing and not existing.done():
        existing.cancel()
    _inactivity_tasks[conv_id] = asyncio.create_task(_inactivity_timeout(conv_id))


def _cancel_inactivity_timeout(conv_id: str) -> None:
    """Cancel a pending silence-auto-end timer. Idempotent.

    Called when a conversation ends explicitly (via ``end_conversation``
    or a turn-limit-driven auto-end) so we don't double-finalize.
    """
    task = _inactivity_tasks.pop(conv_id, None)
    if task and not task.done():
        task.cancel()


async def _handle_chat(connection_id: str, body: dict, agent_conn) -> None:
    """Handle a ``chat`` action — initiate a new conversation.

    Looks up the target agent by ``agentName``, validates neither party is
    already in a conversation, creates an active conversation record in
    DynamoDB, and delivers a ``conversation_invite`` event to the target.

    Args:
        connection_id: API Gateway connection ID of the initiating agent.
        body: Parsed WebSocket message body containing ``target`` (agentName)
            and ``message`` (opening line).
        agent_conn: Connection record for the initiating agent.
    """
    from core.services.daemon_mailbox import get_daemon_mailbox

    mailbox = get_daemon_mailbox()
    service = TownService()
    ws_manager = get_town_agent_ws_manager()
    initiator_id = agent_conn.agent_id

    def _reject(reason: str) -> None:
        mailbox.reject(connection_id, "chat_rejected", reason)

    target_agent_id = body.get("target", "").strip()
    opening_message = body.get("message", "").strip()

    if not target_agent_id or not opening_message:
        _reject("missing_target_or_message")
        return

    # Enforce UUID target — names are not globally unique. The TOWN_STATUS.md
    # action menu always emits the UUID; if a name slips through it means the
    # agent is using a stale or hand-written command. Fail fast so the agent
    # knows to look up the correct ID rather than silently creating a broken
    # conversation record.
    if not _UUID_RE.match(target_agent_id):
        logger.warning(
            "chat rejected: target %r is not a UUID (agent %s)",
            target_agent_id,
            initiator_id,
        )
        _reject("target_must_be_uuid")
        return

    # Load initiator from DB
    initiator = service.get_town_agent_by_id(initiator_id)
    if not initiator:
        _reject("initiator_not_found")
        return

    # Validate initiator is not already in a conversation
    if initiator.get("currentConversationId"):
        _reject("already_in_conversation")
        return

    # Validate initiator is not in the middle of a timed activity. Without
    # this guard, the chat-start path overwrote the game server's
    # ``_agent_activity`` with "talking" while the DDB ``currentActivity``
    # field stayed set, leaving the two sources of truth diverged until
    # the activity timer fired. Surfaced 2026-05-04 by the game-designer
    # audit.
    if initiator.get("currentActivity"):
        _reject("already_in_activity")
        return

    # Target is agent_id (UUID). Globally unique — the only identifier safe
    # to use for cross-user chat. Machine names and display names can collide.
    target = service.get_town_agent_by_id(target_agent_id)
    if not target or target.get("isActive") != "true":
        _reject("target_not_found")
        return

    target_id = target["id"]
    if target_id == initiator_id:
        _reject("cannot_chat_with_self")
        return

    # Validate target is not already in a conversation
    if target.get("currentConversationId"):
        _reject("target_busy")
        return

    # Validate target is not mid-activity. Same divergence concern as the
    # initiator check above — pulling the target out of an activity to
    # join a chat would corrupt their activity state.
    if target.get("currentActivity"):
        _reject("target_busy")
        return

    # Validate target's daemon is live. If the target isn't connected to the
    # WebSocket, there is nobody to receive the conversation_invite — the
    # invite would silently vanish and the initiator would get an orphaned
    # conv_id with no reply path. Reject synchronously so the initiator's CLI
    # sees "target_not_connected" rather than a silent {ok} that leads nowhere.
    if ws_manager.get_by_agent_id(target_id) is None:
        logger.info(
            "chat rejected: target %s is not connected (initiator %s)",
            target_id,
            initiator_id,
        )
        _reject("target_not_connected")
        return

    # Validate agents are within proximity (3 tiles / 96px — same as game server)
    gs_client = get_game_server_client()
    init_state = gs_client.get_cached_agent_state(initiator_id)
    target_state = gs_client.get_cached_agent_state(target_id)
    if init_state and target_state:
        import math

        dx = float(init_state.get("x", 0)) - float(target_state.get("x", 0))
        dy = float(init_state.get("y", 0)) - float(target_state.get("y", 0))
        dist = math.sqrt(dx * dx + dy * dy)
        if dist > 96.0:  # PROXIMITY_THRESHOLD from agent_controller.gd
            _reject("not_nearby")
            return
    elif not init_state or not target_state:
        # No cached position — fall back to location check
        initiator_location = initiator.get("currentLocation", "")
        target_location = target.get("currentLocation", "")
        if initiator_location != target_location:
            _reject("not_nearby")
            return

    # Create the conversation record
    location = initiator.get("currentLocation", "residence")
    initiator_display = initiator.get("displayName", agent_conn.agent_name)
    conv = service.create_active_conversation(
        initiator_id=initiator_id,
        target_id=target_id,
        location=location,
        opening_message=opening_message,
        initiator_display_name=initiator_display,
    )
    conv_id = conv["id"]

    # Hot-path cache: `_send_to_daemon` reads `current_conversation_id`
    # from ws_manager on every 2s agent_state broadcast. Keep it synced
    # with the DB write we just did.
    ws_manager.update_conversation(initiator_id, conv_id)
    ws_manager.update_conversation(target_id, conv_id)

    # Deliver invite to target agent if they are connected
    target_conn = ws_manager.get_by_agent_id(target_id)
    if target_conn:
        mailbox.notify_connection(
            target_conn.connection_id,
            {
                "event": "conversation_invite",
                "conv_id": conv_id,
                "from": initiator_display,
                "from_agent_name": agent_conn.agent_name,
                "message": opening_message,
                "location": location,
            },
        )
    else:
        logger.info("Target agent %s not connected — invite stored in conversation record", target_id)

    # Echo the conversation back to the initiator so their daemon can populate
    # active_conversation. Without this the initiator has no local view of their
    # own outgoing chat — they see replies flow in but not their own opening
    # line, and the conv_id isn't mirrored into state.json. F11 from the
    # 2026-04-21 agent-team dry run.
    target_display_name = target.get("displayName", target.get("agentName", target_agent_id))
    mailbox.notify_connection(
        connection_id,
        {
            "event": "conversation_started",
            "conv_id": conv_id,
            "with_agent_id": target_id,
            "with_display": target_display_name,
            "with_agent_name": target.get("agentName", ""),
            "opening_message": opening_message,
            "location": location,
        },
    )

    # Acknowledge to initiator with the conversation ID
    mailbox.notify_connection(
        connection_id,
        {
            "event": "act_ok",
            "action": "chat",
            "conv_id": conv_id,
            "target": target_display_name,
        },
    )

    # Send speech bubble to game server for viewer display
    try:
        gs_client = get_game_server_client()
        await gs_client.send_command(initiator_id, {"cmd": "set_speech", "text": opening_message})
    except Exception:
        # Speech bubble is cosmetic, don't fail the conversation. Log at
        # debug so we can still see when the game server connection is
        # flaky in production.
        logger.debug(
            "Speech-bubble push failed for %s on conversation %s",
            initiator_id,
            conv_id,
            exc_info=True,
        )

    # Lay out the 2-agent conversation: axis-align + face each other. The
    # game server's `layout_conversation` command computes target positions
    # (on whichever axis currently has greater separation, 2 tiles apart
    # centered on the midpoint), walks each agent there via NavigationAgent2D,
    # and sets forward-looking facing toward the target centroid. Replaces
    # the earlier pair of `set_facing` commands — positioning + facing come
    # from one source of truth so they can't drift.
    try:
        gs_client = get_game_server_client()
        await gs_client.send_command(
            initiator_id,
            {"cmd": "layout_conversation", "agent_ids": [initiator_id, target_id]},
        )
        await gs_client.send_command(initiator_id, {"cmd": "set_activity", "activity": "talking"})
    except Exception:
        # Layout / talking-activity is cosmetic — agent can still chat.
        logger.debug(
            "layout_conversation/set_activity failed for %s on conversation %s",
            initiator_id,
            conv_id,
            exc_info=True,
        )

    logger.info(
        "Conversation %s started between %s and %s at %s",
        conv_id,
        initiator_id,
        target_id,
        location,
    )

    # Lazy-enqueue 'talking' animation for both participants on their first
    # conversation. Non-fatal: a failed enqueue must never block chat.
    try:
        _maybe_enqueue_talking(initiator)
        _maybe_enqueue_talking(target)
    except Exception:
        logger.debug("_maybe_enqueue_talking failed (non-fatal)", exc_info=True)

    # Start the silence timer. Reset on every ``say`` / ``join_conversation``;
    # if it fires, ``_inactivity_timeout`` calls ``_finalize_conversation``
    # with ``end_reason="inactivity"``.
    _schedule_inactivity_timeout(conv_id)


async def _handle_say(connection_id: str, body: dict, agent_conn) -> None:
    """Handle a ``say`` action — append a message to an active conversation.

    Validates that the conversation exists, is active, and the speaker is a
    participant. Appends the message and forwards it to the other participant.
    Auto-ends the conversation when the turn limit is reached.

    Args:
        connection_id: API Gateway connection ID of the speaking agent.
        body: Parsed message body with ``conv_id`` and ``message``.
        agent_conn: Connection record for the speaking agent.
    """
    from core.services.daemon_mailbox import get_daemon_mailbox

    mailbox = get_daemon_mailbox()
    service = TownService()
    ws_manager = get_town_agent_ws_manager()
    speaker_id = agent_conn.agent_id

    def _reject(reason: str) -> None:
        mailbox.reject(connection_id, "say_rejected", reason)

    conv_id = body.get("conv_id", "").strip()
    message = body.get("message", "").strip()

    if not conv_id or not message:
        _reject("missing_conv_id_or_message")
        return

    # Validate the conversation
    conv = service.get_conversation_by_id(conv_id)
    if not conv:
        _reject("conversation_not_found")
        return

    if conv.get("status") != "active":
        _reject("conversation_not_active")
        return

    participants: list[str] = conv.get("participants") or [
        conv.get("participantAId"),
        conv.get("participantBId"),
    ]
    participants = [p for p in participants if p]
    if speaker_id not in participants:
        _reject("not_a_participant")
        return

    # Enforce "not twice in a row" for group conversations (3+ participants)
    if len(participants) > 2 and conv.get("lastSpeakerId") == speaker_id:
        _reject("must_let_others_speak")
        return

    # Append the message
    speaker = service.get_town_agent_by_id(speaker_id)
    speaker_display = speaker.get("displayName", agent_conn.agent_name) if speaker else agent_conn.agent_name

    updated = service.append_message_to_conversation(conv_id, speaker_id, speaker_display, message)
    if not updated:
        _reject("conversation_not_active")
        return

    turn_count = updated.get("turnCount", 0)

    # Forward message to ALL participants (including the speaker). Without
    # the speaker echo their daemon has no local record of their own turn —
    # `active_conversation.turns` only shows what others said, which makes
    # the conversation half-visible to the speaker. F8 from the 2026-04-21
    # agent-team dry run. Single source of truth: backend broadcasts, daemons
    # append.
    for pid in participants:
        mailbox.notify(
            pid,
            {
                "event": "conversation_message",
                "conv_id": conv_id,
                "from": speaker_display,
                "from_agent_name": agent_conn.agent_name,
                "text": message,
                "turn": turn_count,
            },
        )

    # Send speech bubble to game server for viewer display.
    try:
        gs_client = get_game_server_client()
        await gs_client.send_command(speaker_id, {"cmd": "set_speech", "text": message})
    except Exception:
        # Speech bubble is cosmetic, don't fail the conversation.
        logger.debug(
            "set_speech failed for %s on conversation %s",
            speaker_id,
            conv_id,
            exc_info=True,
        )

    # Set talking activity on the speaker
    try:
        gs_client = get_game_server_client()
        await gs_client.send_command(speaker_id, {"cmd": "set_activity", "activity": "talking"})
    except Exception:
        logger.debug(
            "set_activity 'talking' failed for %s on conversation %s",
            speaker_id,
            conv_id,
            exc_info=True,
        )

    # Per-turn mood drip on the speaker. Pre-fix, the only conversation
    # mood reward fired at ``_finalize_conversation``, so a 2-turn
    # conversation and a 20-turn conversation produced identical mood
    # outcomes — agents had no signal that sustained dialogue was worth
    # more than a quick exchange. Fired through
    # ``_apply_event_with_sleep_guard`` so a speaker who runs themselves
    # to energy=0 over a long conversation hits the same auto-sleep
    # path the back-loaded event would have hit. Surfaced 2026-05-04
    # game-designer audit.
    speaker_agent = service.get_town_agent_by_id(speaker_id)
    if speaker_agent:
        _apply_event_with_sleep_guard(service, speaker_id, speaker_agent, "conversation_turn")

    logger.info("Conversation %s turn %d: %s said %d chars", conv_id, turn_count, speaker_id, len(message))

    # Auto-end at turn limit
    if turn_count >= _MAX_CONVERSATION_TURNS:
        logger.info("Conversation %s reached turn limit (%d), auto-ending", conv_id, _MAX_CONVERSATION_TURNS)
        await _finalize_conversation(conv_id, service, ws_manager, auto_ended=True)
        return

    # A new turn is activity — reset the silence timer. Do this AFTER the
    # turn-limit auto-end so we don't schedule a timer on a just-finalized
    # conversation.
    _schedule_inactivity_timeout(conv_id)


async def _handle_join_conversation(connection_id: str, body: dict, agent_conn) -> None:
    """Handle a join_conversation action — agent joins an existing active conversation.

    Rejects with structured ``join_rejected`` events on:

    * ``missing_conv_id`` — body has no conversation id.
    * ``joiner_not_found`` — agent record missing from DDB.
    * ``already_in_conversation`` / ``already_in_activity`` — joiner is busy.
    * ``conversation_not_found`` — ``conv_id`` doesn't resolve.
    * ``not_in_proximity`` — joiner is not at the conversation's location, or
      is at the same location but >96px away from every existing participant.
      Tier 1 #1.5: prevents an agent at the library from joining a
      conversation at the cafe ("town is a place" pillar).
    * ``conversation_not_joinable`` — service refused to add the joiner
      (typically: conversation already finalized between read and write).
    """
    import math

    from core.services.daemon_mailbox import get_daemon_mailbox

    mailbox = get_daemon_mailbox()
    service = TownService()
    ws_manager = get_town_agent_ws_manager()
    gs_client = get_game_server_client()
    joiner_id = agent_conn.agent_id

    def _reject(reason: str) -> None:
        mailbox.reject(connection_id, "join_rejected", reason)

    conv_id = body.get("conv_id", "")
    if not conv_id:
        _reject("missing_conv_id")
        return

    joiner = service.get_town_agent_by_id(joiner_id)
    if not joiner:
        _reject("joiner_not_found")
        return

    if joiner.get("currentConversationId"):
        _reject("already_in_conversation")
        return

    if joiner.get("currentActivity"):
        _reject("already_in_activity")
        return

    # Read the conversation BEFORE mutating — the proximity check below needs
    # the existing participants list and the conversation's location, and we
    # must reject without calling ``service.join_conversation`` so the
    # participants list isn't dirtied.
    conv = service.get_conversation_by_id(conv_id)
    if not conv:
        _reject("conversation_not_found")
        return

    # Proximity guard. Two layers:
    #   1. Location-name equality. Conversations are bound to the location of
    #      the initiator at creation time. Cross-location joins teleport an
    #      agent into a private dialogue and break the spatial-presence model.
    #   2. Within the same location, enforce the 96px / 3-tile radius rule
    #      that ``_handle_chat`` uses. A location like ``plaza`` is large
    #      enough that two agents can stand 5 tiles apart and not be in
    #      audible range of each other's conversation.
    #
    # If cached x/y positions are available for the joiner AND at least one
    # existing participant, we use them. Otherwise we fall back to location-
    # name equality alone — same fail-OPEN-within-location / fail-CLOSED-
    # across-location pattern as ``_handle_chat``.
    joiner_location = joiner.get("currentLocation") or ""
    conv_location = conv.get("location") or ""
    if joiner_location != conv_location:
        _reject("not_in_proximity")
        return

    existing_participants: list[str] = [pid for pid in conv.get("participants", []) if pid != joiner_id]
    joiner_state = gs_client.get_cached_agent_state(joiner_id)
    if joiner_state and existing_participants:
        any_within_radius = False
        any_participant_state_seen = False
        jx = float(joiner_state.get("x", 0))
        jy = float(joiner_state.get("y", 0))
        for pid in existing_participants:
            p_state = gs_client.get_cached_agent_state(pid)
            if not p_state:
                continue
            any_participant_state_seen = True
            dx = jx - float(p_state.get("x", 0))
            dy = jy - float(p_state.get("y", 0))
            if math.hypot(dx, dy) <= 96.0:  # PROXIMITY_THRESHOLD from agent_controller.gd
                any_within_radius = True
                break
        if any_participant_state_seen and not any_within_radius:
            _reject("not_in_proximity")
            return
        # If no participant cached state at all, the location-equality check
        # above is the strongest guard available; fall through to join.

    updated = service.join_conversation(conv_id, joiner_id)
    if updated is None:
        _reject("conversation_not_joinable")
        return

    # Hot-path cache sync (see _handle_chat).
    ws_manager.update_conversation(joiner_id, conv_id)

    joiner_display = joiner.get("displayName", agent_conn.agent_name)
    participants: list[str] = updated.get("participants", [])

    # Notify all other participants
    for pid in participants:
        if pid == joiner_id:
            continue
        mailbox.notify(
            pid,
            {
                "event": "participant_joined",
                "conv_id": conv_id,
                "agent_id": joiner_id,
                "display_name": joiner_display,
            },
        )

    # Acknowledge to the joiner
    mailbox.notify_connection(
        connection_id,
        {"event": "act_ok", "action": "join_conversation", "conv_id": conv_id},
    )

    # Re-lay-out the whole group now that a new participant joined. The game
    # server's ``layout_conversation`` handler reshuffles everyone onto a
    # circle around the group centroid and points them inward — otherwise a
    # joiner would stand wherever they happened to be and nobody would turn
    # to acknowledge them.
    try:
        gs_client = get_game_server_client()
        await gs_client.send_command(
            joiner_id,
            {"cmd": "layout_conversation", "agent_ids": participants},
        )
        # Joiner enters the talking animation immediately so their first say
        # doesn't show a flash of idle; existing participants are already
        # talking from the initial chat.
        await gs_client.send_command(joiner_id, {"cmd": "set_activity", "activity": "talking"})
    except Exception:
        # Cosmetic — don't fail the join.
        logger.debug(
            "layout_conversation/set_activity failed for joiner %s on conversation %s",
            joiner_id,
            conv_id,
            exc_info=True,
        )

    logger.info("Agent %s joined conversation %s (%d participants)", joiner_id, conv_id, len(participants))

    # Joining is activity — reset the silence timer so the conversation
    # gets another full window to produce a turn before auto-ending.
    _schedule_inactivity_timeout(conv_id)


async def _handle_end_conversation(connection_id: str, body: dict, agent_conn) -> None:
    """Handle an ``end_conversation`` action — agent LEAVES the conversation.

    Validates the requester is a participant, marks the conversation ended,
    applies mood effects to both agents, updates the relationship affinity,
    and notifies both participants.

    Args:
        connection_id: API Gateway connection ID of the ending agent.
        body: Parsed message body with ``conv_id``.
        agent_conn: Connection record for the ending agent.
    """
    from core.services.daemon_mailbox import get_daemon_mailbox

    mailbox = get_daemon_mailbox()
    service = TownService()
    ws_manager = get_town_agent_ws_manager()
    ender_id = agent_conn.agent_id

    def _reject(reason: str) -> None:
        mailbox.reject(connection_id, "end_rejected", reason)

    conv_id = body.get("conv_id", "").strip()
    if not conv_id:
        _reject("missing_conv_id")
        return

    conv = service.get_conversation_by_id(conv_id)
    if not conv:
        _reject("conversation_not_found")
        return

    participants: list[str] = conv.get("participants") or [
        conv.get("participantAId"),
        conv.get("participantBId"),
    ]
    participants = [p for p in participants if p]
    if ender_id not in participants:
        _reject("not_a_participant")
        return

    if conv.get("status") != "active":
        _reject("conversation_not_active")
        return

    # Leave semantics: remove agent from participants
    remaining = service.leave_conversation(conv_id, ender_id)

    # Hot-path cache sync (see _handle_chat). Clear conv_id for the leaver.
    ws_manager.update_conversation(ender_id, "")

    # Notify remaining participants that someone left
    leaver_display = agent_conn.agent_name
    for pid in participants:
        if pid == ender_id:
            continue
        mailbox.notify(
            pid,
            {
                "event": "participant_left",
                "conv_id": conv_id,
                "agent_id": ender_id,
                "display_name": leaver_display,
                "remaining": remaining,
            },
        )

    # Acknowledge leaver
    mailbox.notify_connection(
        connection_id,
        {"event": "act_ok", "action": "end_conversation", "conv_id": conv_id},
    )

    # The leaver also needs a ``conversation_ended`` event so their daemon
    # clears local ``active_conversation`` / ``pending_messages`` cache.
    # Without this their next ``chat`` blocks with
    # ``already_in_conversation`` for the duration of either the
    # inactivity timer (180 s default) OR the global finalize event —
    # neither of which fires for the leaver in the multi-participant
    # case. 5/5 agents in the 2026-05-04 soak hit this; conversations
    # appeared stuck for 2-10+ minutes. Finding W.
    mailbox.notify_connection(
        connection_id,
        {
            "event": "conversation_ended",
            "conv_id": conv_id,
            "turn_count": conv.get("turnCount", 0),
            "location": conv.get("location", "unknown"),
            "reason": "left_by_self",
        },
    )

    # If fewer than 2 remain, finalize the conversation entirely.
    # ``_finalize_conversation``'s notify-loop iterates over the
    # post-leave participants list (which excludes the leaver), so the
    # leaver does NOT get duplicate ``conversation_ended`` events from
    # finalize — the explicit send above is the leaver's only one.
    if remaining < 2:
        await _finalize_conversation(conv_id, service, ws_manager, auto_ended=False)


async def _finalize_conversation(
    conv_id: str,
    service: TownService,
    ws_manager,
    auto_ended: bool,
    end_reason_override: str | None = None,
) -> None:
    """End a conversation, apply mood/relationship effects, and notify both agents.

    Extracted from ``_handle_end_conversation`` so the auto-end path on turn
    limit can share the same logic.

    Args:
        conv_id: Conversation UUID to finalise.
        service: ``TownService`` instance (caller's, to avoid re-instantiation).
        ws_manager: Active WebSocket manager.
        auto_ended: Whether the conversation ended by hitting the turn limit
            or the silence timeout (``True``) or by explicit agent request
            (``False``).
    """
    from core.services.daemon_mailbox import get_daemon_mailbox

    mailbox = get_daemon_mailbox()
    # Cancel any pending silence-auto-end timer for this conv. Idempotent —
    # if the silence-timer path itself is finalising, the task has already
    # self-removed from ``_inactivity_tasks`` in its ``finally`` block.
    _cancel_inactivity_timeout(conv_id)

    conv = service.end_conversation(conv_id)
    if not conv:
        logger.warning("end_conversation returned None for conv_id %s", conv_id)
        return

    # Hot-path cache sync: end_conversation cleared currentConversationId
    # in DynamoDB for every participant. Clear the ws_manager cache too
    # so agent_state broadcasts stop claiming they're still in a conv.
    for _pid in conv.get("participants", []) or [
        conv.get("participantAId"),
        conv.get("participantBId"),
    ]:
        if _pid:
            ws_manager.update_conversation(_pid, "")

    participants: list[str] = conv.get("participants") or [
        conv.get("participantAId"),
        conv.get("participantBId"),
    ]
    participants = [p for p in participants if p]
    turn_count = conv.get("turnCount", 0)
    location = conv.get("location", "unknown")

    if end_reason_override:
        end_reason = end_reason_override
    else:
        end_reason = "turn_limit_reached" if auto_ended else "ended_by_participant"

    # Apply mood/energy effects for each participant. The helper server-enforces
    # sleep (energy→100, WS close) if the event would drop energy to 0.
    for agent_id in participants:
        agent = service.get_town_agent_by_id(agent_id)
        if not agent:
            continue
        _apply_event_with_sleep_guard(service, agent_id, agent, "conversation_completed")

    # Update relationship affinity for all participant pairs
    if len(participants) >= 2:
        for i in range(len(participants)):
            for j in range(i + 1, len(participants)):
                try:
                    service.get_or_create_relationship(participants[i], participants[j])
                    service.update_relationship(participants[i], participants[j], affinity_delta=5)
                except Exception as exc:
                    logger.warning("Failed to update relationship: %s", exc)

    # Stop the "talking" animation for all participants. Deliberately do NOT
    # clear the speech bubble here: with paced, fast exchanges the final line
    # would be wiped the instant the conversation ends, before anyone reads it.
    # The game server's SPEECH_DURATION timer expires each bubble ~5s after the
    # last say, so the closing line lingers naturally instead of snapping away.
    gs_client = get_game_server_client()
    for agent_id in participants:
        try:
            await gs_client.send_command(agent_id, {"cmd": "set_activity", "activity": ""})
        except Exception:
            # Cosmetic cleanup — viewer will reconcile from world_state
            # broadcast within ~2s anyway. Keep finalisation moving.
            logger.debug(
                "Failed to clear activity for %s on conversation %s finalise",
                agent_id,
                conv_id,
                exc_info=True,
            )

    # Notify all connected participants
    public_log = conv.get("publicLog") or []
    log_summary = _build_log_summary(public_log)

    for agent_id in participants:
        mailbox.notify(
            agent_id,
            {
                "event": "conversation_ended",
                "conv_id": conv_id,
                "turn_count": turn_count,
                "location": location,
                "reason": end_reason,
                "log_summary": log_summary,
            },
        )

    # LOG-only nudge: after finishing a conversation, prompt each participant
    # to consider whether their stance toward the other(s) has changed.
    # ``think`` is deliberately absent so the daemon only logs this to
    # TOWN_EVENTS.md and does not interrupt the agent mid-flow.
    other_names = {p: ws_manager.get_by_agent_id(p) for p in participants}
    for agent_id in participants:
        others = [p for p in participants if p != agent_id]
        if not others:
            continue
        other_displays = []
        for other_id in others:
            conn = other_names.get(other_id)
            display = getattr(conn, "agent_name", None) or other_id[:8]
            other_displays.append(display)
        mailbox.notify(
            agent_id,
            {
                "event": "system.consider_stance",
                "conv_id": conv_id,
                "others": others,
                "message": (
                    f"You just finished a conversation with "
                    f"{', '.join(other_displays)}. "
                    "How do you feel about them now? "
                    "Use `act describe_stance <agent_id> <text>` to record "
                    "your impression."
                ),
            },
        )

    logger.info(
        "Conversation %s finalised (%s, %d turns, location=%s, %d participants)",
        conv_id,
        end_reason,
        turn_count,
        location,
        len(participants),
    )


def _build_log_summary(public_log: list, max_turns: int = 5) -> str:
    """Build a short plaintext summary of the last N turns for the ended event.

    Included in ``conversation_ended`` so agents can log to TOWN_LIFE.md
    without re-fetching the full log.

    Args:
        public_log: List of ``{speaker, text}`` dicts from DynamoDB.
        max_turns: How many recent turns to include (default: last 5).

    Returns:
        Multi-line string, or empty string if the log is empty.
    """
    if not public_log:
        return ""
    recent = public_log[-max_turns:]
    return "\n".join(f"{t.get('speaker', '?')}: {t.get('text', '')}" for t in recent)
