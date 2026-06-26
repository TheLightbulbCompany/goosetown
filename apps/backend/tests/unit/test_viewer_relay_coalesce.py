"""ViewerRelay frame coalescing — a burst of world_states collapses to the
latest (intermediate frames dropped), so the 10Hz viewer path can't pile up
overlapping fan-outs."""

from __future__ import annotations

import asyncio

from core.services.viewer_relay import ViewerRelay


async def _drive_burst() -> list[int]:
    relay = ViewerRelay("ws://gs")
    relay._running = True
    relay._pending_event = asyncio.Event()

    broadcasted: list[int] = []
    first_started = asyncio.Event()
    release = asyncio.Event()

    async def fake_broadcast(data: dict) -> None:
        broadcasted.append(data["seq"])
        if data["seq"] == 1:
            first_started.set()
            await release.wait()  # hold frame 1 mid-fanout

    relay._broadcast = fake_broadcast  # type: ignore[method-assign]
    worker = asyncio.create_task(relay._broadcast_worker())

    relay._pending = {"seq": 1}
    relay._pending_event.set()
    await first_started.wait()  # frame 1 is now in flight, blocked

    # frames 2 and 3 arrive during frame 1's fanout -> coalesce to 3
    relay._pending = {"seq": 2}
    relay._pending_event.set()
    relay._pending = {"seq": 3}
    relay._pending_event.set()

    release.set()  # let frame 1 finish; worker loops and takes latest (3)
    await asyncio.sleep(0.05)

    relay._running = False
    relay._pending_event.set()
    worker.cancel()
    return broadcasted


def test_burst_coalesces_to_latest():
    assert asyncio.run(_drive_burst()) == [1, 3]  # frame 2 superseded by 3


async def _drive_single() -> list[int]:
    relay = ViewerRelay("ws://gs")
    relay._running = True
    relay._pending_event = asyncio.Event()
    broadcasted: list[int] = []

    async def fake_broadcast(data: dict) -> None:
        broadcasted.append(data["seq"])

    relay._broadcast = fake_broadcast  # type: ignore[method-assign]
    worker = asyncio.create_task(relay._broadcast_worker())
    relay._pending = {"seq": 7}
    relay._pending_event.set()
    await asyncio.sleep(0.02)
    relay._running = False
    relay._pending_event.set()
    worker.cancel()
    return broadcasted


def test_single_frame_delivered():
    assert asyncio.run(_drive_single()) == [7]
