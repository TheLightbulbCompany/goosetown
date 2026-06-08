"""Build a structured snapshot of the live town from real data (no LLM, no writes).

The World GM consumes this each turn so every storyline it authors is grounded in
the actual residents, their state, relationships, and recent conversations — never
invented fiction. See docs/superpowers/specs/2026-06-07-worldgm-emergent-narrative-design.md.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field
from itertools import combinations

from core.services.town_mood_engine import mood_label

logger = logging.getLogger(__name__)

# Shared town locations agents cluster at. Apartments are private/per-user and are
# NOT merged into a shared cluster.
_HOME_LOCATIONS = {"bed", "apartment", "residence"}


@dataclass
class ResidentView:
    agent_id: str
    name: str
    location: str
    context: str
    mood: int
    mood_label: str
    energy: int
    traits: list[str]
    persona: str
    activity: str
    awake: bool


@dataclass
class RelationshipView:
    a: str
    b: str
    affinity: int
    rel_type: str


@dataclass
class ConversationView:
    participants: list[str]
    location: str
    topic: str
    minutes_ago: int


@dataclass
class WorldSnapshot:
    residents: list[ResidentView] = field(default_factory=list)
    relationships: list[RelationshipView] = field(default_factory=list)
    recent_conversations: list[ConversationView] = field(default_factory=list)
    clusters: dict[str, list[str]] = field(default_factory=dict)
    hour_utc: int = 0


def _coerce_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _traits(agent: dict) -> list[str]:
    t = agent.get("traits")
    if isinstance(t, list):
        return [str(x) for x in t]
    if isinstance(t, str) and t:
        return [t]
    return []


def build_snapshot(state, *, now_utc_hour: int | None = None) -> WorldSnapshot:
    """Assemble a WorldSnapshot from town_service. Best-effort: any single data
    source failing degrades to a partial snapshot rather than raising."""
    hour = now_utc_hour if now_utc_hour is not None else datetime.datetime.now(datetime.UTC).hour
    snap = WorldSnapshot(hour_utc=hour)

    try:
        agents = state.town_service.get_active_agents()
    except Exception:
        logger.warning("[snapshot] get_active_agents failed", exc_info=True)
        agents = []

    id_to_name: dict[str, str] = {}
    for a in agents:
        name = a.get("displayName") or a.get("agentName") or "a resident"
        aid = a.get("id", "")
        id_to_name[aid] = name
        mood = _coerce_int(a.get("mood"), 0)
        snap.residents.append(
            ResidentView(
                agent_id=aid,
                name=name,
                location=a.get("currentLocation") or "unknown",
                context=a.get("location_context") or "town",
                mood=mood,
                mood_label=mood_label(mood),
                energy=_coerce_int(a.get("energy"), 100),
                traits=_traits(a),
                persona=str(a.get("personalitySummary") or ""),
                activity=str(a.get("activity") or "idle"),
                awake=(a.get("currentLocation") not in _HOME_LOCATIONS),
            )
        )

    # Clusters: shared town locations only (apartments stay private).
    for r in snap.residents:
        if r.location in _HOME_LOCATIONS or r.location == "unknown":
            continue
        snap.clusters.setdefault(r.location, []).append(r.name)

    # Recent conversations → ConversationView. Resolve participant ids to names.
    try:
        convos = state.town_service.get_recent_conversations(limit=8)
    except Exception:
        logger.warning("[snapshot] get_recent_conversations failed", exc_info=True)
        convos = []
    recent_pairs: set[tuple[str, str]] = set()
    for c in convos:
        pids = c.get("participants") or [c.get("participantAId"), c.get("participantBId")]
        pids = [p for p in pids if p]
        names = [id_to_name.get(p, p) for p in pids]
        snap.recent_conversations.append(
            ConversationView(
                participants=names,
                location=c.get("location") or "unknown",
                topic=str(c.get("topicSummary") or "")[:80],
                minutes_ago=0,
            )
        )
        for x, y in combinations(sorted(pids), 2):
            recent_pairs.add((x, y))

    # Relevant relationship pairs = co-located pairs ∪ recent-conversation pairs.
    by_location: dict[str, list[str]] = {}
    for a in agents:
        loc = a.get("currentLocation")
        if loc and loc not in _HOME_LOCATIONS:
            by_location.setdefault(loc, []).append(a.get("id", ""))
    pairs: set[tuple[str, str]] = set(recent_pairs)
    for ids in by_location.values():
        for x, y in combinations(sorted(ids), 2):
            pairs.add((x, y))

    for a_id, b_id in pairs:
        try:
            rel, _ = state.town_service.get_or_create_relationship(a_id, b_id)
        except Exception:
            continue
        snap.relationships.append(
            RelationshipView(
                a=id_to_name.get(a_id, a_id),
                b=id_to_name.get(b_id, b_id),
                affinity=_coerce_int(rel.get("affinityScore"), 0),
                rel_type=str(rel.get("relationshipType") or "acquaintance"),
            )
        )

    return snap


# ---------------------------------------------------------------------------
# Signals — deterministic, ranked story hooks derived from the snapshot
# ---------------------------------------------------------------------------

_RIVALRY_AFFINITY = -30
_BOND_AFFINITY = 30
_MOOD_EXTREME = 25
_CLUSTER_MIN = 3


@dataclass
class Signal:
    kind: str
    score: float
    cast: list[str]
    location: str | None
    note: str


def _co_located(snap: WorldSnapshot, a: str, b: str) -> bool:
    return any(a in members and b in members for members in snap.clusters.values())


def compute_signals(snap: WorldSnapshot) -> list[Signal]:
    """Derive ranked, deterministic 'hooks' from the snapshot. v1 uses absolute
    thresholds + co-location boosts (no hour-over-hour deltas)."""
    signals: list[Signal] = []

    for rel in snap.relationships:
        boost = 1.0 if _co_located(snap, rel.a, rel.b) else 0.0
        if rel.affinity <= _RIVALRY_AFFINITY:
            signals.append(
                Signal(
                    "rivalry",
                    1.0 + boost,
                    [rel.a, rel.b],
                    None,
                    f"{rel.a} & {rel.b}: affinity {rel.affinity} ({rel.rel_type})",
                )
            )
        elif rel.affinity >= _BOND_AFFINITY:
            signals.append(
                Signal(
                    "bond",
                    1.0 + boost,
                    [rel.a, rel.b],
                    None,
                    f"{rel.a} & {rel.b}: affinity {rel.affinity} ({rel.rel_type})",
                )
            )

    for location, members in snap.clusters.items():
        if len(members) >= _CLUSTER_MIN:
            signals.append(
                Signal(
                    "cluster",
                    1.0 + 0.25 * (len(members) - _CLUSTER_MIN),
                    list(members),
                    location,
                    f"{len(members)} residents gathered at the {location}",
                )
            )

    for r in snap.residents:
        if abs(r.mood) >= _MOOD_EXTREME:
            signals.append(
                Signal(
                    "mood_extreme",
                    1.0 + (abs(r.mood) - _MOOD_EXTREME) / 25.0,
                    [r.name],
                    r.location,
                    f"{r.name} is feeling {r.mood_label} (mood {r.mood})",
                )
            )

    topic_counts: dict[str, list[str]] = {}
    for c in snap.recent_conversations:
        if c.topic:
            topic_counts.setdefault(c.topic.lower(), []).extend(c.participants)
    for topic, who in topic_counts.items():
        echoes = sum(1 for c in snap.recent_conversations if c.topic.lower() == topic)
        if len({*who}) >= 2 and echoes >= 2:
            signals.append(Signal("topic_echo", 1.0, list({*who}), None, f"the town keeps circling: {topic}"))

    for r in snap.residents:
        if r.activity == "idle" and any(t.lower() in ("extrovert", "outgoing", "gregarious") for t in r.traits):
            if len(snap.clusters.get(r.location, [])) <= 1:
                signals.append(Signal("loner", 0.5, [r.name], r.location, f"{r.name} is alone and restless"))

    signals.sort(key=lambda s: s.score, reverse=True)
    return signals
