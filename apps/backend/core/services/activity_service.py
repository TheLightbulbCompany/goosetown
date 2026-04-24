"""Activity system — timed activities, custom animation generation, cleanup.

Extracted from ``routers/town.py`` as the third sub-project-#3 slice
(after sprite_pipeline and conversation_service). ~540 lines moved out,
bringing the router below 1700 lines.

Functions:
* ``_handle_agent_activity`` — validates and starts a timed activity
  (called from the ``handle_ws_message`` dispatch ladder).
* ``_generate_custom_animation`` — background PixelLab task that lazily
  generates a missing activity animation (e.g. "reading", "cooking").
* ``_activity_timer`` — the asyncio task that runs while an activity is
  active, resolves at completion, and emits the result to the daemon.
* ``_clear_activity_and_notify`` — shared cleanup helper used by timer
  + startup-resume paths.
* ``complete_activity_from_startup`` / ``restart_activity_timer`` —
  entry points used by ``main.py:_reconnect_active_agents`` to pick up
  activities that were mid-flight when the backend was restarted.

Module-level state (``_activity_tasks``, ``_custom_anim_in_progress``)
holds strong references to in-flight asyncio tasks so they're not GC'd
mid-wait. Stays per-process in-memory; a backend restart drops these
and the startup code above re-creates whatever's still valid.

The dispatch ladder, ``main.py`` startup, and any external caller can
still import these names from ``routers.town`` via the re-export block
at the bottom of that module.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from core.services.activity_registry import apply_activity_effect, get_activity
from core.services.daemon_bridge import _send_to_daemon
from core.services.game_server_client import get_game_server_client
from core.services.town_agent_ws import get_town_agent_ws_manager
from core.services.town_mood_engine import parse_mood
from core.services.town_service import TownService

logger = logging.getLogger(__name__)


# Strong references to activity timer tasks to prevent GC
_activity_tasks: set = set()
_custom_anim_in_progress: set[tuple[str, str]] = set()  # (agent_id, animation_name)


async def _handle_agent_activity(connection_id: str, activity_name: str, agent_conn) -> None:
    """Validate and start a timed activity for a connected agent.

    Sends an ``activity_rejected`` event back to the agent if validation
    fails, or an ``act_ok`` followed by an asyncio background timer that
    sends ``activity_complete`` (or ``activity_interrupted``) when the
    duration elapses.

    For apartment activities that have a ``spot`` field the agent is first
    navigated to that furniture marker via the game server; the activity
    timer starts only after the ``arrived`` event for that spot.

    Args:
        connection_id: API Gateway connection ID for the acting agent.
        activity_name: The ``town_act`` argument (e.g. ``"read"``).
        agent_conn: Connection record from ``TownAgentWsManager``.
    """
    from core.services.activity_registry import (
        canonical_location,
        get_context_mismatch_message,
    )
    from core.services.management_api_client import ManagementApiClient

    mgmt = ManagementApiClient()
    service = TownService()
    agent_id = agent_conn.agent_id

    def _reject(reason: str, message: Optional[str] = None) -> None:
        payload: dict = {"event": "activity_rejected", "activity": activity_name, "reason": reason}
        if message:
            payload["message"] = message
        try:
            mgmt.send_message(connection_id, payload)
        except Exception as exc:
            logger.warning("Failed to send activity_rejected to %s: %s", agent_id, exc)

    # --- Look up activity ---
    activity = get_activity(activity_name)
    if activity is None:
        _reject("unknown_activity")
        return

    # --- Load agent from DB ---
    agent = service.get_town_agent_by_id(agent_id)
    if not agent:
        _reject("agent_not_found")
        return

    # --- Validate location context (town vs apartment) ---
    # This must happen before the fine-grained location check so the agent
    # receives a useful navigation hint rather than a bare "wrong_location".
    agent_location_context = agent.get("locationContext", "town")
    mismatch_msg = get_context_mismatch_message(activity, agent_location_context)
    if mismatch_msg:
        _reject("wrong_context", mismatch_msg)
        return

    # --- Validate fine-grained location within the correct context ---
    current_location = agent.get("currentLocation", "")
    if canonical_location(current_location) != activity.location:
        _reject("wrong_location")
        return

    # --- Validate not already busy ---
    if agent.get("currentActivity"):
        _reject("already_busy")
        return

    # --- Mark agent as busy in DB ---
    import time as _time

    logger.info("[agent:%s] Activity START: %s (conn=%s)", agent_id[:8], activity_name, connection_id[:12])
    service.update_agent_activity(agent_id, activity_name, _time.time())

    # Notify game server of activity state change
    try:
        gs_client = get_game_server_client()
        await gs_client.send_command(
            agent_id,
            {
                "cmd": "set_activity",
                "activity": activity_name,
            },
        )
    except Exception as exc:
        logger.warning("Failed to send set_activity to game server for %s: %s", agent_id, exc)

    # --- Acknowledge to the agent ---
    try:
        mgmt.send_message(
            connection_id,
            {
                "event": "act_ok",
                "activity": activity_name,
                "duration_seconds": activity.duration_seconds,
            },
        )
    except Exception as exc:
        logger.warning("Failed to send act_ok to %s: %s", agent_id, exc)

    # --- Trigger lazy custom animation generation if needed ---
    from core.services.pixellab_service import CUSTOM_ANIMATIONS

    activity_anim = activity.animation
    if activity_anim in CUSTOM_ANIMATIONS:
        agent_animations = agent.get("spriteAnimations") or {}
        anim_key = (agent_id, activity_anim)
        char_id = agent.get("pixellabCharacterId") or agent.get("pixellab_character_id")
        logger.info(
            "Custom animation check: anim=%s, already_have=%s, in_progress=%s, char_id=%s",
            activity_anim,
            activity_anim in agent_animations,
            anim_key in _custom_anim_in_progress,
            char_id or "MISSING",
        )
        if activity_anim not in agent_animations and anim_key not in _custom_anim_in_progress:
            if char_id:
                _custom_anim_in_progress.add(anim_key)
                logger.info(
                    "Triggering lazy animation generation: %s for agent %s (char %s)",
                    activity_anim,
                    agent_id,
                    char_id,
                )
                gen_task = asyncio.create_task(_generate_custom_animation(agent_id, char_id, activity_anim))
                _activity_tasks.add(gen_task)
                gen_task.add_done_callback(_activity_tasks.discard)
            else:
                logger.warning(
                    "Cannot trigger custom animation %s for agent %s: no pixellabCharacterId in DB",
                    activity_anim,
                    agent_id,
                )

    # --- Schedule the activity timer ---
    task = asyncio.create_task(_activity_timer(connection_id, agent_id, activity, agent))
    _activity_tasks.add(task)

    def _on_timer_done(t: asyncio.Task) -> None:
        _activity_tasks.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            logger.error(
                "[agent:%s] Activity timer CRASHED for %s: %s",
                agent_id[:8],
                activity.name,
                exc,
                exc_info=exc,
            )
            # Best-effort cleanup: clear the stuck activity in DB
            try:
                TownService().update_agent_activity(agent_id, None, None)
                logger.info("[agent:%s] Cleared stuck activity %s after timer crash", agent_id[:8], activity.name)
            except Exception:
                logger.warning("[agent:%s] Failed to clear stuck activity %s", agent_id[:8], activity.name)

    task.add_done_callback(_on_timer_done)


async def _generate_custom_animation(agent_id: str, character_id: str, animation_name: str) -> None:
    """Background task: generate a custom animation via animate-with-text-v2.

    Uses the PixelLab V2 pro endpoint which takes the character's rotation
    images and an action description. Generates one direction at a time,
    collects frames, composites into a spritesheet, uploads to S3.
    """
    from core.config import settings
    from core.services.pixellab_service import ANIMATION_REGISTRY, CUSTOM_ANIMATIONS, PixelLabService
    from core.services.sprite_storage import composite_v2_animation, upload_sprites_to_s3

    config = CUSTOM_ANIMATIONS.get(animation_name)
    if not config:
        return

    pxl = PixelLabService()
    service = TownService()

    # Set a visible "learning" status on the agent so the frontend's
    # apartment card can render "Learning to cook…" instead of "Idle"
    # while we wait 1-3 minutes for PixelLab to generate the sprite.
    # Cleared in the ``finally`` block so both the success and failure
    # paths leave the agent without a stale status string.
    learning_status = f"Learning to {animation_name}…"
    try:
        service.update_agent_state(agent_id, status_message=learning_status)
    except Exception:
        logger.warning("Could not set learning status for agent %s", agent_id, exc_info=True)

    try:
        # Queue custom animation (one V2 job per direction)
        job_ids = await pxl.queue_custom_animation(
            character_id=character_id,
            animation_name=animation_name,
            action_description=config["prompt"],
        )
        if not job_ids:
            logger.warning("No job IDs returned for custom animation %s", animation_name)
            return

        logger.info(
            "Custom animation %s queued for agent %s (%d jobs), waiting...",
            animation_name,
            agent_id,
            len(job_ids),
        )

        # Wait for all V2 jobs to complete
        await pxl.wait_for_jobs(
            job_ids,
            progress_label=f"custom:{animation_name}",
        )

        # Collect frames from each completed job
        # Jobs are ordered: south, west, east, north (matching queue order)
        directions = ["south", "west", "east", "north"]
        direction_frames: dict[str, list[bytes]] = {}
        for i, job_id in enumerate(job_ids):
            direction = directions[i] if i < len(directions) else f"dir{i}"
            frames = await pxl.get_v2_animation_frames(job_id)
            if frames:
                direction_frames[direction] = frames
                logger.info("Got %d frames for %s/%s", len(frames), animation_name, direction)
            else:
                logger.warning("No frames for %s/%s (job %s)", animation_name, direction, job_id)

        if not direction_frames:
            logger.warning("No frames from any direction for %s", animation_name)
            return

        # Composite into spritesheet
        result = composite_v2_animation(direction_frames)
        if result is None:
            logger.warning("Failed to composite %s for agent %s", animation_name, agent_id)
            return

        png_bytes, frame_count = result
        all_sheets = {animation_name: (png_bytes, frame_count)}

        # Upload spritesheet to S3
        uploaded = await asyncio.to_thread(upload_sprites_to_s3, all_sheets, agent_id, settings.SPRITE_S3_BUCKET)

        # Merge into existing spriteAnimations
        agent = service.get_town_agent_by_id(agent_id)
        existing = (agent.get("spriteAnimations") or {}) if agent else {}

        for anim_name, info in uploaded.items():
            registry_entry = ANIMATION_REGISTRY.get(anim_name, {})
            existing[anim_name] = {
                "url": f"{settings.SPRITE_CDN_URL}/{info['key']}",
                "frameCount": info["frame_count"],
                "fps": int(registry_entry.get("fps", 8)),
            }

        service.update_agent_state(agent_id, sprite_animations=existing)
        logger.info("Custom animation %s ready for agent %s", animation_name, agent_id)

        # Push sprites_updated to daemon and viewers
        try:
            from core.services.management_api_client import ManagementApiClient

            ws_mgr = get_town_agent_ws_manager()
            conn = ws_mgr.get_by_agent_id(agent_id)
            if conn:
                mgmt = ManagementApiClient()
                mgmt.send_message(
                    conn.connection_id,
                    {
                        "event": "sprites_updated",
                        "agent_id": agent_id,
                        "animations": existing,
                    },
                )
        except Exception:
            pass

    except Exception:
        logger.exception("Custom animation generation failed: %s for agent %s", animation_name, agent_id)
    finally:
        _custom_anim_in_progress.discard((agent_id, animation_name))
        # Always clear the learning status so a crashed generation doesn't
        # leave the agent stuck "Learning to cook…" forever.
        try:
            service.update_agent_state(agent_id, status_message=None)
        except Exception:
            logger.warning("Could not clear learning status for agent %s", agent_id, exc_info=True)


async def _activity_timer(
    connection_id: str,
    agent_id: str,
    activity,
    agent_snapshot: dict,
) -> None:
    """Background timer that completes a timed activity.

    For apartment activities with a ``spot`` the agent is first navigated
    to the furniture marker.  The activity duration begins only after the
    game server confirms arrival at the spot (``arrived`` event).

    Args:
        connection_id: API Gateway connection ID — used to push events back.
        agent_id: Internal agent UUID.
        activity: The ``Activity`` being performed.
        agent_snapshot: Agent DB record captured at activity start (used for
            initial energy/mood; fresh values are re-fetched before applying
            effects so concurrent updates are handled correctly).
    """
    from core.services.management_api_client import ManagementApiClient

    mgmt = ManagementApiClient()
    service = TownService()
    gs_client = get_game_server_client()

    import time as _time

    start_time = _time.time()

    # --- For apartment activities, move to the furniture spot first ---
    if activity.spot:
        try:
            # Register arrival waiter BEFORE sending move to avoid race condition
            arrival_event = gs_client.expect_arrival(agent_id)
            await gs_client.send_command(
                agent_id,
                {"cmd": "move", "destination": activity.spot},
            )
            await asyncio.wait_for(
                gs_client.wait_for_arrival(agent_id, arrival_event),
                timeout=60.0,
            )
            # Reset start time: timer begins from when agent arrives at spot
            start_time = _time.time()
        except asyncio.TimeoutError:
            logger.warning(
                "Agent %s timed out navigating to spot %s for activity %s",
                agent_id,
                activity.spot,
                activity.name,
            )
            _clear_activity_and_notify(service, mgmt, connection_id, agent_id, activity.name, "navigation_timeout")
            try:
                await gs_client.send_command(agent_id, {"cmd": "set_activity", "activity": ""})
            except Exception:
                pass
            return
        except Exception as exc:
            logger.warning("Navigation error for agent %s activity %s: %s", agent_id, activity.name, exc)
            _clear_activity_and_notify(service, mgmt, connection_id, agent_id, activity.name, "navigation_error")
            try:
                await gs_client.send_command(agent_id, {"cmd": "set_activity", "activity": ""})
            except Exception:
                pass
            return

    # --- Run the activity timer ---
    await asyncio.sleep(activity.duration_seconds)

    elapsed = _time.time() - start_time

    # --- Fetch fresh agent state before applying effects ---
    agent = service.get_town_agent_by_id(agent_id)
    if not agent:
        logger.warning("Agent %s not found when completing activity %s", agent_id, activity.name)
        return

    current_energy = int(agent.get("energy", 100))
    current_mood = parse_mood(agent.get("mood"))
    traits = agent.get("traits", "")

    new_energy, new_mood, interrupted = apply_activity_effect(activity, current_energy, current_mood, traits, elapsed)

    # --- Persist updated mood/energy and clear activity ---
    service.update_agent_mood_energy(agent_id, new_mood, new_energy)
    service.update_agent_activity(agent_id, None, None)
    logger.info(
        "[agent:%s] Activity COMPLETE: %s (mood %d→%d, energy %d→%d, conn=%s)",
        agent_id[:8],
        activity.name,
        current_mood,
        new_mood,
        current_energy,
        new_energy,
        connection_id[:12],
    )

    # Clear activity state in game server
    try:
        await gs_client.send_command(
            agent_id,
            {
                "cmd": "set_activity",
                "activity": "",
            },
        )
    except Exception as exc:
        logger.warning("Failed to clear activity in game server for %s: %s", agent_id, exc)

    # --- Send outcome event to agent ---
    event_name = "activity_interrupted" if interrupted else "activity_complete"
    try:
        mgmt.send_message(
            connection_id,
            {
                "event": event_name,
                "activity": activity.name,
                "mood_delta": new_mood - current_mood,
                "energy_delta": new_energy - current_energy,
                "new_mood": new_mood,
                "new_energy": new_energy,
                "interrupted": interrupted,
            },
        )
        logger.info(
            "Activity %s %s for agent %s (mood %d→%d, energy %d→%d)",
            activity.name,
            "interrupted" if interrupted else "complete",
            agent_id,
            current_mood,
            new_mood,
            current_energy,
            new_energy,
        )
    except Exception as exc:
        logger.warning("Failed to send %s to %s: %s", event_name, agent_id, exc)


def _clear_activity_and_notify(
    service: TownService,
    mgmt,
    connection_id: str,
    agent_id: str,
    activity_name: str,
    reason: str,
) -> None:
    """Clear the agent's busy state and send an activity_rejected event.

    Used when navigation fails before the activity timer even starts.
    """
    try:
        service.update_agent_activity(agent_id, None, None)
    except Exception as exc:
        logger.warning("Failed to clear activity for %s: %s", agent_id, exc)
    try:
        mgmt.send_message(
            connection_id,
            {"event": "activity_rejected", "activity": activity_name, "reason": reason},
        )
    except Exception as exc:
        logger.warning("Failed to send activity_rejected for navigation failure %s: %s", agent_id, exc)


async def complete_activity_from_startup(agent_id: str, activity, elapsed: float) -> None:
    """Complete an activity that should have finished while the backend was down."""

    service = TownService()
    agent = service.get_town_agent_by_id(agent_id)
    if not agent:
        return

    current_energy = int(agent.get("energy", 100))
    current_mood = parse_mood(agent.get("mood"))
    traits = agent.get("traits", "")

    new_energy, new_mood, interrupted = apply_activity_effect(activity, current_energy, current_mood, traits, elapsed)

    service.update_agent_mood_energy(agent_id, new_mood, new_energy)
    service.update_agent_activity(agent_id, None, None)

    # Clear activity in game server
    gs_client = get_game_server_client()
    try:
        await gs_client.send_command(agent_id, {"cmd": "set_activity", "activity": ""})
    except Exception:
        pass

    # Notify daemon
    event_name = "activity_interrupted" if interrupted else "activity_complete"
    _send_to_daemon(
        agent_id,
        {
            "event": event_name,
            "activity": activity.name,
            "mood_delta": new_mood - current_mood,
            "energy_delta": new_energy - current_energy,
            "new_mood": new_mood,
            "new_energy": new_energy,
            "interrupted": interrupted,
        },
    )

    logger.info("Startup: completed overdue activity %s for agent %s", activity.name, agent_id)


async def restart_activity_timer(agent_id: str, activity, remaining: float) -> None:
    """Restart an activity timer for the remaining duration after backend restart."""
    import asyncio

    await asyncio.sleep(remaining)

    service = TownService()
    agent = service.get_town_agent_by_id(agent_id)
    if not agent or agent.get("currentActivity") != activity.name:
        return  # Activity was cancelled or changed

    current_energy = int(agent.get("energy", 100))
    current_mood = parse_mood(agent.get("mood"))
    traits = agent.get("traits", "")

    new_energy, new_mood, interrupted = apply_activity_effect(
        activity, current_energy, current_mood, traits, activity.duration_seconds
    )

    service.update_agent_mood_energy(agent_id, new_mood, new_energy)
    service.update_agent_activity(agent_id, None, None)

    # Clear activity in game server
    gs_client = get_game_server_client()
    try:
        await gs_client.send_command(agent_id, {"cmd": "set_activity", "activity": ""})
    except Exception:
        pass

    # Notify daemon
    event_name = "activity_interrupted" if interrupted else "activity_complete"
    _send_to_daemon(
        agent_id,
        {
            "event": event_name,
            "activity": activity.name,
            "mood_delta": new_mood - current_mood,
            "energy_delta": new_energy - current_energy,
            "new_mood": new_mood,
            "new_energy": new_energy,
            "interrupted": interrupted,
        },
    )

    logger.info("Startup: timer completed activity %s for agent %s", activity.name, agent_id)
