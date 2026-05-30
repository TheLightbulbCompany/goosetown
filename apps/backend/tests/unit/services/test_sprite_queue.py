"""Unit tests for the PixelLab sprite generation queue.

The queue enforces bounded concurrency against PixelLab (max 3 in flight) so
a burst of signups doesn't hit rate limits, retries transient failures with
exponential backoff, and deduplicates re-enqueue of in-flight agents.

The upstream ``_sprite_pipeline_task`` (in core/services/sprite_pipeline.py) is patched with
an AsyncMock so tests don't touch PixelLab, S3, or DynamoDB.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_enqueue_returns_position_and_tracks_active():
    from core.services.sprite_queue import SpriteQueue

    q = SpriteQueue(max_concurrent=3)
    pos0 = q.enqueue("agent-1", "Lucky", "a cat")
    pos1 = q.enqueue("agent-2", "Marvin", "a robot")
    pos2 = q.enqueue("agent-3", "Ziggy", "a lizard")
    # First enqueue reports position 0, next 1, next 2
    assert pos0 == 0
    assert pos1 == 1
    assert pos2 == 2
    # Re-enqueuing an agent already in the queue returns -1
    assert q.enqueue("agent-1", "Lucky", "a cat") == -1


@pytest.mark.asyncio
async def test_enqueue_dedups_while_active():
    """Re-enqueuing an agent that's already in the in-flight set returns
    -1 and does not push a duplicate into the queue."""
    from core.services.sprite_queue import SpriteQueue

    q = SpriteQueue(max_concurrent=3)
    q.enqueue("agent-1", "Lucky", "a cat")
    assert q.enqueue("agent-1", "Lucky", "a cat") == -1
    # Still only one in the internal queue
    assert q._queue.qsize() == 1


@pytest.mark.asyncio
async def test_worker_processes_jobs_with_bounded_concurrency():
    """With max_concurrent=2 and 5 slow jobs enqueued, at most 2 run at once.

    We gate the mocked pipeline on a lock that's held until we release.
    """
    from core.services.sprite_queue import SpriteQueue

    q = SpriteQueue(max_concurrent=2)
    in_flight = 0
    peak_in_flight = 0
    gate = asyncio.Event()

    async def fake_pipeline(agent_id, display_name, appearance):
        nonlocal in_flight, peak_in_flight
        in_flight += 1
        peak_in_flight = max(peak_in_flight, in_flight)
        await gate.wait()
        in_flight -= 1

    with patch("core.services.sprite_queue._sprite_pipeline_task", side_effect=fake_pipeline):
        q.start()
        for i in range(5):
            q.enqueue(f"agent-{i}", f"Name{i}", "appearance")

        # Let the worker dispatch jobs
        await asyncio.sleep(0.05)
        # Open the gate so jobs can finish
        gate.set()
        # Drain
        for _ in range(50):
            if q._queue.empty() and in_flight == 0:
                break
            await asyncio.sleep(0.02)
        await q.stop()

    assert peak_in_flight <= 2
    # Every job should eventually complete
    assert in_flight == 0


@pytest.mark.asyncio
async def test_failed_job_re_enqueues_with_incremented_retry():
    """A transient failure should re-enqueue the job with retries+1. We test
    the single-job behavior directly via _process_job to avoid the real
    backoff delays of the worker loop."""
    from core.services import sprite_queue as sq

    q = sq.SpriteQueue(max_concurrent=3)
    job = sq.SpriteJob(agent_id="agent-flaky", display_name="Flaky", appearance="x")
    q._active_jobs.add(job.agent_id)

    # Bypass the real backoff wait inside _process_job.
    async def instant_sleep(_):
        return None

    with (
        patch(
            "core.services.sprite_queue._sprite_pipeline_task",
            new=AsyncMock(side_effect=RuntimeError("PixelLab 500")),
        ),
        patch.object(sq.asyncio, "sleep", side_effect=instant_sleep),
    ):
        await q._process_job(job)

    # The job should have been re-enqueued with retries=1.
    assert q._queue.qsize() == 1
    requeued = await q._queue.get()
    assert requeued.agent_id == "agent-flaky"
    assert requeued.retries == 1
    # finally clause clears active_jobs on both retry and give-up paths;
    # the requeued task will be re-tracked when the worker picks it up.
    assert "agent-flaky" not in q._active_jobs


@pytest.mark.asyncio
async def test_exhausted_retries_gives_up_and_clears_active():
    """When a job has already consumed max_retries, a failure does not
    re-enqueue and the agent is removed from active_jobs."""
    from core.services import sprite_queue as sq

    q = sq.SpriteQueue(max_concurrent=3)
    job = sq.SpriteJob(
        agent_id="agent-doomed",
        display_name="Doomed",
        appearance="x",
        retries=3,
        max_retries=3,
    )
    q._active_jobs.add(job.agent_id)

    with patch(
        "core.services.sprite_queue._sprite_pipeline_task",
        new=AsyncMock(side_effect=RuntimeError("PixelLab permanent")),
    ):
        await q._process_job(job)

    assert q._queue.qsize() == 0
    assert "agent-doomed" not in q._active_jobs


@pytest.mark.asyncio
async def test_successful_job_clears_active():
    from core.services import sprite_queue as sq

    q = sq.SpriteQueue(max_concurrent=3)
    job = sq.SpriteJob(agent_id="agent-ok", display_name="OK", appearance="x")
    q._active_jobs.add(job.agent_id)

    with patch("core.services.sprite_queue._sprite_pipeline_task", new=AsyncMock(return_value=None)):
        await q._process_job(job)

    assert q._queue.qsize() == 0
    assert "agent-ok" not in q._active_jobs


def test_get_sprite_queue_returns_singleton():
    from core.services import sprite_queue as sq

    # Reset the module-level singleton for test isolation
    sq._sprite_queue = None
    a = sq.get_sprite_queue()
    b = sq.get_sprite_queue()
    assert a is b
    sq._sprite_queue = None


def test_singleton_throttles_to_one_concurrent_job():
    """Regression guard: the production singleton must serialize pipelines.

    PixelLab Tier 2 caps at 10 concurrent background jobs; a single in-flight
    sprite pipeline already issues 4 jobs per animation phase, so anything >1
    runs over the cap and silently abandons agents mid-pipeline.
    """
    from core.services import sprite_queue as sq

    sq._sprite_queue = None
    q = sq.get_sprite_queue()
    assert q._semaphore._value == 1
    sq._sprite_queue = None
