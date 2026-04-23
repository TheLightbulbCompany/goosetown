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
"""

from __future__ import annotations

import asyncio
import logging

from core.services.daemon_bridge import _make_agent_event_callback, _send_to_daemon
from core.services.game_server_client import get_game_server_client
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
                static_png = build_static_spritesheet_from_rotations(rotation_bytes)
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
                service.update_agent_state(
                    agent_id,
                    sprite_ready=True,
                    sprite_url=static_cdn_url,
                    sprite_animations=initial_sprite_animations,
                    sprite_status="static_ready",
                    onboarding_phase="static",
                )
                logger.info("[agent:%s] Phase B complete: static sprite ready", agent_id)

                # Hot-path cache sync (see _send_to_daemon).
                get_town_agent_ws_manager().update_onboarding_phase(str(agent_id), "static")

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

        signup_job_ids: dict[str, list[str]] = {}
        try:
            signup_job_ids = await pxl.queue_signup_animations(char_id, on_progress=_anim_progress)
        except Exception:
            logger.exception("Signup animations failed for %s", agent_id)

        # Download and resolve animation frames via API cross-reference
        service.update_agent_state(agent_id, sprite_status="downloading")
        _send_progress("Downloading sprites...")

        raw_animations = await pxl.fetch_animation_spritesheets(char_id)

        # Map PixelLab API names to our friendly names
        template_id_to_friendly: dict[str, str] = {
            cfg["template_id"]: name for name, cfg in ANIMATION_REGISTRY.items() if cfg.get("template_id")
        }

        full_sprite_animations: dict = dict(initial_sprite_animations)

        # Process template animations from ZIP (walk, idle)
        for pixellab_name, direction_frames in raw_animations.items():
            friendly = template_id_to_friendly.get(pixellab_name, pixellab_name)
            sheet = composite_animation_spritesheet(direction_frames)
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

        # Process custom animations from job results (talking, etc.)
        # Custom animate-with-text-v2 results are NOT in the ZIP — they're
        # stored in the job's last_response as frame images.
        for anim_name, job_ids in signup_job_ids.items():
            if anim_name in full_sprite_animations:
                continue  # Already resolved from ZIP
            config = ANIMATION_REGISTRY.get(anim_name, {})
            if config.get("type") != "custom" or not job_ids:
                continue
            # Each job is one direction (south, west, east, north)
            directions = ["south", "west", "east", "north"]
            direction_frames: dict[str, list[bytes]] = {}
            for i, jid in enumerate(job_ids):
                if i >= len(directions):
                    break
                frames = await pxl.get_v2_animation_frames(jid)
                if frames:
                    direction_frames[directions[i]] = frames
            if direction_frames:
                sheet = composite_animation_spritesheet(direction_frames)
                if sheet:
                    png_bytes, frame_count = sheet
                    s3_key = await asyncio.to_thread(
                        upload_sprite_to_s3,
                        png_bytes,
                        str(agent_id),
                        settings.SPRITE_S3_BUCKET,
                        f"{anim_name}.png",
                    )
                    full_sprite_animations[anim_name] = {
                        "url": f"{settings.SPRITE_CDN_URL}/{s3_key}",
                        "frameCount": frame_count,
                        "fps": int(config.get("fps", 6)),
                    }
                    logger.info(
                        "[agent:%s] Custom animation %s: %d directions, %d frames",
                        agent_id,
                        anim_name,
                        len(direction_frames),
                        frame_count,
                    )

        if not full_sprite_animations:
            logger.warning("[agent:%s] No animations resolved — agent remains on static sprite", agent_id)
            service.update_agent_state(agent_id, onboarding_phase="alive")
            # Hot-path cache sync (see _send_to_daemon).
            get_town_agent_ws_manager().update_onboarding_phase(str(agent_id), "alive")
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

        # Flip to alive
        service.update_agent_state(agent_id, onboarding_phase="alive")
        # Hot-path cache sync (see _send_to_daemon).
        get_town_agent_ws_manager().update_onboarding_phase(str(agent_id), "alive")
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
            pass

    except Exception:
        logger.exception("Sprite generation failed for agent %s", agent_id)
        try:
            TownService().update_agent_state(agent_id, sprite_status=None)
        except Exception:
            pass
