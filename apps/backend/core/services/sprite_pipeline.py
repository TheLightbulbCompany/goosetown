"""Progressive sprite generation pipeline.

Extracted from ``routers/town.py`` to break the circular import with
``core.services.sprite_queue`` — the queue consumer used a deferred
``from routers.town import _sprite_pipeline_task`` inside the loop body
because the top-level cycle (routers.town ⇄ sprite_queue) would have
failed to resolve at import time.

Now sprite_queue imports this module at the top level, and routers.town
imports ``_sprite_pipeline_task`` from here when registration kicks off
a pipeline. Both daemon-messaging helpers (``_send_to_daemon`` and
``_make_agent_event_callback``) now also live in a dedicated module
(``core.services.daemon_bridge``) so the pipeline no longer defers
imports back into the routers layer.

Pipeline phases:

* **Phase A — Create**: call PixelLab ``create-character-with-4-directions``
  and poll the creation job until it completes (up to ~5 min).
* **Phase B — Static**: fetch all 4 rotation PNGs, composite them into a
  static spritesheet, upload to S3, set ``spriteReady=True`` and
  ``onboardingPhase='static'``, and connect the agent to the game server
  so they appear in town right away (even without walk animation).
* **Phase C — Animate**: queue signup animations (walk, idle, talking),
  download the resulting ZIP + custom-animation frames, composite each
  into its own spritesheet, upload, and flip ``onboardingPhase='alive'``.

The module also owns ``resume_sprite_generation`` — a companion used by
``main.py:_reconnect_active_agents`` on backend startup when an agent's
``sprite_status`` is still ``creating`` / ``animating`` / ``downloading``
from before the restart. It picks up whichever phase is applicable and
runs to completion so agents don't end up stuck mid-pipeline.
"""

from __future__ import annotations

import asyncio
import logging

from core.services.daemon_bridge import _make_agent_event_callback, _send_to_daemon
from core.services.game_server_client import get_game_server_client
from core.services.phase_lattice import advance_phase
from core.services.town_agent_ws import get_town_agent_ws_manager
from core.services.town_service import TownService

logger = logging.getLogger(__name__)


async def _sprite_pipeline_task(agent_id: str, display_name: str, appearance: str) -> None:
    """Run the full sprite generation pipeline for a single agent.

    Args:
        agent_id: Agent UUID (string).
        display_name: Human-readable name, used in game server connect
            payload.
        appearance: Free-form PixelLab description ("a pixel-art goose
            in a suit", etc.).

    Failures in any phase log + return cleanly so the sprite queue can
    move on to the next job. The agent's ``sprite_status`` field tracks
    progress (``creating`` → ``building_static`` → ``static_ready`` →
    ``animating`` → ``downloading`` → ``complete``) and the daemon
    receives ``sprite_progress`` events throughout.
    """
    from core.config import settings
    from core.services.management_api_client import ManagementApiClient
    from core.services.pixellab_service import ANIMATION_REGISTRY, PixelLabService
    from core.services.sprite_storage import (
        build_static_spritesheet_from_rotations,
        composite_animation_spritesheet,
        upload_sprite_to_s3,
    )

    pxl = PixelLabService()
    service = TownService()

    def _send_progress(status: str, **extra) -> None:
        msg = {"event": "sprite_progress", "status": status}
        msg.update(extra)
        ok = _send_to_daemon(str(agent_id), msg)
        logger.info("[KEEPALIVE] agent=%s ok=%s status=%s", str(agent_id)[:8], ok, status[:50])

    try:
        # ---- Phase A: Create character ----
        service.update_agent_state(agent_id, sprite_status="creating")
        _send_progress("Creating your character...")
        try:
            char_id, creation_job_id = await pxl.create_character(description=appearance)
        except Exception:
            logger.exception("Character creation failed for agent %s", agent_id)
            service.update_agent_state(agent_id, sprite_status=None)
            return

        service.update_agent_state(agent_id, pixellab_character_id=char_id)
        logger.info("PixelLab returned char_id=%s job=%s for %s", char_id, creation_job_id, agent_id)

        # Poll creation job until complete (up to 5 min)
        if creation_job_id:
            for attempt in range(30):
                await asyncio.sleep(10)
                try:
                    job = await pxl.get_job_status(creation_job_id)
                    status_val = job.get("status", "unknown")
                    _send_progress(f"Creating your character sprite ({status_val})...")
                    if status_val == "completed":
                        break
                except Exception as e:
                    logger.warning("Failed to check creation job: %s", e)
            else:
                logger.warning("Character creation timed out for %s", char_id)

        # ---- Phase B: Build static spritesheet and mark agent ready ----
        service.update_agent_state(agent_id, sprite_status="building_static")
        _send_progress("Sketching your initial look...")

        rotation_bytes = await pxl.fetch_all_rotations(char_id)
        static_cdn_url = ""
        initial_sprite_animations: dict = {}

        if rotation_bytes:
            try:
                static_png = await asyncio.to_thread(build_static_spritesheet_from_rotations, rotation_bytes)
                s3_key = await asyncio.to_thread(
                    upload_sprite_to_s3, static_png, str(agent_id), settings.SPRITE_S3_BUCKET
                )
            except Exception:
                logger.exception("Static spritesheet build/upload failed for %s", agent_id)
                s3_key = ""

            if s3_key:
                static_cdn_url = f"{settings.SPRITE_CDN_URL}/{s3_key}"
                initial_sprite_animations = {
                    "walk": {
                        "url": static_cdn_url,
                        "frameCount": 6,
                        "fps": 8,
                    },
                }
                # Phase advance via the lattice helper — atomically writes
                # to DDB + ws_manager cache + enforces the monotonic
                # ordering. Bundles the sibling fields (sprite_*) into
                # the same DDB write so they don't fan out into multiple
                # round-trips.
                advance_phase(
                    agent_id,
                    "static",
                    service=service,
                    sprite_ready=True,
                    sprite_url=static_cdn_url,
                    sprite_animations=initial_sprite_animations,
                    sprite_status="static_ready",
                )
                logger.info("[agent:%s] Phase B complete: static sprite ready", agent_id)

                # Connect to game server immediately with static sprite
                gs_client = get_game_server_client()
                _ws_mgr = get_town_agent_ws_manager()
                _conn = _ws_mgr.get_by_agent_id(str(agent_id))
                if _conn:
                    try:
                        agent_record = service.get_town_agent_by_id(str(agent_id))
                        await gs_client.connect_agent(
                            agent_id=str(agent_id),
                            display_name=display_name,
                            sprite_url=static_cdn_url,
                            location=agent_record.get("currentLocation", "plaza") if agent_record else "plaza",
                            context=_conn.location_context or "town",
                            user_id=_conn.user_id,
                            sprite_animations=initial_sprite_animations,
                            on_event=_make_agent_event_callback(),
                        )
                        logger.info("[agent:%s] Connected to game server with static sprite", agent_id)
                    except Exception:
                        logger.exception("Failed to connect agent %s with static sprite", agent_id)
        else:
            logger.warning("[agent:%s] No rotations returned — skipping Phase B static sprite", agent_id)

        # ---- Phase C: Generate signup animations (walk, idle, talking) ----
        service.update_agent_state(agent_id, sprite_status="animating")
        _send_progress("Bringing you to life (animating walk, idle, talking)...")

        async def _anim_progress(status_str: str) -> None:
            _send_progress(status_str)

        # generate_animation (via queue_signup_animations) now returns the
        # composited per-direction frames directly — no separate ZIP download
        # or per-job v2 fetch. Each signup animation (walk, idle, talking) comes
        # back as {direction: [png_bytes]} ready to composite + upload.
        signup_frames: dict[str, dict[str, list[bytes]]] = {}
        try:
            signup_frames = await pxl.queue_signup_animations(char_id, on_progress=_anim_progress)
        except Exception:
            logger.exception("Signup animations failed for %s", agent_id)

        service.update_agent_state(agent_id, sprite_status="downloading")
        _send_progress("Compositing sprites...")

        full_sprite_animations: dict = dict(initial_sprite_animations)

        for friendly, direction_frames in signup_frames.items():
            if not direction_frames:
                continue
            sheet = await asyncio.to_thread(composite_animation_spritesheet, direction_frames)
            if sheet:
                png_bytes, frame_count = sheet
                s3_key = await asyncio.to_thread(
                    upload_sprite_to_s3,
                    png_bytes,
                    str(agent_id),
                    settings.SPRITE_S3_BUCKET,
                    f"{friendly}.png",
                )
                registry_entry = ANIMATION_REGISTRY.get(friendly, {})
                full_sprite_animations[friendly] = {
                    "url": f"{settings.SPRITE_CDN_URL}/{s3_key}",
                    "frameCount": frame_count,
                    "fps": int(registry_entry.get("fps", 8)),
                }
                logger.info(
                    "[agent:%s] Signup animation %s: %d directions, %d frames",
                    agent_id,
                    friendly,
                    len(direction_frames),
                    frame_count,
                )

        if not full_sprite_animations:
            logger.warning("[agent:%s] No animations resolved — agent remains on static sprite", agent_id)
            advance_phase(agent_id, "alive", service=service)
            return

        # Real walk URL overrides the static placeholder
        real_walk_url = full_sprite_animations.get("walk", {}).get("url", static_cdn_url)

        service.update_agent_state(
            agent_id,
            sprite_ready=True,
            sprite_url=real_walk_url,
            sprite_animations=full_sprite_animations,
            sprite_status="complete",
        )
        logger.info("[agent:%s] Phase C complete: %d animations", agent_id, len(full_sprite_animations))

        # Push updated sprites to game server
        try:
            gs_client_c = get_game_server_client()
            await gs_client_c.update_agent_sprites(str(agent_id), full_sprite_animations)
        except Exception:
            logger.exception("Failed to push sprite update for %s", agent_id)

        # If agent was not connected in Phase B, connect now
        if not static_cdn_url:
            gs_client = get_game_server_client()
            _ws_mgr = get_town_agent_ws_manager()
            _conn = _ws_mgr.get_by_agent_id(str(agent_id))
            if _conn:
                try:
                    agent_record = service.get_town_agent_by_id(str(agent_id))
                    await gs_client.connect_agent(
                        agent_id=str(agent_id),
                        display_name=display_name,
                        sprite_url=real_walk_url,
                        location=agent_record.get("currentLocation", "plaza") if agent_record else "plaza",
                        context=_conn.location_context or "town",
                        user_id=_conn.user_id,
                        sprite_animations=full_sprite_animations,
                        on_event=_make_agent_event_callback(),
                    )
                except Exception:
                    logger.exception("Failed to connect agent %s with animations", agent_id)

        # Flip to alive (single seam — atomic DDB + cache via the lattice).
        advance_phase(agent_id, "alive", service=service)
        logger.info("[agent:%s] Agent is alive", agent_id)

        # Notify daemon that sprite is fully ready
        try:
            _ws_mgr = get_town_agent_ws_manager()
            _conn = _ws_mgr.get_by_agent_id(str(agent_id))
            if _conn:
                mgmt = ManagementApiClient()
                mgmt.send_message(
                    _conn.connection_id,
                    {
                        "event": "sprite_ready",
                        "agent_id": str(agent_id),
                        "sprite_url": real_walk_url,
                    },
                )
        except Exception:
            # sprite_ready push is best-effort — daemon picks up the
            # spriteUrl from /apartment polling within 2s anyway.
            logger.debug(
                "sprite_ready push to daemon failed for %s",
                agent_id,
                exc_info=True,
            )

    except Exception:
        logger.exception("Sprite generation failed for agent %s", agent_id)
        try:
            TownService().update_agent_state(agent_id, sprite_status=None)
        except Exception:
            # If the cleanup status-clear also fails, the row will keep
            # its stale phase but the next pipeline run will overwrite it.
            logger.debug(
                "Failed to clear sprite_status for %s after pipeline failure",
                agent_id,
                exc_info=True,
            )


async def resume_sprite_generation(
    agent_id: str,
    character_id: str | None,
    appearance: str,
    display_name: str,
    current_status: str,
) -> None:
    """Resume an interrupted sprite generation from a specific phase.

    Called from startup recovery (``main.py:_reconnect_active_agents``)
    when an agent has a non-complete ``spriteStatus`` — meaning the
    pipeline was mid-flight when the backend restarted. Picks up at
    whichever phase is applicable and runs to completion.
    """
    from core.config import settings
    from core.services.pixellab_service import ANIMATION_REGISTRY, PixelLabService
    from core.services.sprite_storage import (
        composite_animation_spritesheet,
        upload_sprite_to_s3,
    )

    pxl = PixelLabService()
    service = TownService()

    # Recover the existing spriteUrl from DynamoDB so we don't overwrite it
    # with "" if Phase C can't find a "walk" key in the animation dict.
    existing_agent = service.get_town_agent_by_id(agent_id)
    existing_sprite_url = (existing_agent or {}).get("spriteUrl", "")

    def _progress(status: str) -> None:
        _send_to_daemon(agent_id, {"event": "sprite_progress", "status": status})

    try:

        async def _anim_progress(status: str) -> None:
            _progress(status)

        char_id = character_id

        # Phase: creating
        if current_status == "creating":
            if not char_id:
                _progress("Recreating character...")
                char_id, creation_job_id = await pxl.create_character(description=appearance)
                service.update_agent_state(agent_id, pixellab_character_id=char_id)
                if creation_job_id:
                    await pxl.wait_for_jobs([creation_job_id], on_progress=_anim_progress, progress_label="creation")
            service.update_agent_state(agent_id, sprite_status="animating")
            current_status = "animating"

        # Phase: animating vs downloading — two DISTINCT resume entry points
        # that share the composite+upload+advance tail but differ ONLY in where
        # the per-direction frames come from:
        #
        #   * "animating" (reached after a fresh "creating" regenerate): GENERATE
        #     via queue_signup_animations — this costs PixelLab credits and is
        #     the intended behavior for /admin/regenerate-sprite.
        #   * "downloading" (the /admin/recomposite-sprites entry): RE-DOWNLOAD
        #     the character's EXISTING animations via fetch_animation_spritesheets
        #     — no generation, no credits. Recomposite's whole point is to
        #     re-run the gray-strip composite over already-generated frames, so
        #     it MUST NOT call queue_signup_animations.
        #
        # Both sources return the same {anim_name: {direction: [png_bytes]}}
        # shape, so only the SOURCE differs — the loop below is identical.
        if char_id and current_status in ("animating", "downloading"):
            if current_status == "animating":
                _progress("Resuming animations...")
                anim_source = await pxl.queue_signup_animations(char_id, on_progress=_anim_progress)
            else:  # downloading == recomposite: re-download existing, never regenerate
                _progress("Re-downloading sprites...")
                anim_source = await pxl.fetch_animation_spritesheets(char_id)

            service.update_agent_state(agent_id, sprite_status="downloading")
            current_status = "downloading"

            _progress("Compositing sprites...")

            sprite_animations: dict = {}
            for friendly, direction_frames in anim_source.items():
                if not direction_frames:
                    continue
                sheet = await asyncio.to_thread(composite_animation_spritesheet, direction_frames)
                if sheet:
                    png_bytes, frame_count = sheet
                    s3_key = await asyncio.to_thread(
                        upload_sprite_to_s3,
                        png_bytes,
                        agent_id,
                        settings.SPRITE_S3_BUCKET,
                        f"{friendly}.png",
                    )
                    registry_entry = ANIMATION_REGISTRY.get(friendly, {})
                    sprite_animations[friendly] = {
                        "url": f"{settings.SPRITE_CDN_URL}/{s3_key}",
                        "frameCount": frame_count,
                        "fps": int(registry_entry.get("fps", 8)),
                    }

            walk_url = sprite_animations.get("walk", {}).get("url", existing_sprite_url)
            # Resume path was missing the cache mirror entirely — phase
            # got written to DDB but the in-memory ws_manager cache stayed
            # stale, so a daemon connecting to a recovered agent could
            # see the old phase. The lattice helper closes that gap.
            advance_phase(
                agent_id,
                "alive",
                service=service,
                sprite_ready=True,
                sprite_url=walk_url,
                sprite_animations=sprite_animations,
                sprite_status="complete",
            )
            logger.info("[resume:%s] Complete: %d animations", agent_id, len(sprite_animations))

            # Connect to game server
            gs_client = get_game_server_client()
            agent = service.get_town_agent_by_id(agent_id)
            if agent and agent.get("spriteReady"):
                await gs_client.connect_agent(
                    agent_id=agent_id,
                    display_name=display_name,
                    sprite_url=agent.get("spriteUrl") or "",
                    location=agent.get("currentLocation", "plaza"),
                    context=agent.get("locationContext", "town"),
                    user_id=agent.get("userId", ""),
                    sprite_animations=agent.get("spriteAnimations", {}),
                    on_event=_make_agent_event_callback(),
                )

            _send_to_daemon(
                agent_id,
                {
                    "event": "sprite_ready",
                    "agent_id": agent_id,
                    "sprite_url": agent.get("spriteUrl", "") if agent else "",
                },
            )

        logger.info("Sprite recovery completed for agent %s", agent_id)

    except Exception:
        logger.exception("Failed to resume sprite generation for %s", agent_id)
        service.update_agent_state(agent_id, sprite_status=None)
