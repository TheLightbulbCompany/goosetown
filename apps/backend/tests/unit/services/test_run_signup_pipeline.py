"""Tests for the resume-aware, sink-driven sprite pipeline (durable-sprite-pipeline Task 6).

``run_signup_pipeline`` replaces the linear ``_sprite_pipeline_task`` flow with a
worker-runnable version:

* The agent ROW is the source of truth (display_name / appearance / char_id /
  status are read from DynamoDB, not from caller args).
* All side effects that need backend-process state (game-server pushes, daemon
  notifications) go through a ``SpriteEventSink`` instead of being called
  directly — the SQS sprite worker passes an HTTP sink, in-backend callers pass
  ``LocalSpriteEventSink``.
* Failures PROPAGATE (the worker releases the SQS message for redelivery) and
  ``spriteStatus`` is left where it was — it is the resume pointer. The old
  swallow-and-clear-status contract lives only in the legacy
  ``_sprite_pipeline_task`` wrapper.

Mocking style follows ``test_resume_dedup.py`` / ``test_resume_recomposite.py``:
function-local imports are patched at their source modules
(``core.services.pixellab_service.PixelLabService``, ``core.services.sprite_storage.*``);
names bound at sprite_pipeline import time are patched in the sprite_pipeline
namespace.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import core.services.sprite_pipeline as sprite_pipeline
from core.services.sprite_pipeline import run_animation_job, run_signup_pipeline

_FOUR_DIR_FRAMES = {
    "south": [b"f"],
    "west": [b"f"],
    "east": [b"f"],
    "north": [b"f"],
}

_FOUR_ROTATIONS = {
    "south": b"png",
    "west": b"png",
    "east": b"png",
    "north": b"png",
}


def _make_sink() -> MagicMock:
    sink = MagicMock()
    sink.progress = AsyncMock()
    sink.sprite_updated = AsyncMock()
    return sink


def _make_service(agent_row: dict | None) -> MagicMock:
    ts = MagicMock()
    ts.get_town_agent_by_id.return_value = agent_row
    return ts


@contextlib.contextmanager
def _patched_pipeline(pxl, ts):
    """Patch every collaborator the pipeline touches."""
    with (
        patch("core.services.pixellab_service.PixelLabService", return_value=pxl) as pxl_cls,
        patch(
            "core.services.sprite_storage.build_static_spritesheet_from_rotations",
            return_value=b"static-png",
        ),
        patch(
            "core.services.sprite_storage.composite_animation_spritesheet",
            return_value=(b"sheet-png", 6),
        ),
        patch(
            "core.services.sprite_storage.upload_sprite_to_s3",
            return_value="sprites/a-1/walk.png",
        ),
        patch("core.services.sprite_pipeline.TownService", return_value=ts),
        patch("core.services.sprite_pipeline.advance_phase") as advance,
        # The creation-job poll loop sleeps 10s per attempt — make it instant.
        patch("core.services.sprite_pipeline.asyncio.sleep", new=AsyncMock()),
    ):
        yield advance, pxl_cls


@pytest.fixture(autouse=True)
def _clean_in_flight():
    sprite_pipeline._resume_in_flight.clear()
    yield
    sprite_pipeline._resume_in_flight.clear()


# ---------------------------------------------------------------------------
# 1. Fresh agent — full Phase A → B → C run
# ---------------------------------------------------------------------------


async def test_fresh_agent_runs_all_phases_through_sink():
    row = {
        "id": "a-1",
        "spriteStatus": "queued",
        "appearance": "a pixel-art goose in a suit",
        "displayName": "Goose",
    }
    ts = _make_service(row)
    sink = _make_sink()

    pxl = MagicMock()
    pxl.create_character = AsyncMock(return_value=("char-1", "job-1"))
    pxl.get_job_status = AsyncMock(return_value={"status": "completed"})
    pxl.fetch_all_rotations = AsyncMock(return_value=dict(_FOUR_ROTATIONS))
    pxl.queue_signup_animations = AsyncMock(return_value={"walk": dict(_FOUR_DIR_FRAMES)})

    with _patched_pipeline(pxl, ts) as (advance, _):
        await run_signup_pipeline("a-1", sink=sink)

    # Phase A ran.
    pxl.create_character.assert_awaited_once()
    # Phase B ran: rotations fetched, phase advanced to "static" with the static sheet.
    pxl.fetch_all_rotations.assert_awaited_once()
    static_calls = [c for c in advance.call_args_list if c.args[1] == "static"]
    assert len(static_calls) == 1
    assert static_calls[0].kwargs["sprite_ready"] is True
    assert static_calls[0].kwargs["sprite_status"] == "static_ready"
    sink.sprite_updated.assert_any_await("a-1", "static_ready")

    # Phase C ran: final row write has sprite_status="complete" + walk animation.
    complete_calls = [c for c in ts.update_agent_state.call_args_list if c.kwargs.get("sprite_status") == "complete"]
    assert len(complete_calls) == 1
    assert "walk" in complete_calls[0].kwargs["sprite_animations"]
    alive_calls = [c for c in advance.call_args_list if c.args[1] == "alive"]
    assert len(alive_calls) == 1
    sink.sprite_updated.assert_any_await("a-1", "animations_updated")


# ---------------------------------------------------------------------------
# 2. Resume — char_id present, status "animating", spriteUrl present
# ---------------------------------------------------------------------------


async def test_resume_skips_create_and_phase_b_and_preserves_existing_animations():
    existing_animations = {
        "walk": {"url": "https://cdn/old-walk.png", "frameCount": 6, "fps": 8},
        "idle": {"url": "https://cdn/idle.png", "frameCount": 4, "fps": 4},
    }
    row = {
        "id": "a-1",
        "spriteStatus": "animating",
        "appearance": "a goose",
        "displayName": "Goose",
        "pixellabCharacterId": "char-1",
        "spriteUrl": "https://cdn/old-walk.png",
        "spriteAnimations": dict(existing_animations),
    }
    ts = _make_service(row)
    sink = _make_sink()

    pxl = MagicMock()
    pxl.create_character = AsyncMock()
    pxl.fetch_all_rotations = AsyncMock()
    pxl.queue_signup_animations = AsyncMock(return_value={"walk": dict(_FOUR_DIR_FRAMES)})

    with _patched_pipeline(pxl, ts):
        await run_signup_pipeline("a-1", sink=sink)

    pxl.create_character.assert_not_awaited()
    pxl.fetch_all_rotations.assert_not_awaited()  # Phase B skipped
    sink_kinds = [c.args[1] for c in sink.sprite_updated.await_args_list]
    assert "static_ready" not in sink_kinds

    # Phase C still ran and the final merge kept the existing row entries.
    complete_calls = [c for c in ts.update_agent_state.call_args_list if c.kwargs.get("sprite_status") == "complete"]
    assert len(complete_calls) == 1
    final_animations = complete_calls[0].kwargs["sprite_animations"]
    assert "idle" in final_animations  # preserved from the existing row
    assert "walk" in final_animations
    sink.sprite_updated.assert_any_await("a-1", "animations_updated")


async def test_resume_walk_url_falls_back_to_existing_sprite_url():
    """No walk frames come back on resume → spriteUrl must not regress to ''."""
    row = {
        "id": "a-1",
        "spriteStatus": "downloading",
        "appearance": "a goose",
        "pixellabCharacterId": "char-1",
        "spriteUrl": "https://cdn/old-walk.png",
        "spriteAnimations": {"walk": {"url": "https://cdn/old-walk.png", "frameCount": 6, "fps": 8}},
    }
    ts = _make_service(row)
    sink = _make_sink()

    pxl = MagicMock()
    pxl.create_character = AsyncMock()
    pxl.fetch_all_rotations = AsyncMock()
    pxl.queue_signup_animations = AsyncMock(return_value={})

    with _patched_pipeline(pxl, ts):
        await run_signup_pipeline("a-1", sink=sink)

    # idle survives because initial_sprite_animations seeds from the existing row's spriteAnimations.
    complete_calls = [c for c in ts.update_agent_state.call_args_list if c.kwargs.get("sprite_status") == "complete"]
    assert len(complete_calls) == 1
    assert complete_calls[0].kwargs["sprite_url"] == "https://cdn/old-walk.png"


# ---------------------------------------------------------------------------
# 3. Duplicate delivery — complete agent is a no-op
# ---------------------------------------------------------------------------


async def test_complete_agent_returns_without_any_work():
    row = {"id": "a-1", "spriteStatus": "complete", "spriteReady": True}
    ts = _make_service(row)
    sink = _make_sink()
    pxl = MagicMock()

    with _patched_pipeline(pxl, ts) as (advance, pxl_cls):
        await run_signup_pipeline("a-1", sink=sink)

    pxl_cls.assert_not_called()  # PixelLab client never even constructed
    ts.update_agent_state.assert_not_called()
    advance.assert_not_called()
    sink.sprite_updated.assert_not_awaited()


async def test_missing_agent_returns_without_any_work():
    ts = _make_service(None)
    sink = _make_sink()
    pxl = MagicMock()

    with _patched_pipeline(pxl, ts) as (_, pxl_cls):
        await run_signup_pipeline("a-gone", sink=sink)

    pxl_cls.assert_not_called()
    ts.update_agent_state.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Failure propagates; spriteStatus left as the resume pointer
# ---------------------------------------------------------------------------


async def test_create_failure_propagates_and_status_left_at_creating():
    row = {"id": "a-1", "spriteStatus": "queued", "appearance": "a goose"}
    ts = _make_service(row)
    sink = _make_sink()

    pxl = MagicMock()
    pxl.create_character = AsyncMock(side_effect=RuntimeError("PixelLab 500"))

    with _patched_pipeline(pxl, ts):
        with pytest.raises(RuntimeError):
            await run_signup_pipeline("a-1", sink=sink)

    # The LAST status write is "creating" — never cleared to None.
    status_writes = [c.kwargs.get("sprite_status") for c in ts.update_agent_state.call_args_list]
    assert status_writes[-1] == "creating"
    assert None not in status_writes
    # Guard drained even on failure.
    assert "a-1" not in sprite_pipeline._resume_in_flight


# ---------------------------------------------------------------------------
# 4b. Empty pipeline output — raises, does NOT advance to alive
# ---------------------------------------------------------------------------


async def test_no_animations_output_raises_and_does_not_advance_alive():
    """Phase B returns no rotations AND Phase C returns no signup frames —
    full_sprite_animations stays empty.  The pipeline must raise RuntimeError
    so the SQS worker retries rather than silently completing with no sprite.
    advance_phase must NOT be called with "alive", and sprite_updated must NOT
    be awaited with "animations_updated"."""
    row = {
        "id": "a-1",
        "spriteStatus": "queued",
        "appearance": "a goose",
        "pixellabCharacterId": "char-1",
        # No spriteUrl, no spriteAnimations — Phase B will attempt rotations.
    }
    ts = _make_service(row)
    sink = _make_sink()

    pxl = MagicMock()
    pxl.create_character = AsyncMock()
    pxl.fetch_all_rotations = AsyncMock(return_value={})  # Phase B produces nothing
    pxl.queue_signup_animations = AsyncMock(return_value={})  # Phase C produces nothing

    with _patched_pipeline(pxl, ts) as (advance, _):
        with pytest.raises(RuntimeError, match="sprite pipeline produced no animations"):
            await run_signup_pipeline("a-1", sink=sink)

    # advance_phase must NOT have been called with "alive" — job is not done.
    alive_calls = [c for c in advance.call_args_list if c.args[1] == "alive"]
    assert len(alive_calls) == 0

    # sink.sprite_updated must NOT have been awaited with "animations_updated".
    animations_updated_calls = [c for c in sink.sprite_updated.await_args_list if c.args[1] == "animations_updated"]
    assert len(animations_updated_calls) == 0

    # Guard drained even on failure.
    assert "a-1" not in sprite_pipeline._resume_in_flight


# ---------------------------------------------------------------------------
# 5. run_animation_job
# ---------------------------------------------------------------------------


async def test_run_animation_job_happy_path_merges_and_notifies():
    row = {
        "id": "a-1",
        "pixellabCharacterId": "char-1",
        "spriteAnimations": {"walk": {"url": "https://cdn/walk.png", "frameCount": 6, "fps": 8}},
    }
    ts = _make_service(row)
    sink = _make_sink()

    pxl = MagicMock()
    pxl.generate_animation = AsyncMock(return_value=dict(_FOUR_DIR_FRAMES))

    with (
        _patched_pipeline(pxl, ts),
        patch(
            "core.services.sprite_storage.upload_sprite_to_s3",
            return_value="sprites/a-1/reading.png",
        ),
    ):
        await run_animation_job("a-1", "reading", sink=sink)

    pxl.generate_animation.assert_awaited_once_with("char-1", "reading")
    merged = ts.update_agent_state.call_args.kwargs["sprite_animations"]
    assert "walk" in merged  # existing keys preserved
    assert "reading" in merged
    assert merged["reading"]["url"].endswith("sprites/a-1/reading.png")
    # The row's own dict was not mutated in place (merge into a COPY).
    assert "reading" not in row["spriteAnimations"]
    sink.sprite_updated.assert_awaited_once_with("a-1", "animations_updated")


async def test_run_animation_job_skips_when_animation_already_present():
    row = {
        "id": "a-1",
        "pixellabCharacterId": "char-1",
        "spriteAnimations": {"reading": {"url": "https://cdn/reading.png", "frameCount": 12, "fps": 4}},
    }
    ts = _make_service(row)
    sink = _make_sink()
    pxl = MagicMock()
    pxl.generate_animation = AsyncMock()

    with _patched_pipeline(pxl, ts):
        await run_animation_job("a-1", "reading", sink=sink)

    pxl.generate_animation.assert_not_awaited()
    ts.update_agent_state.assert_not_called()
    sink.sprite_updated.assert_not_awaited()


async def test_run_animation_job_no_char_id_returns_without_raising():
    row = {"id": "a-1", "spriteAnimations": {}}
    ts = _make_service(row)
    sink = _make_sink()
    pxl = MagicMock()
    pxl.generate_animation = AsyncMock()

    with _patched_pipeline(pxl, ts):
        await run_animation_job("a-1", "reading", sink=sink)

    pxl.generate_animation.assert_not_awaited()
    ts.update_agent_state.assert_not_called()


async def test_run_animation_job_unknown_animation_returns_without_pixellab():
    row = {"id": "a-1", "pixellabCharacterId": "char-1", "spriteAnimations": {}}
    ts = _make_service(row)
    sink = _make_sink()
    pxl = MagicMock()
    pxl.generate_animation = AsyncMock()

    with _patched_pipeline(pxl, ts):
        await run_animation_job("a-1", "moonwalking", sink=sink)

    pxl.generate_animation.assert_not_awaited()
    ts.update_agent_state.assert_not_called()


# ---------------------------------------------------------------------------
# 6. In-flight guard (per-agent serialization, #85)
# ---------------------------------------------------------------------------


async def test_concurrent_signup_for_same_agent_runs_only_once():
    row = {"id": "a-1", "spriteStatus": "queued", "appearance": "a goose"}
    ts = _make_service(row)
    sink = _make_sink()

    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_create(description):
        started.set()
        await release.wait()
        return ("char-1", None)

    pxl = MagicMock()
    pxl.create_character = AsyncMock(side_effect=_slow_create)
    pxl.fetch_all_rotations = AsyncMock(return_value={})
    pxl.queue_signup_animations = AsyncMock(return_value={})

    with _patched_pipeline(pxl, ts):
        run_a = asyncio.create_task(run_signup_pipeline("a-1", sink=sink))
        await started.wait()
        # Second concurrent call: with the guard it returns instantly.
        await asyncio.wait_for(run_signup_pipeline("a-1", sink=sink), timeout=2.0)
        release.set()
        # The first run raises RuntimeError (no rotations + no signup frames →
        # empty full_sprite_animations) — the guard test only cares about the
        # create_character call count, not the outcome.
        with contextlib.suppress(RuntimeError):
            await run_a

    assert pxl.create_character.await_count == 1
    assert "a-1" not in sprite_pipeline._resume_in_flight


async def test_animation_job_blocked_while_signup_in_flight_for_same_agent():
    """run_animation_job shares the same per-agent guard."""
    row = {"id": "a-1", "pixellabCharacterId": "char-1", "spriteAnimations": {}}
    ts = _make_service(row)
    sink = _make_sink()
    pxl = MagicMock()
    pxl.generate_animation = AsyncMock()

    sprite_pipeline._resume_in_flight.add("a-1")  # simulate signup in flight
    with _patched_pipeline(pxl, ts):
        await run_animation_job("a-1", "reading", sink=sink)

    pxl.generate_animation.assert_not_awaited()
    # Guard entry is NOT discarded by the short-circuit path — the owning run does that.
    assert "a-1" in sprite_pipeline._resume_in_flight
