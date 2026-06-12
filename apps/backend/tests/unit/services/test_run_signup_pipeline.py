"""Tests for the resume-aware, sink-driven sprite pipeline (durable-sprite-pipeline Task 6).

``run_signup_pipeline`` is the resume-aware, worker-runnable pipeline:

* The agent ROW is the source of truth (display_name / appearance / char_id /
  status are read from DynamoDB, not from caller args).
* All side effects that need backend-process state (game-server pushes, daemon
  notifications) go through a ``SpriteEventSink`` instead of being called
  directly — the SQS sprite worker passes an HTTP sink, in-backend callers pass
  ``LocalSpriteEventSink``.
* Failures PROPAGATE (the worker releases the SQS message for redelivery) and
  ``spriteStatus`` is left where it was — it is the resume pointer.

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


async def test_resume_walk_url_empty_queue_raises_not_completes():
    """No walk frames come back on resume (queue_signup_animations returns {}) →
    the new per-animation guard must raise RuntimeError so the SQS worker retries
    rather than silently completing on the existing static spriteUrl.
    Previously this test asserted the URL-fallback path; the URL-fallback is now
    unreachable because missing frames are caught first."""
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
        with pytest.raises(RuntimeError, match="signup animation"):
            await run_signup_pipeline("a-1", sink=sink)

    # sprite_status must never reach "complete".
    complete_calls = [c for c in ts.update_agent_state.call_args_list if c.kwargs.get("sprite_status") == "complete"]
    assert len(complete_calls) == 0

    # Guard drained.
    assert "a-1" not in sprite_pipeline._resume_in_flight


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


async def test_walk_generation_failure_propagates_after_phase_b():
    """Phase B succeeds (static sprite ready) but queue_signup_animations
    raises — the failure must propagate out of run_signup_pipeline so the SQS
    worker's retry/DLQ owns it.  The agent keeps operating on the Phase B
    static sprite between retries; the last sprite_status write is "animating"
    (never "complete") and advance_phase is never called with "alive"."""
    row = {
        "id": "a-1",
        "spriteStatus": "queued",
        "appearance": "a goose",
        "pixellabCharacterId": "char-1",
        # No spriteUrl — Phase B runs and succeeds via rotations below.
    }
    ts = _make_service(row)
    sink = _make_sink()

    pxl = MagicMock()
    pxl.create_character = AsyncMock()
    pxl.fetch_all_rotations = AsyncMock(return_value=dict(_FOUR_ROTATIONS))
    pxl.queue_signup_animations = AsyncMock(side_effect=RuntimeError("walk generation persistently failing"))

    with _patched_pipeline(pxl, ts) as (advance, _):
        with pytest.raises(RuntimeError, match="persistently failing"):
            await run_signup_pipeline("a-1", sink=sink)

    # Phase B completed: phase advanced to "static" with the static sheet.
    static_calls = [c for c in advance.call_args_list if c.args[1] == "static"]
    assert len(static_calls) == 1

    # Last sprite_status write is "animating" — the resume pointer; never "complete".
    status_writes = [
        c.kwargs["sprite_status"] for c in ts.update_agent_state.call_args_list if "sprite_status" in c.kwargs
    ]
    assert status_writes[-1] == "animating"
    assert "complete" not in status_writes

    # advance_phase must NOT have been called with "alive" — job is not done.
    alive_calls = [c for c in advance.call_args_list if c.args[1] == "alive"]
    assert len(alive_calls) == 0

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
        # The new per-animation check fires first (before the full_sprite_animations
        # guard) because queue_signup_animations returned {} — no walk frames.
        with pytest.raises(RuntimeError, match="signup animation"):
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
# 4c. Empty walk output with Phase B static placeholder already seeded
#     (codex P2 — the no-exception empty-output failure mode)
# ---------------------------------------------------------------------------


async def test_empty_walk_frames_raises_not_silently_completes_on_static_placeholder():
    """Phase B succeeds (static sprite + walk placeholder seeded into
    initial_sprite_animations), but queue_signup_animations returns
    {"walk": {}} — empty frame dict — without raising.

    The composite loop skips the empty entry, leaving full_sprite_animations
    with only the Phase B static placeholder.  The existing
    ``if not full_sprite_animations`` guard does NOT fire because the dict is
    non-empty (it has the static placeholder).  The new per-animation check
    must catch this and raise RuntimeError so the SQS worker retries instead
    of silently completing spriteStatus="complete" on the static sheet.

    Assertions:
    - pytest.raises(RuntimeError) matching the new message.
    - update_agent_state never called with sprite_status="complete".
    - advance_phase never called with "alive".
    - In-flight guard drained.
    """
    row = {
        "id": "a-1",
        "spriteStatus": "queued",
        "appearance": "a goose",
        "pixellabCharacterId": "char-1",
        # No spriteUrl — Phase B runs and seeds the static placeholder.
    }
    ts = _make_service(row)
    sink = _make_sink()

    pxl = MagicMock()
    pxl.create_character = AsyncMock()
    # Phase B: rotations come back so the static placeholder is seeded.
    pxl.fetch_all_rotations = AsyncMock(return_value=dict(_FOUR_ROTATIONS))
    # Phase C: queue_signup_animations returns without raising, but with empty frames.
    pxl.queue_signup_animations = AsyncMock(return_value={"walk": {}})

    with _patched_pipeline(pxl, ts) as (advance, _):
        with pytest.raises(RuntimeError, match="signup animation"):
            await run_signup_pipeline("a-1", sink=sink)

    # sprite_status must never reach "complete".
    status_writes = [
        c.kwargs["sprite_status"] for c in ts.update_agent_state.call_args_list if "sprite_status" in c.kwargs
    ]
    assert "complete" not in status_writes, f"unexpected 'complete' write: {status_writes}"

    # advance_phase must NOT have been called with "alive".
    alive_calls = [c for c in advance.call_args_list if c.args[1] == "alive"]
    assert len(alive_calls) == 0

    # In-flight guard must be drained.
    assert "a-1" not in sprite_pipeline._resume_in_flight


async def test_walk_frames_present_completes_successfully():
    """Sanity regression: the happy path (walk frames present) is unaffected by
    the new per-animation check.  Pipeline must advance to "alive" and fire
    sink.sprite_updated("animations_updated")."""
    row = {
        "id": "a-1",
        "spriteStatus": "queued",
        "appearance": "a pixel-art goose in a suit",
        "pixellabCharacterId": "char-1",
        # No spriteUrl — Phase B runs.
    }
    ts = _make_service(row)
    sink = _make_sink()

    pxl = MagicMock()
    pxl.create_character = AsyncMock()
    pxl.fetch_all_rotations = AsyncMock(return_value=dict(_FOUR_ROTATIONS))
    # Walk frames present — new guard must not fire.
    pxl.queue_signup_animations = AsyncMock(return_value={"walk": dict(_FOUR_DIR_FRAMES)})

    with _patched_pipeline(pxl, ts) as (advance, _):
        await run_signup_pipeline("a-1", sink=sink)

    # Pipeline completed: sprite_status="complete" written.
    complete_calls = [c for c in ts.update_agent_state.call_args_list if c.kwargs.get("sprite_status") == "complete"]
    assert len(complete_calls) == 1

    # advance_phase called with "alive".
    alive_calls = [c for c in advance.call_args_list if c.args[1] == "alive"]
    assert len(alive_calls) == 1

    # Sink notified.
    sink.sprite_updated.assert_any_await("a-1", "animations_updated")

    # In-flight guard drained.
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


async def test_run_animation_job_empty_frames_raises_for_redelivery():
    """generate_animation can return {} on an all-jobs timeout WITHOUT raising.
    That must NOT delete the SQS message (the animation would be permanently
    missing) — run_animation_job raises so the worker releases for retry."""
    row = {"id": "a-1", "pixellabCharacterId": "char-1", "spriteAnimations": {}}
    ts = _make_service(row)
    sink = _make_sink()

    pxl = MagicMock()
    pxl.generate_animation = AsyncMock(return_value={})

    with _patched_pipeline(pxl, ts), pytest.raises(RuntimeError, match="produced no frames"):
        await run_animation_job("a-1", "reading", sink=sink)

    ts.update_agent_state.assert_not_called()
    sink.sprite_updated.assert_not_awaited()
    # The in-flight guard must be drained even on the raise path.
    from core.services.sprite_pipeline import _resume_in_flight

    assert "a-1" not in _resume_in_flight


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


# ---------------------------------------------------------------------------
# 7. Fallback appearance from SQS message (codex P1 — register-path gap)
# ---------------------------------------------------------------------------


async def test_row_without_appearance_uses_fallback_and_promotes_it():
    """Row lacks appearance (opt_in path); fallback_appearance supplied by the
    worker processor.  Pipeline must:
    (a) call create_character with the fallback value, and
    (b) persist the fallback onto the row via update_agent_state BEFORE Phase A.
    """
    row = {
        "id": "a-1",
        "spriteStatus": "queued",
        # NOTE: no 'appearance' key — simulates an opt_in-created row
        "displayName": "Goose",
    }
    ts = _make_service(row)
    sink = _make_sink()

    pxl = MagicMock()
    pxl.create_character = AsyncMock(return_value=("char-1", "job-1"))
    pxl.get_job_status = AsyncMock(return_value={"status": "completed"})
    pxl.fetch_all_rotations = AsyncMock(return_value=dict(_FOUR_ROTATIONS))
    pxl.queue_signup_animations = AsyncMock(return_value={"walk": dict(_FOUR_DIR_FRAMES)})

    with _patched_pipeline(pxl, ts):
        await run_signup_pipeline(
            "a-1",
            sink=sink,
            fallback_appearance="a pixel-art goose in a suit",
        )

    # (a) create_character called with the fallback description.
    pxl.create_character.assert_awaited_once_with(description="a pixel-art goose in a suit")

    # (b) appearance was promoted onto the row before Phase A (must appear as a
    #     kwarg to update_agent_state among the early calls, before "creating").
    all_kwargs = [c.kwargs for c in ts.update_agent_state.call_args_list]
    promote_calls = [kw for kw in all_kwargs if kw.get("appearance") == "a pixel-art goose in a suit"]
    assert promote_calls, "appearance was never promoted onto the row"

    # The promote write must precede the sprite_status="creating" write.
    writes_in_order = [
        (i, kw) for i, kw in enumerate(all_kwargs) if kw.get("appearance") or kw.get("sprite_status") == "creating"
    ]
    appearance_idx = next(i for i, kw in writes_in_order if kw.get("appearance"))
    creating_idx = next(i for i, kw in writes_in_order if kw.get("sprite_status") == "creating")
    assert appearance_idx < creating_idx, "appearance promote must happen before sprite_status='creating' write"


async def test_row_with_appearance_ignores_fallback():
    """When the row already has appearance, the fallback must NOT overwrite it
    and must NOT trigger an extra update_agent_state call for appearance."""
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

    with _patched_pipeline(pxl, ts):
        await run_signup_pipeline(
            "a-1",
            sink=sink,
            fallback_appearance="DIFFERENT fallback that must be ignored",
        )

    # create_character must use the ROW's appearance, not the fallback.
    pxl.create_character.assert_awaited_once_with(description="a pixel-art goose in a suit")

    # No update_agent_state call should carry appearance (it's already on the row).
    all_kwargs = [c.kwargs for c in ts.update_agent_state.call_args_list]
    promote_calls = [kw for kw in all_kwargs if "appearance" in kw]
    assert not promote_calls, f"unexpected appearance writes: {promote_calls}"


async def test_no_appearance_anywhere_releases_claim_and_returns():
    """Row has no appearance AND no fallback supplied → early-return path.
    Pipeline must NOT call PixelLab AND must attempt to release the claim
    via release_sprite_claim."""
    row = {
        "id": "a-1",
        "spriteStatus": "queued",
        # No appearance
    }
    ts = _make_service(row)
    ts.release_sprite_claim = MagicMock(return_value=True)
    sink = _make_sink()

    pxl = MagicMock()
    pxl.create_character = AsyncMock()

    with _patched_pipeline(pxl, ts):
        # Must NOT raise — the early return is a terminal no-op, not a retryable error.
        await run_signup_pipeline("a-1", sink=sink)

    pxl.create_character.assert_not_awaited()
    ts.release_sprite_claim.assert_called_once_with("a-1")


async def test_no_appearance_nonqueued_status_is_cleared():
    """release_sprite_claim returns False (row status isn't "queued" — e.g. a
    pre-migration row stuck at "creating" with no char_id and no appearance,
    re-enqueued by the boot sweep). The pipeline must clear sprite_status so
    the row doesn't loop sweep→no-op→delete forever; a future register attempt
    (which persists appearance) can then re-claim. Safe: single worker process
    + per-agent in-flight guard means no concurrent pipeline for this agent."""
    row = {
        "id": "a-1",
        "spriteStatus": "creating",
    }
    ts = _make_service(row)
    ts.release_sprite_claim = MagicMock(return_value=False)
    sink = _make_sink()

    pxl = MagicMock()
    pxl.create_character = AsyncMock()

    with _patched_pipeline(pxl, ts):
        await run_signup_pipeline("a-1", sink=sink)

    pxl.create_character.assert_not_awaited()
    ts.release_sprite_claim.assert_called_once_with("a-1")
    # The stuck non-queued status must be cleared.
    status_clear_calls = [c for c in ts.update_agent_state.call_args_list if c.kwargs.get("sprite_status") is None]
    assert status_clear_calls, "expected a sprite_status=None clear for the stuck row"
