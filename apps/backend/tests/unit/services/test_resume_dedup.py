"""Per-agent in-flight dedup on ``resume_sprite_generation`` (#85).

Regenerating a RESIDENT raced two concurrent ``resume_sprite_generation`` runs
for the same agent — the admin ``POST /town/admin/regenerate-sprite/{id}``
endpoint (resumes at ``current_status="creating"``) AND startup recovery
(``main.py:_reconnect_active_agents``, which fires a resume on any agent whose
``spriteStatus`` is ``creating``/``animating``/``downloading``). The admin run
bumps ``spriteStatus`` to ``"animating"`` in DDB, so a worldgm backend booting
mid-run sees that and re-triggers its own resume.

Both runs generated and uploaded all 3 sheets, but the phase-lattice monotonic
guard (``advance_phase``) skipped the second run's ``advance_phase("alive", …)``
because the first run had already reached ``alive`` — dropping the second run's
``spriteAnimations`` write. Net: walk.png rendered but idle/talking weren't
registered, plus a full duplicate generation wasted PixelLab credits.

The fix is a module-level ``set[str]`` of in-flight agent_ids: a second
concurrent call for the same agent returns early before touching PixelLab.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import core.services.sprite_pipeline as sprite_pipeline
from core.services.sprite_pipeline import resume_sprite_generation

_FOUR_DIR_FRAMES = {
    "south": [b"f"],
    "west": [b"f"],
    "east": [b"f"],
    "north": [b"f"],
}

_AGENT_RECORD = {
    "spriteReady": True,
    "spriteUrl": "https://cdn/old.png",
    "currentLocation": "plaza",
    "locationContext": "town",
    "userId": "u1",
    "spriteAnimations": {},
}


@contextlib.contextmanager
def _patched_pipeline(pxl):
    """Patch every collaborator resume_sprite_generation touches.

    PixelLabService is patched at its source module (imported locally inside the
    function); the daemon / game-server / lattice helpers are patched at the
    sprite_pipeline module where they're bound at import time.
    """
    ts = MagicMock()
    ts.get_town_agent_by_id.return_value = dict(_AGENT_RECORD)
    gs = MagicMock()
    gs.connect_agent = AsyncMock()

    with (
        patch("core.services.pixellab_service.PixelLabService", return_value=pxl),
        patch(
            "core.services.sprite_storage.composite_animation_spritesheet",
            return_value=(b"png", 4),
        ),
        patch(
            "core.services.sprite_storage.upload_sprite_to_s3",
            return_value="sprites/agent-1/walk.png",
        ),
        patch("core.services.sprite_pipeline.TownService", return_value=ts),
        patch("core.services.sprite_pipeline.get_game_server_client", return_value=gs),
        patch("core.services.sprite_pipeline.advance_phase"),
        patch("core.services.sprite_pipeline._send_to_daemon"),
        patch("core.services.sprite_pipeline._make_agent_event_callback"),
    ):
        yield


@pytest.fixture(autouse=True)
def _clean_in_flight():
    """Each test starts and ends with an empty in-flight set so leaked state
    from one test can't mask a missing-guard regression in another.

    ``getattr`` with a fallback so this fixture doesn't error out (masking the
    real assertion) when run against a pre-fix tree that lacks the guard — in
    that state the *assertion* about generation count is what should fail."""
    getattr(sprite_pipeline, "_resume_in_flight", set()).clear()
    yield
    getattr(sprite_pipeline, "_resume_in_flight", set()).clear()


def _in_flight() -> set[str]:
    return getattr(sprite_pipeline, "_resume_in_flight", set())


@pytest.mark.asyncio
async def test_concurrent_resume_for_same_agent_runs_only_once():
    """Two concurrent resumes for the SAME agent_id: only ONE actually
    generates; the second returns early without touching PixelLab.

    The first call's generation blocks on an event we control, so it's still
    "in flight" when the second call fires — exactly the live race in #85.
    """
    started = asyncio.Event()  # set once the first run reaches generation
    release = asyncio.Event()  # we hold the first run here until both fired

    pxl = MagicMock()
    pxl.fetch_animation_spritesheets = AsyncMock(return_value={})

    async def _slow_generate(char_id, on_progress=None):
        started.set()
        await release.wait()  # keep run A in flight
        return {"walk": dict(_FOUR_DIR_FRAMES)}

    pxl.queue_signup_animations = AsyncMock(side_effect=_slow_generate)

    with _patched_pipeline(pxl):
        # Run A: resume at "animating" — will block inside queue_signup_animations.
        run_a = asyncio.create_task(resume_sprite_generation("agent-1", "char-1", "", "Name", "animating"))
        await started.wait()  # A is now in flight (agent registered in the set)

        # Run B: same agent, fired while A is in flight. With the guard it
        # short-circuits and returns instantly. WITHOUT the guard (pre-fix) it
        # runs the real generation and blocks on `release` — the wait_for then
        # raises TimeoutError, so this test FAILS pre-fix and PASSES post-fix
        # instead of deadlocking the whole suite.
        await asyncio.wait_for(
            resume_sprite_generation("agent-1", "char-1", "", "Name", "animating"),
            timeout=2.0,
        )

        # Let A finish.
        release.set()
        await run_a

    # Exactly one generation happened — B short-circuited before PixelLab.
    assert pxl.queue_signup_animations.await_count == 1
    # The in-flight set is drained by the finally on both paths.
    assert "agent-1" not in _in_flight()


@pytest.mark.asyncio
async def test_in_flight_drained_after_completion_allows_resume_again():
    """After a resume completes, the guard releases the agent so a LATER
    (non-concurrent) resume for the same agent runs normally."""
    pxl = MagicMock()
    pxl.queue_signup_animations = AsyncMock(return_value={"walk": dict(_FOUR_DIR_FRAMES)})
    pxl.fetch_animation_spritesheets = AsyncMock(return_value={})

    with _patched_pipeline(pxl):
        await resume_sprite_generation("agent-1", "char-1", "", "Name", "animating")
        assert "agent-1" not in _in_flight()
        # Second, sequential call runs again (guard was released).
        await resume_sprite_generation("agent-1", "char-1", "", "Name", "animating")

    assert pxl.queue_signup_animations.await_count == 2


@pytest.mark.asyncio
async def test_different_agents_are_not_blocked():
    """The guard is per-agent: a concurrent resume for a DIFFERENT agent_id
    runs in parallel (no cross-agent blocking)."""
    started_a = asyncio.Event()
    release = asyncio.Event()

    pxl = MagicMock()
    pxl.fetch_animation_spritesheets = AsyncMock(return_value={})

    async def _slow_generate(char_id, on_progress=None):
        started_a.set()
        await release.wait()
        return {"walk": dict(_FOUR_DIR_FRAMES)}

    pxl.queue_signup_animations = AsyncMock(side_effect=_slow_generate)

    with _patched_pipeline(pxl):
        run_a = asyncio.create_task(resume_sprite_generation("agent-A", "char-A", "", "Name", "animating"))
        await started_a.wait()
        # agent-B is a different agent — must NOT be blocked by agent-A.
        run_b = asyncio.create_task(resume_sprite_generation("agent-B", "char-B", "", "Name", "animating"))
        await started_a.wait()
        release.set()
        await asyncio.gather(run_a, run_b)

    # Both agents generated (no false cross-agent dedup).
    assert pxl.queue_signup_animations.await_count == 2
