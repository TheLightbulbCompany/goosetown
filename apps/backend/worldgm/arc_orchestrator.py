"""Emergent storyline orchestrator: author grounded, cast-based storylines from the
real world snapshot and deliver them per-cast. Replaces the old template-seeded arcs.
See docs/superpowers/specs/2026-06-07-worldgm-emergent-narrative-design.md."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
import uuid

from core import dynamo

from .event_publisher import _emit_metric, emit_world_event
from .narrative_director import author_beat, author_kickoff
from .world_snapshot import WorldSnapshot, build_snapshot, compute_signals

logger = logging.getLogger(__name__)

_MIN_ONLINE_AGENTS = 2
_RANDOM_KICKOFF_CHANCE = 0.10  # per tick when no signal clears the bar
_SIGNAL_MIN_SCORE = float(os.environ.get("WORLDGM_SIGNAL_MIN_SCORE", "1.5"))
_BEAT_INTERVAL_MS = int(float(os.environ.get("WORLDGM_BEAT_INTERVAL_MIN", "4")) * 60_000)
_DURATION_MS = int(float(os.environ.get("WORLDGM_STORYLINE_DURATION_MIN", "30")) * 60_000)
_MAX_KICKOFFS_PER_MIN = int(os.environ.get("WORLDGM_MAX_KICKOFFS_PER_MIN", "2"))

# Process-local sliding window of recent kickoff timestamps (ms) for the rate cap.
_recent_kickoffs: list[int] = []


def _now_ms() -> int:
    return int(time.time() * 1000)


def _rate_capped(now: int) -> bool:
    cutoff = now - 60_000
    _recent_kickoffs[:] = [t for t in _recent_kickoffs if t >= cutoff]
    return len(_recent_kickoffs) >= _MAX_KICKOFFS_PER_MIN


async def _read_active_arcs() -> list[dict]:
    table = dynamo.worldgm_arcs_table()
    resp = table.query(
        IndexName="status-nextBeatAt-index",
        KeyConditionExpression="#status = :active",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":active": "active"},
    )
    return resp.get("Items", [])


def _busy_agent_ids(active: list[dict]) -> set[str]:
    busy: set[str] = set()
    for arc in active:
        busy.update(arc.get("cast", []))
    return busy


def _filter_snapshot(snap: WorldSnapshot, busy: set[str]) -> WorldSnapshot:
    """Return a snapshot whose residents exclude agents already in a storyline, so
    the director only recruits free agents (each agent in <=1 storyline)."""
    if not busy:
        return snap
    free = [r for r in snap.residents if r.agent_id not in busy]
    free_names = {r.name for r in free}
    return WorldSnapshot(
        residents=free,
        relationships=[r for r in snap.relationships if r.a in free_names and r.b in free_names],
        recent_conversations=snap.recent_conversations,
        clusters={loc: [n for n in m if n in free_names] for loc, m in snap.clusters.items()},
        hour_utc=snap.hour_utc,
    )


async def maybe_kickoff_arc(state) -> dict | None:
    """Author + persist a new cast-based storyline if conditions allow, else None."""
    if len(state.last_known_locations) < _MIN_ONLINE_AGENTS:
        return None
    now = _now_ms()
    if _rate_capped(now):
        return None

    active = await _read_active_arcs()
    busy = _busy_agent_ids(active)

    snap = await asyncio.to_thread(build_snapshot, state)
    snap = _filter_snapshot(snap, busy)
    if len(snap.residents) < _MIN_ONLINE_AGENTS:
        return None

    signals = compute_signals(snap)
    has_hook = bool(signals) and signals[0].score >= _SIGNAL_MIN_SCORE
    if not has_hook and random.random() >= _RANDOM_KICKOFF_CHANCE:
        return None

    injection = await asyncio.to_thread(author_kickoff, state.model_lite, snap, signals)
    if injection is None:
        return None

    name_to_id = {r.name: r.agent_id for r in snap.residents}
    cast_ids = [name_to_id[n] for n in injection.cast if n in name_to_id]
    if len(cast_ids) < 1:
        return None

    arc = {
        "arcId": str(uuid.uuid4()),
        "status": "active",
        "startedAt": now,
        "expiresAt": now + _DURATION_MS,
        "narrativeKickoff": injection.narrative,
        "location": injection.location,
        "cast": cast_ids,
        "nextBeatAt": str(now + _BEAT_INTERVAL_MS),
    }
    dynamo.worldgm_arcs_table().put_item(Item=arc)
    _recent_kickoffs.append(now)
    _emit_metric("ArcsKickoff")
    logger.info("[arc:%s] kicked off — cast=%d location=%s", arc["arcId"][:8], len(cast_ids), injection.location)
    await _emit_arc_event(tag="arc.kickoff", arc=arc, summary=injection.narrative)
    return arc


async def process_arc_beats(state) -> list[dict]:
    """Advance/resolve due arcs; return the still-active set (fresh, in-memory)."""
    active = await _read_active_arcs()
    now = _now_ms()
    still: list[dict] = []
    for arc in active:
        if int(arc.get("nextBeatAt", 0)) <= now:
            if await _fire_beat(state, arc):
                continue
        still.append(arc)
    return still


async def _fire_beat(state, arc: dict) -> bool:
    """Fire one beat. Returns True if the arc resolved (drop it)."""
    now = _now_ms()
    if int(arc.get("expiresAt", 0)) <= now:
        await _resolve_arc(arc)
        return True

    snap = await asyncio.to_thread(build_snapshot, state)
    beat_text = await asyncio.to_thread(author_beat, state.model_lite, arc, snap)
    if beat_text is None:
        await _resolve_arc(arc)
        return True

    next_beat = str(now + _BEAT_INTERVAL_MS)
    dynamo.worldgm_arcs_table().update_item(
        Key={"arcId": arc["arcId"]},
        UpdateExpression="SET nextBeatAt = :n, lastBeatAt = :l, lastBeatText = :t",
        ExpressionAttributeValues={":n": next_beat, ":l": now, ":t": beat_text},
    )
    arc["lastBeatText"] = beat_text
    arc["lastBeatAt"] = now
    arc["nextBeatAt"] = next_beat
    await _emit_arc_event(tag="arc.beat", arc=arc, summary=beat_text)
    return False


async def _resolve_arc(arc: dict) -> None:
    dynamo.worldgm_arcs_table().update_item(
        Key={"arcId": arc["arcId"]},
        UpdateExpression="SET #status = :s, resolvedAt = :r",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":s": "resolved", ":r": _now_ms()},
    )
    _emit_metric("ArcsResolved")
    await _emit_arc_event(
        tag="arc.resolved", arc=arc, summary=arc.get("lastBeatText") or arc.get("narrativeKickoff", "")
    )


async def _emit_arc_event(*, tag: str, arc: dict, summary: str) -> None:
    """Per-cast world event (best-effort). witnesses = cast ids → personal delivery."""
    try:
        await asyncio.to_thread(
            emit_world_event,
            tag=tag,
            detail={"arcId": arc["arcId"]},
            witnesses=arc.get("cast", []),
            location=arc.get("location"),
            summary=summary,
        )
    except Exception:
        logger.exception("[arc:%s] emit_world_event failed for %s", arc["arcId"][:8], tag)
