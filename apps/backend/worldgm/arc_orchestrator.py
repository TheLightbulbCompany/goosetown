"""Arc kickoff + beat processing.

Reads/writes arc state from the worldgm-arcs DDB table. Decides when to
kickoff a new narrative arc based on town signals (online agent count,
time-since-last-arc, optional goal-advanceability). Uses BedrockLanguageModel
directly (via NarrativeEventResolution.resolve) for narrative generation.

Architectural note: in v1 we don't use a full Concordia GM. The arc orchestrator
IS the GM: it decides timing, picks templates, generates per-witness narrative,
and publishes events.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid

from core import dynamo

from .arc_templates import ARC_TEMPLATES, find_template
from .event_publisher import _emit_metric, emit_world_event

logger = logging.getLogger(__name__)


_MIN_ONLINE_AGENTS = 2
_ARC_COOLDOWN_MINUTES = 60
_RANDOM_KICKOFF_CHANCE = 0.05  # per tick after cooldown


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _emit_arc_event(*, tag: str, arc: dict, summary: str, location: str | None, witnesses: list[str]) -> None:
    """Publish an arc event to EventBridge/world-events. Best-effort: a failure
    is logged and swallowed so the arc (already persisted) is unaffected. Runs
    in a thread because emit_world_event does sync EventBridge + DDB puts."""
    try:
        await asyncio.to_thread(
            emit_world_event,
            tag=tag,
            detail={"arcId": arc["arcId"], "templateId": arc.get("templateId", "")},
            witnesses=witnesses,
            location=location,
            summary=summary,
        )
    except Exception:
        logger.exception("[arc:%s] emit_world_event failed for %s", arc["arcId"][:8], tag)


async def maybe_kickoff_arc(state) -> dict | None:
    """Check kickoff conditions; if met, pick a template and run NarrativeEventResolution.

    Returns the kicked-off arc dict, or None.
    """
    # Cheap no-op cases
    online = state.last_known_locations  # set by location_sync
    if len(online) < _MIN_ONLINE_AGENTS:
        return None

    # No active arc?
    active = await _read_active_arcs()
    if active:
        return None

    # Cooldown?
    last_arc_at = await _read_last_arc_time()
    minutes_since = (_now_ms() - last_arc_at) / 60_000 if last_arc_at else float("inf")
    if minutes_since < _ARC_COOLDOWN_MINUTES:
        return None

    # Random kickoff (Phase C5 will add goal-advanceability biasing)
    if random.random() >= _RANDOM_KICKOFF_CHANCE:
        return None

    template = _pick_template(online)
    if not template:
        return None

    return await _kickoff_arc(state, template)


def _pick_template(online_locations: dict[str, str]) -> dict | None:
    """Pick a template, biased toward templates whose preferred_location has agents."""
    online_loc_set = set(online_locations.values())
    candidates: list[tuple[float, dict]] = []
    for tpl in ARC_TEMPLATES:
        score = 1.0
        if tpl.get("preferred_location") in online_loc_set:
            score += 1.0
        candidates.append((score, tpl))
    if not candidates:
        return None
    # Weighted random
    total = sum(s for s, _ in candidates)
    r = random.uniform(0, total)
    accum = 0.0
    for s, t in candidates:
        accum += s
        if r <= accum:
            return t
    return candidates[-1][1]


async def _read_active_arcs() -> list[dict]:
    table = dynamo.worldgm_arcs_table()
    # GSI: status-nextBeatAt-index
    resp = table.query(
        IndexName="status-nextBeatAt-index",
        KeyConditionExpression="#status = :active",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":active": "active"},
    )
    return resp.get("Items", [])


async def _read_last_arc_time() -> int | None:
    table = dynamo.worldgm_arcs_table()
    # Scan for most-recent startedAt — small table, scan is fine for now
    resp = table.scan(ProjectionExpression="startedAt")
    if not resp.get("Items"):
        return None
    return max(int(i.get("startedAt", 0)) for i in resp["Items"]) or None


async def _kickoff_arc(state, template: dict) -> dict:
    """Generate the kickoff narrative and publish events."""
    arc_id = str(uuid.uuid4())
    now = _now_ms()

    narrative_text = await asyncio.to_thread(state.narrative_resolver.resolve, template["kickoff_prompt"])

    # Determine witnesses: agents at the preferred_location, or all online if no preferred
    witnesses = []
    preferred = template.get("preferred_location")
    if preferred:
        witnesses = [agent_id for agent_id, loc in state.last_known_locations.items() if loc == preferred]
    if not witnesses:
        witnesses = list(state.last_known_locations.keys())

    # Persist arc state
    arc = {
        "arcId": arc_id,
        "templateId": template["id"],
        "status": "active",
        "startedAt": now,
        "expiresAt": now + template["duration_hours"] * 3_600_000,
        "narrativeKickoff": narrative_text,
        "witnessIds": witnesses,
        "nextBeatAt": str(now + 3_600_000),  # next beat in 1h
    }
    table = dynamo.worldgm_arcs_table()
    table.put_item(Item=arc)

    logger.info(
        "[arc:%s] kicked off — template=%s witnesses=%d location=%s",
        arc_id[:8],
        template["id"],
        len(witnesses),
        preferred or "any",
    )

    _emit_metric("ArcsKickoff")

    # Publish to EventBridge + world-events (durable feed log + external agents).
    # Best-effort: a publish failure can't abort the already-persisted arc.
    await _emit_arc_event(
        tag="arc.kickoff", arc=arc, summary=narrative_text, location=preferred, witnesses=witnesses
    )
    return arc


async def process_arc_beats(state) -> int:
    """Fire any arc beats whose nextBeatAt has elapsed. Returns count fired."""
    active = await _read_active_arcs()
    fired = 0
    now = _now_ms()
    for arc in active:
        next_at = int(arc.get("nextBeatAt", 0))
        if next_at <= now:
            await _fire_beat(state, arc)
            fired += 1
    return fired


async def _fire_beat(state, arc: dict) -> None:
    """Generate next beat for an arc; persist updates."""
    template = find_template(arc["templateId"])
    if not template:
        logger.warning("[arc:%s] template %s not found, abandoning", arc["arcId"][:8], arc["templateId"])
        await _abandon_arc(arc["arcId"])
        return

    # If past expiresAt, resolve the arc
    now = _now_ms()
    if int(arc.get("expiresAt", 0)) <= now:
        await _resolve_arc(state, arc, template)
        return

    # Generate a continuation beat
    beat_prompt = (
        f"Continue this narrative arc:\n{arc.get('narrativeKickoff', '')}\n\nWhat happens next, in one short paragraph?"
    )
    beat_text = await asyncio.to_thread(state.narrative_resolver.resolve, beat_prompt)

    # Update arc with next beat
    table = dynamo.worldgm_arcs_table()
    table.update_item(
        Key={"arcId": arc["arcId"]},
        UpdateExpression="SET nextBeatAt = :n, lastBeatAt = :l, lastBeatText = :t",
        ExpressionAttributeValues={
            ":n": str(now + 3_600_000),
            ":l": now,
            ":t": beat_text,
        },
    )
    logger.info("[arc:%s] beat fired", arc["arcId"][:8])

    await _emit_arc_event(
        tag="arc.beat",
        arc=arc,
        summary=beat_text,
        location=template.get("preferred_location"),
        witnesses=arc.get("witnessIds", []),
    )


async def _resolve_arc(state, arc: dict, template: dict) -> None:
    """Mark arc resolved + emit a final 'arc.resolved' event."""
    table = dynamo.worldgm_arcs_table()
    table.update_item(
        Key={"arcId": arc["arcId"]},
        UpdateExpression="SET #status = :s, resolvedAt = :r",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":s": "resolved", ":r": _now_ms()},
    )
    logger.info("[arc:%s] resolved", arc["arcId"][:8])
    _emit_metric("ArcsResolved")

    await _emit_arc_event(
        tag="arc.resolved",
        arc=arc,
        summary=arc.get("lastBeatText") or arc.get("narrativeKickoff", ""),
        location=template.get("preferred_location"),
        witnesses=arc.get("witnessIds", []),
    )


async def _abandon_arc(arc_id: str) -> None:
    table = dynamo.worldgm_arcs_table()
    table.update_item(
        Key={"arcId": arc_id},
        UpdateExpression="SET #status = :s",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":s": "abandoned"},
    )
    _emit_metric("ArcsAbandoned")
