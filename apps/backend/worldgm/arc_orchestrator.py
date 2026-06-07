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


def _witness_names(state, witness_ids: list[str], *, limit: int = 8) -> list[str]:
    """Resolve witness agent_ids to display names via ``town_service``.

    Best-effort: any failure returns ``[]``. Caps the roster at ``limit`` so the
    grounded prompt stays focused when many residents are present.
    """
    if not witness_ids:
        return []
    try:
        agents = state.town_service.get_active_agents()
    except Exception:
        logger.warning("[arc] could not resolve witness names for grounding", exc_info=True)
        return []
    by_id = {a["id"]: (a.get("displayName") or a.get("agentName") or "a resident") for a in agents}
    return [by_id[w] for w in witness_ids if w in by_id][:limit]


def _ground(seed: str, names: list[str], location: str | None) -> str:
    """Wrap a narrative seed with the real residents present so the model
    features them by name instead of inventing townspeople (the WORLD panel
    used to narrate non-existent characters like "Anna the librarian").

    When no names are known (shouldn't happen — kickoff needs >=2 online agents)
    the bare seed is returned unchanged so behaviour degrades gracefully.
    """
    if not names:
        return seed
    roster = ", ".join(names)
    where = f"at the {location}" if location else "around town"
    return (
        f"{seed}\n\n"
        f"The residents present {where} right now are: {roster}. "
        f"Write 2-3 vivid sentences about how the scene unfolds, featuring these "
        f"residents by name. They are the town's only people — do NOT invent any "
        f"other named residents (no new librarians, friends, neighbours, or "
        f"narrators); the sole newcomer or new object is the one described above. "
        f"Ground it in how these specific residents would notice and react."
    )


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

    # Determine witnesses FIRST (agents at preferred_location, else all online)
    # so the kickoff narrative can be grounded in the residents actually present
    # — otherwise the model invents townspeople who don't exist.
    witnesses = []
    preferred = template.get("preferred_location")
    if preferred:
        witnesses = [agent_id for agent_id, loc in state.last_known_locations.items() if loc == preferred]
    if not witnesses:
        witnesses = list(state.last_known_locations.keys())

    grounded_prompt = _ground(template["kickoff_prompt"], _witness_names(state, witnesses), preferred)
    narrative_text = await asyncio.to_thread(state.narrative_resolver.resolve, grounded_prompt)

    # Persist arc state. preferred_location is copied from the template onto the
    # row so the resident-facing _arc_text_for_location (which reads the DDB row,
    # not the template) can target the arc by location. A None value means the
    # arc is town-wide.
    arc = {
        "arcId": arc_id,
        "templateId": template["id"],
        "status": "active",
        "startedAt": now,
        "expiresAt": now + template["duration_hours"] * 3_600_000,
        "narrativeKickoff": narrative_text,
        "preferred_location": preferred,
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
    await _emit_arc_event(tag="arc.kickoff", arc=arc, summary=narrative_text, location=preferred, witnesses=witnesses)
    return arc


async def process_arc_beats(state) -> list[dict]:
    """Fire any due beats / resolutions and return the arcs STILL active after
    processing (in-memory, fresh).

    Returning the in-memory list lets the caller cache it directly instead of
    re-reading the ``status-nextBeatAt-index`` GSI right after writing to it —
    that GSI is eventually consistent, so a same-tick re-read could miss a
    just-written ``lastBeatText`` or still show a just-resolved arc as active.
    """
    active = await _read_active_arcs()
    now = _now_ms()
    still_active: list[dict] = []
    for arc in active:
        next_at = int(arc.get("nextBeatAt", 0))
        if next_at <= now:
            resolved = await _fire_beat(state, arc)
            if resolved:
                continue  # resolved/abandoned — drop from the active set
        still_active.append(arc)
    return still_active


async def _fire_beat(state, arc: dict) -> bool:
    """Generate the next beat for an arc; persist updates and update ``arc``
    in place so the caller's in-memory copy is fresh. Returns True when the arc
    was resolved or abandoned (so the caller drops it from the active set)."""
    template = find_template(arc["templateId"])
    if not template:
        logger.warning("[arc:%s] template %s not found, abandoning", arc["arcId"][:8], arc["templateId"])
        await _abandon_arc(arc["arcId"])
        return True

    # If past expiresAt, resolve the arc
    now = _now_ms()
    if int(arc.get("expiresAt", 0)) <= now:
        await _resolve_arc(state, arc, template)
        return True

    # Generate a continuation beat — grounded in the arc's real witnesses so the
    # continuation doesn't drift into invented characters either.
    names = _witness_names(state, arc.get("witnessIds", []))
    roster = f" Feature only these residents by name: {', '.join(names)}." if names else ""
    beat_prompt = (
        f"Continue this narrative arc:\n{arc.get('narrativeKickoff', '')}\n\n"
        f"What happens next, in one short paragraph?{roster} "
        f"Do not introduce any new named townspeople."
    )
    beat_text = await asyncio.to_thread(state.narrative_resolver.resolve, beat_prompt)

    # Update arc with next beat
    next_beat_at = str(now + 3_600_000)
    table = dynamo.worldgm_arcs_table()
    table.update_item(
        Key={"arcId": arc["arcId"]},
        UpdateExpression="SET nextBeatAt = :n, lastBeatAt = :l, lastBeatText = :t",
        ExpressionAttributeValues={
            ":n": next_beat_at,
            ":l": now,
            ":t": beat_text,
        },
    )
    # Mirror the write into the in-memory copy so the cached active list the
    # caller builds is fresh without a GSI re-read.
    arc["lastBeatText"] = beat_text
    arc["lastBeatAt"] = now
    arc["nextBeatAt"] = next_beat_at
    logger.info("[arc:%s] beat fired", arc["arcId"][:8])

    await _emit_arc_event(
        tag="arc.beat",
        arc=arc,
        summary=beat_text,
        location=template.get("preferred_location"),
        witnesses=arc.get("witnessIds", []),
    )
    return False


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
