"""Progressive sprite generation pipeline (resume-aware, sink-driven).

The pipeline is **worker-runnable**: it makes no direct calls into
backend-process state (game-server client, in-memory ws_manager). All such
side effects go through a ``SpriteEventSink`` — the dedicated SQS sprite
worker passes an HTTP sink that POSTs ``/town/internal/sprite-event``;
in-backend callers pass ``LocalSpriteEventSink`` which applies the effects
in-process via ``core.services.sprite_update_apply``.

The agent ROW in DynamoDB is the source of truth: ``run_signup_pipeline``
reads display_name / appearance / pixellabCharacterId / spriteStatus from
the row (any copies in the SQS message are advisory only) and branches on
them, so SQS redelivery, standard-queue duplicates, and the worker boot
sweep all converge on the same idempotent code path.

Pipeline phases:

* **Phase A — Create** (only when the row has no ``pixellabCharacterId``):
  call PixelLab ``create-character-with-4-directions`` and poll the creation
  job until it completes (up to ~5 min).
* **Phase B — Static** (skipped when a spriteUrl already exists and the
  status shows Phase B finished): fetch all 4 rotation PNGs, composite them
  into a static spritesheet, upload to S3, set ``spriteReady=True`` and
  ``onboardingPhase='static'``, then ``sink.sprite_updated('static_ready')``
  so the agent appears in town right away (even without walk animation).
* **Phase C — Animate**: queue signup animations (walk only — idle/talking
  generate lazily via activity layer / conversation-start hook), composite
  walk frames into its own spritesheet, upload, flip
  ``onboardingPhase='alive'``, then ``sink.sprite_updated('animations_updated')``.

Failure contract: exceptions PROPAGATE (the SQS worker releases the message
for redelivery) and ``spriteStatus`` is left where it was — it is the resume
pointer. This includes the case where generation yields no animations at all
(``RuntimeError`` raised so the SQS worker retries rather than silently
completing without a sprite).

The module also owns ``resume_sprite_generation`` — used by the admin
recomposite ("downloading") endpoint.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from core.services.daemon_bridge import _make_agent_event_callback, _send_to_daemon
from core.services.game_server_client import get_game_server_client
from core.services.phase_lattice import advance_phase
from core.services.pixellab_service import PixelLabCreditError
from core.services.town_service import TownService

logger = logging.getLogger(__name__)


# Per-agent in-flight guard shared by ALL generation entry points
# (``run_signup_pipeline``, ``run_animation_job`` and
# ``resume_sprite_generation``). Only one generation may run per agent at a
# time — a second concurrent call for the same ``agent_id`` returns early
# instead of generating a duplicate set of sheets and racing the
# phase-lattice monotonic guard (#85).
#
# Why a plain ``set`` is race-free: asyncio is single-threaded, so the
# ``if agent_id in _resume_in_flight`` check and the subsequent ``.add`` run
# without an intervening ``await`` — no other coroutine can interleave between
# them. The two direct callers that bypass the queue are the admin
# ``POST /town/admin/regenerate-sprite/{id}`` endpoint and startup recovery
# (``main.py:_reconnect_active_agents``); before this guard they could both
# call ``resume_sprite_generation`` for the same resident at once, which is
# exactly the race #85 surfaced live (the admin run bumped spriteStatus to
# "animating" in DDB, then the worldgm backend's startup recovery saw that and
# fired its own resume while the admin run was still going).
_resume_in_flight: set[str] = set()

# spriteStatus values that mean Phase B (static sprite) has not finished yet.
_PRE_STATIC_STATUSES = (None, "queued", "creating", "building_static")


class SpriteEventSink(Protocol):
    """Where pipeline side effects go. The sprite worker passes an HTTP sink
    that POSTs /town/internal/sprite-event; in-backend callers pass
    LocalSpriteEventSink."""

    async def progress(self, agent_id: str, status: str) -> None: ...

    async def sprite_updated(self, agent_id: str, kind: str) -> None: ...


class LocalSpriteEventSink:
    """In-process sink for pipeline code that runs inside the backend."""

    async def progress(self, agent_id: str, status: str) -> None:
        _send_to_daemon(agent_id, {"event": "sprite_progress", "status": status})

    async def sprite_updated(self, agent_id: str, kind: str) -> None:
        from core.services.sprite_update_apply import apply_sprite_update

        await apply_sprite_update(agent_id, kind)


async def run_signup_pipeline(
    agent_id: str,
    *,
    sink: SpriteEventSink,
    fallback_display_name: str = "",
    fallback_appearance: str = "",
) -> None:
    """Run (or resume) the signup sprite pipeline for one agent.

    Resume-aware: reads the agent row and branches on
    (``spriteStatus``, ``pixellabCharacterId``, ``spriteUrl``) so a redelivered
    SQS message picks up exactly where the dead run stopped:

    * no char_id → full run: create → poll → Phase B static → Phase C walk.
    * char_id but Phase B unfinished → skip creation, resume from Phase B.
    * char_id + spriteUrl + status past Phase B → resume at Phase C only.
    * ``spriteStatus == "complete"`` and spriteReady → no-op (duplicate
      delivery).

    Raises on transient failure (PixelLab/S3/DDB) — the worker releases the
    message for redelivery and ``spriteStatus`` stays put as the resume
    pointer. Also raises ``RuntimeError`` when generation yields no animations
    at all (Phase B returned no rotations AND Phase C returned no frames) so
    the SQS worker retries rather than silently completing with no sprite.
    Raises ``RuntimeError`` even in the no-exception empty-output case: when
    ``queue_signup_animations`` returns normally but with empty frame dicts
    for one or more signup animations — the Phase B static placeholder in
    ``initial_sprite_animations`` would otherwise let the composite loop
    silently complete with ``spriteStatus="complete"`` on the old static sheet
    without the SQS retry/DLQ path ever seeing the failure.
    Never clears ``spriteStatus`` on failure.

    ``fallback_display_name`` and ``fallback_appearance`` are one-shot fallbacks
    from the SQS message body (populated by legacy producers or enqueue_signup).
    They are used ONLY when the agent row itself lacks the field, and are then
    promoted onto the row so that mid-pipeline crashes + redelivery and the boot
    sweep (which reads only the row) converge correctly on the same code path.
    The row is still the canonical source of truth; these fallbacks paper over
    the register-path gap where ``opt_in`` did not persist ``appearance``.
    """
    from core.config import settings
    from core.services.pixellab_service import ANIMATION_REGISTRY, SIGNUP_ANIMATIONS, PixelLabService
    from core.services.sprite_storage import (
        build_static_spritesheet_from_rotations,
        composite_animation_spritesheet,
        upload_sprite_to_s3,
    )

    if agent_id in _resume_in_flight:
        logger.info("Sprite generation already in flight for %s — skipping duplicate", agent_id)
        return
    _resume_in_flight.add(agent_id)

    try:
        service = TownService()

        # The row is the source of truth — the SQS message's display_name /
        # appearance copies are advisory only.
        agent = service.get_town_agent_by_id(str(agent_id))
        if not agent:
            logger.warning("run_signup_pipeline: agent %s not found — dropping job", agent_id)
            return

        status = agent.get("spriteStatus")
        if status == "complete" and agent.get("spriteReady"):
            logger.info("run_signup_pipeline: agent %s already complete — duplicate delivery", agent_id)
            return

        # Both attribute spellings exist in older rows (camelCase is current).
        char_id = agent.get("pixellabCharacterId") or agent.get("pixellab_character_id")

        # Row is the source of truth; fall back to the SQS message's copy when
        # the row lacks appearance (e.g. rows created via opt_in before the
        # register-path appearance-persist fix landed).  Promote onto the row
        # so that mid-pipeline crash + redelivery and the boot sweep both
        # converge on the correct code path.
        appearance = agent.get("appearance") or fallback_appearance
        if appearance and not agent.get("appearance"):
            service.update_agent_state(agent_id, appearance=appearance)
            logger.info("run_signup_pipeline: promoted fallback appearance onto row for %s", agent_id)

        pxl = PixelLabService()

        async def _anim_progress(status_str: str) -> None:
            await sink.progress(agent_id, status_str)

        # ---- Phase A: Create character (only when the row has no char_id) ----
        if not char_id:
            if not appearance:
                logger.warning("run_signup_pipeline: agent %s has no appearance — cannot create character", agent_id)
                # Clear the in-progress status so a future register attempt
                # (which now persists appearance) can re-claim and re-enqueue.
                # Conditional release covers the normal "queued" claim; the
                # unconditional fallback covers pre-migration rows stuck at
                # e.g. "creating" with neither char_id nor appearance — the
                # boot sweep re-enqueues those as-is, and without this clear
                # they'd loop sweep→no-op→delete forever. Safe to clear: this
                # is the only worker process and the per-agent in-flight guard
                # means no other pipeline is running for this agent right now.
                # Best-effort: swallow DDB errors so the worker doesn't treat
                # a terminally un-processable message as retriable.
                try:
                    if not service.release_sprite_claim(agent_id):
                        service.update_agent_state(agent_id, sprite_status=None)
                        logger.info(
                            "run_signup_pipeline: cleared stuck non-queued status for %s (no appearance)",
                            agent_id,
                        )
                except Exception:
                    logger.warning(
                        "run_signup_pipeline: best-effort claim release failed for %s", agent_id, exc_info=True
                    )
                return

            service.update_agent_state(agent_id, sprite_status="creating")
            await sink.progress(agent_id, "Creating your character...")

            char_id, creation_job_id = await pxl.create_character(description=appearance)
            service.update_agent_state(agent_id, pixellab_character_id=char_id)
            logger.info("PixelLab returned char_id=%s job=%s for %s", char_id, creation_job_id, agent_id)

            # Poll creation job until complete (up to 5 min)
            if creation_job_id:
                for _attempt in range(30):
                    await asyncio.sleep(10)
                    try:
                        job = await pxl.get_job_status(creation_job_id)
                        status_val = job.get("status", "unknown")
                        await sink.progress(agent_id, f"Creating your character sprite ({status_val})...")
                        if status_val == "completed":
                            break
                    except Exception as e:
                        logger.warning("Failed to check creation job: %s", e)
                else:
                    logger.warning("Character creation timed out for %s", char_id)

        # Seed Phase C inputs from the EXISTING row so a resume that skips
        # Phase B doesn't lose the static fallback already uploaded.
        static_cdn_url: str = agent.get("spriteUrl") or ""
        initial_sprite_animations: dict = dict(agent.get("spriteAnimations") or {})

        # ---- Phase B: Build static spritesheet and mark agent ready ----
        # Runs unless the row shows Phase B already finished (spriteUrl present
        # AND status past the pre-static stretch).
        if not agent.get("spriteUrl") or status in _PRE_STATIC_STATUSES:
            service.update_agent_state(agent_id, sprite_status="building_static")
            await sink.progress(agent_id, "Sketching your initial look...")

            rotation_bytes = await pxl.fetch_all_rotations(char_id)
            if rotation_bytes:
                static_png = await asyncio.to_thread(build_static_spritesheet_from_rotations, rotation_bytes)
                s3_key = await asyncio.to_thread(
                    upload_sprite_to_s3, static_png, str(agent_id), settings.SPRITE_S3_BUCKET
                )

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

                # Backend-process effects (game-server connect/push, lattice
                # cache refresh) happen behind the sink.
                await sink.sprite_updated(agent_id, "static_ready")
            else:
                logger.warning("[agent:%s] No rotations returned — skipping Phase B static sprite", agent_id)

        # ---- Phase C: Generate signup animations (walk only — idle/talking go lazy) ----
        service.update_agent_state(agent_id, sprite_status="animating")
        await sink.progress(agent_id, "Bringing you to life (animating walk)...")

        # generate_animation (via queue_signup_animations) returns the
        # composited per-direction frames directly — no separate ZIP download
        # or per-job v2 fetch. Each signup animation (walk only — idle/talking
        # are lazy) comes back as {direction: [png_bytes]} ready to composite + upload.
        signup_frames: dict[str, dict[str, list[bytes]]] = await pxl.queue_signup_animations(
            char_id, on_progress=_anim_progress
        )

        # queue_signup_animations propagates exceptions, but an all-jobs-
        # timeout returns empty frame dicts without raising. An empty walk
        # must not silently complete on the Phase B static placeholder —
        # raise so SQS redelivery retries and 3 strikes reach the DLQ.
        missing = [name for name in SIGNUP_ANIMATIONS if not signup_frames.get(name)]
        if missing:
            raise RuntimeError(f"signup animation(s) {missing} produced no frames for agent {agent_id}")

        service.update_agent_state(agent_id, sprite_status="downloading")
        await sink.progress(agent_id, "Compositing sprites...")

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
            # Nothing came back from Phase B OR Phase C (no rotations AND no
            # walk frames) — the agent would be left with no sprite at all.
            # Raise so the SQS worker releases the message: redelivery retries
            # the whole resume-aware pipeline, and 3 strikes route the job to
            # the DLQ (alarmed). Deliberately NOT advance_phase("alive") — the
            # job is not done.
            raise RuntimeError(f"sprite pipeline produced no animations for agent {agent_id}")

        # Real walk URL overrides the static placeholder; falls back to the
        # existing spriteUrl (never "") when resuming past Phase B.
        real_walk_url = full_sprite_animations.get("walk", {}).get("url", static_cdn_url)

        service.update_agent_state(
            agent_id,
            sprite_ready=True,
            sprite_url=real_walk_url,
            sprite_animations=full_sprite_animations,
            sprite_status="complete",
        )
        logger.info("[agent:%s] Phase C complete: %d animations", agent_id, len(full_sprite_animations))

        # Flip to alive (single seam — atomic DDB + cache via the lattice).
        advance_phase(agent_id, "alive", service=service)
        logger.info("[agent:%s] Agent is alive", agent_id)

        # Game-server sprite push / late connect / daemon sprite_ready all
        # happen behind the sink (in-process or via the internal endpoint).
        await sink.sprite_updated(agent_id, "animations_updated")
    finally:
        _resume_in_flight.discard(agent_id)


async def run_animation_job(agent_id: str, anim_name: str, *, sink: SpriteEventSink) -> None:
    """Generate ONE lazy animation end-to-end (activity anims, idle, talking).
    Idempotent: skips if the animation already exists on the agent row.
    Raises on transient PixelLab failure AND on empty/uncompositable output
    (the worker releases the message for redelivery; 3 strikes → DLQ). The
    quiet early-returns are reserved for terminally un-processable messages:
    missing agent, no character id, already-present or unknown animation."""
    from core.config import settings
    from core.services.pixellab_service import ANIMATION_REGISTRY, PixelLabService
    from core.services.sprite_storage import (
        composite_animation_spritesheet,
        upload_sprite_to_s3,
    )

    if agent_id in _resume_in_flight:
        logger.info("Sprite generation already in flight for %s — skipping animation %s", agent_id, anim_name)
        return
    _resume_in_flight.add(agent_id)

    try:
        service = TownService()

        agent = service.get_town_agent_by_id(str(agent_id))
        if not agent:
            logger.warning("run_animation_job: agent %s not found — dropping job", agent_id)
            return

        # Both attribute spellings exist in older rows (camelCase is current).
        char_id = agent.get("pixellabCharacterId") or agent.get("pixellab_character_id")
        if not char_id:
            logger.warning("run_animation_job: agent %s has no pixellabCharacterId — cannot animate", agent_id)
            return

        if anim_name in (agent.get("spriteAnimations") or {}):
            logger.info("run_animation_job: agent %s already has %s — duplicate delivery", agent_id, anim_name)
            return

        if anim_name not in ANIMATION_REGISTRY:
            logger.warning("run_animation_job: unknown animation %r for agent %s", anim_name, agent_id)
            return

        pxl = PixelLabService()
        direction_frames = await pxl.generate_animation(char_id, anim_name)
        if not direction_frames:
            # generate_animation returns {} on an all-jobs timeout/failure
            # without raising. Returning normally here would DELETE the SQS
            # message and leave the animation permanently missing — raise so
            # redelivery retries and 3 strikes reach the DLQ (same contract
            # as the signup walk guard).
            raise RuntimeError(f"animation {anim_name!r} produced no frames for agent {agent_id}")

        # CPU-bound PIL work — offload off the event loop.
        sheet = await asyncio.to_thread(composite_animation_spritesheet, direction_frames)
        if sheet is None:
            raise RuntimeError(f"failed to composite animation {anim_name!r} for agent {agent_id}")
        png_bytes, frame_count = sheet

        s3_key = await asyncio.to_thread(
            upload_sprite_to_s3,
            png_bytes,
            str(agent_id),
            settings.SPRITE_S3_BUCKET,
            f"{anim_name}.png",
        )

        # Re-read the row before merging: generation takes minutes and other
        # animations may have landed meanwhile (concurrent worker runs).
        # Merge into a COPY.
        fresh = service.get_town_agent_by_id(str(agent_id))
        merged = dict(((fresh or agent).get("spriteAnimations")) or {})
        registry_entry = ANIMATION_REGISTRY.get(anim_name, {})
        merged[anim_name] = {
            "url": f"{settings.SPRITE_CDN_URL}/{s3_key}",
            "frameCount": frame_count,
            "fps": int(registry_entry.get("fps", 8)),
        }

        service.update_agent_state(agent_id, sprite_animations=merged)
        logger.info("[agent:%s] Animation %s ready", agent_id, anim_name)

        await sink.sprite_updated(agent_id, "animations_updated")
    finally:
        _resume_in_flight.discard(agent_id)


async def resume_sprite_generation(
    agent_id: str,
    character_id: str | None,
    appearance: str,
    display_name: str,
    current_status: str,
) -> None:
    """Resume an interrupted sprite generation from a specific phase.

    Used by the admin recomposite endpoint (``/admin/recomposite-sprites``)
    to re-download existing PixelLab frames and rebuild the spritesheet
    without consuming new generation credits.  Picks up at the phase
    passed as ``current_status`` and runs to completion.
    """
    from core.config import settings
    from core.services.pixellab_service import ANIMATION_REGISTRY, PixelLabService
    from core.services.sprite_storage import (
        composite_animation_spritesheet,
        upload_sprite_to_s3,
    )

    # Per-agent in-flight guard (#85). A second concurrent resume for the same
    # agent (e.g. admin /regenerate-sprite running while startup recovery also
    # fires a resume on the same resident) returns early instead of generating
    # a duplicate set of sheets and racing the phase-lattice monotonic guard.
    if agent_id in _resume_in_flight:
        logger.info("Sprite generation already in flight for %s — skipping duplicate", agent_id)
        return
    _resume_in_flight.add(agent_id)

    try:
        pxl = PixelLabService()
        service = TownService()

        # Recover the existing spriteUrl from DynamoDB so we don't overwrite it
        # with "" if Phase C can't find a "walk" key in the animation dict.
        existing_agent = service.get_town_agent_by_id(agent_id)
        existing_sprite_url = (existing_agent or {}).get("spriteUrl", "")

        def _progress(status: str) -> None:
            _send_to_daemon(agent_id, {"event": "sprite_progress", "status": status})

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

    except PixelLabCreditError:
        raise
    except Exception:
        logger.exception("Failed to resume sprite generation for %s", agent_id)
        try:
            TownService().update_agent_state(agent_id, sprite_status=None)
        except Exception:
            logger.debug(
                "Failed to clear sprite_status for %s after resume failure",
                agent_id,
                exc_info=True,
            )
    finally:
        _resume_in_flight.discard(agent_id)
