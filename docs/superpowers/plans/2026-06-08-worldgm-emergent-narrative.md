# Emergent Data-Grounded World GM — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the World GM's template-seeded fiction with an emergent narrative engine that authors every storyline from the real world state (positions, mood, relationships, conversations, activities) and delivers it to a real cast of agents — so storylines feature real residents, reach them wherever they are, and react to how they respond.

**Architecture:** Three units on the existing arc/delivery rails. `world_snapshot.py` reads live data into a structured snapshot + ranked signals (no LLM). `narrative_director.py` wraps the LLM: snapshot+signals → a structured `Injection` (real cast validated) or nothing. `arc_orchestrator.py` is rewritten to use cast-based arcs generated from the director, with per-agent concurrency + rate cap; delivery becomes cast-targeted (per-witness events) and residents perceive the storyline whose cast contains their id.

**Tech Stack:** Python 3.12, FastAPI backend, boto3/DynamoDB, Bedrock (`model_lite`), pytest. Spec: `docs/superpowers/specs/2026-06-07-worldgm-emergent-narrative-design.md`.

## File structure

| File | Responsibility |
|---|---|
| `apps/backend/worldgm/world_snapshot.py` (new) | `ResidentView`/`RelationshipView`/`ConversationView`/`WorldSnapshot`/`Signal` dataclasses; `build_snapshot(state)`; `compute_signals(snapshot)` |
| `apps/backend/worldgm/narrative_director.py` (new) | `Injection` dataclass; `author_kickoff(model, snapshot, signals)`; `author_beat(model, arc, snapshot)`; prompt builders + cast validation |
| `apps/backend/worldgm/arc_orchestrator.py` (rewrite) | cast-based arc lifecycle: `maybe_kickoff_arc`/`process_arc_beats` using the director; per-agent concurrency + rate cap + cadence |
| `apps/backend/worldgm/__main__.py` (modify) | env knobs; `_storyline_text_for_agent`; `_tick_one_resident` perception swap |
| `apps/backend/worldgm/arc_templates.py` (delete) | static fiction seeds — removed |
| `apps/backend/tests/unit/worldgm/test_world_snapshot.py` (new) | snapshot + signals tests |
| `apps/backend/tests/unit/worldgm/test_narrative_director.py` (new) | director + hallucination-guard tests |
| `apps/backend/tests/unit/worldgm/test_arc_orchestrator.py` (rewrite) | cast-based lifecycle tests |

All commands run from `apps/backend/` unless noted. Test runner: `uv run pytest`.

---

### Task 1: WorldSnapshot dataclasses + `build_snapshot`

**Files:**
- Create: `apps/backend/worldgm/world_snapshot.py`
- Test: `apps/backend/tests/unit/worldgm/test_world_snapshot.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/worldgm/test_world_snapshot.py
from unittest.mock import MagicMock

from worldgm.world_snapshot import build_snapshot


def _state(agents, conversations=None, relationships=None):
    state = MagicMock()
    ts = state.town_service
    ts.get_active_agents.return_value = agents
    ts.get_recent_conversations.return_value = conversations or []
    rel_map = relationships or {}

    def _rel(a, b):
        key = tuple(sorted([a, b]))
        return (rel_map.get(key, {"affinityScore": 0, "relationshipType": "acquaintance"}), False)

    ts.get_or_create_relationship.side_effect = _rel
    return state


def test_build_snapshot_collects_resident_views():
    agents = [
        {"id": "a1", "displayName": "Sherlock Holmes", "currentLocation": "library",
         "location_context": "town", "mood": 20, "energy": 80, "traits": ["analytical"],
         "personalitySummary": "a detective", "activity": "read"},
        {"id": "a2", "displayName": "Mark Twain", "currentLocation": "library",
         "location_context": "town", "mood": -30, "energy": 50, "traits": ["wry"],
         "personalitySummary": "a humorist", "activity": "idle"},
    ]
    snap = build_snapshot(_state(agents), now_utc_hour=14)
    assert {r.name for r in snap.residents} == {"Sherlock Holmes", "Mark Twain"}
    holmes = next(r for r in snap.residents if r.name == "Sherlock Holmes")
    assert holmes.mood_label == "happy"  # mood 20 → happy
    assert snap.clusters["library"] == ["Sherlock Holmes", "Mark Twain"] or \
           sorted(snap.clusters["library"]) == ["Mark Twain", "Sherlock Holmes"]
    assert snap.hour_utc == 14


def test_build_snapshot_relationships_only_for_relevant_pairs():
    # Two co-located residents → one relevant pair fetched.
    agents = [
        {"id": "a1", "displayName": "A", "currentLocation": "cafe", "mood": 0, "energy": 100},
        {"id": "a2", "displayName": "B", "currentLocation": "cafe", "mood": 0, "energy": 100},
        {"id": "a3", "displayName": "C", "currentLocation": "library", "mood": 0, "energy": 100},
    ]
    rels = {("a1", "a2"): {"affinityScore": 40, "relationshipType": "friend"}}
    snap = build_snapshot(_state(agents, relationships=rels), now_utc_hour=9)
    pairs = {(r.a, r.b) for r in snap.relationships}
    assert ("A", "B") in pairs or ("B", "A") in pairs
    # C is alone → no pair involving C
    assert not any("C" in (r.a, r.b) for r in snap.relationships)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/worldgm/test_world_snapshot.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'worldgm.world_snapshot'`.

- [ ] **Step 3: Write minimal implementation**

```python
# apps/backend/worldgm/world_snapshot.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/worldgm/test_world_snapshot.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/worldgm/world_snapshot.py apps/backend/tests/unit/worldgm/test_world_snapshot.py
git commit -m "feat(worldgm): WorldSnapshot + build_snapshot (real-data read layer)"
```

---

### Task 2: `compute_signals` + Signal taxonomy

**Files:**
- Modify: `apps/backend/worldgm/world_snapshot.py` (add `Signal` + `compute_signals`)
- Test: `apps/backend/tests/unit/worldgm/test_world_snapshot.py` (add cases)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/worldgm/test_world_snapshot.py
from worldgm.world_snapshot import (
    ConversationView,
    RelationshipView,
    ResidentView,
    Signal,
    WorldSnapshot,
    compute_signals,
)


def _resident(name, location="cafe", mood=0, traits=("neutral",), activity="idle"):
    return ResidentView(
        agent_id=name, name=name, location=location, context="town", mood=mood,
        mood_label="neutral", energy=100, traits=list(traits), persona="", activity=activity,
        awake=True,
    )


def test_compute_signals_detects_rivalry_boosted_by_co_location():
    snap = WorldSnapshot(
        residents=[_resident("A", "cafe"), _resident("B", "cafe")],
        relationships=[RelationshipView(a="A", b="B", affinity=-40, rel_type="rival")],
        clusters={"cafe": ["A", "B"]},
    )
    sigs = compute_signals(snap)
    rivalry = [s for s in sigs if s.kind == "rivalry"]
    assert rivalry and set(rivalry[0].cast) == {"A", "B"}
    assert rivalry[0].score > 1.0  # boosted above base by co-location


def test_compute_signals_detects_cluster_and_mood_extreme():
    snap = WorldSnapshot(
        residents=[_resident("A", "plaza"), _resident("B", "plaza"), _resident("C", "plaza", mood=-30)],
        clusters={"plaza": ["A", "B", "C"]},
    )
    kinds = {s.kind for s in compute_signals(snap)}
    assert "cluster" in kinds
    assert "mood_extreme" in kinds


def test_compute_signals_ranked_descending():
    snap = WorldSnapshot(
        residents=[_resident("A", "cafe"), _resident("B", "cafe")],
        relationships=[RelationshipView(a="A", b="B", affinity=-40, rel_type="rival")],
        clusters={"cafe": ["A", "B"]},
    )
    sigs = compute_signals(snap)
    assert sigs == sorted(sigs, key=lambda s: s.score, reverse=True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/worldgm/test_world_snapshot.py -q`
Expected: FAIL — `ImportError: cannot import name 'Signal'`.

- [ ] **Step 3: Write minimal implementation**

```python
# append to apps/backend/worldgm/world_snapshot.py

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
            signals.append(Signal("rivalry", 1.0 + boost, [rel.a, rel.b], None,
                                   f"{rel.a} & {rel.b}: affinity {rel.affinity} ({rel.rel_type})"))
        elif rel.affinity >= _BOND_AFFINITY:
            signals.append(Signal("bond", 1.0 + boost, [rel.a, rel.b], None,
                                   f"{rel.a} & {rel.b}: affinity {rel.affinity} ({rel.rel_type})"))

    for location, members in snap.clusters.items():
        if len(members) >= _CLUSTER_MIN:
            signals.append(Signal("cluster", 1.0 + 0.25 * (len(members) - _CLUSTER_MIN), list(members),
                                  location, f"{len(members)} residents gathered at the {location}"))

    for r in snap.residents:
        if abs(r.mood) >= _MOOD_EXTREME:
            signals.append(Signal("mood_extreme", 1.0 + (abs(r.mood) - _MOOD_EXTREME) / 25.0, [r.name],
                                  r.location, f"{r.name} is feeling {r.mood_label} (mood {r.mood})"))

    topic_counts: dict[str, list[str]] = {}
    for c in snap.recent_conversations:
        if c.topic:
            topic_counts.setdefault(c.topic.lower(), []).extend(c.participants)
    for topic, who in topic_counts.items():
        if len({*who}) >= 2 and sum(1 for c in snap.recent_conversations if c.topic.lower() == topic) >= 2:
            signals.append(Signal("topic_echo", 1.0, list({*who}), None, f"the town keeps circling: {topic}"))

    for r in snap.residents:
        if r.activity == "idle" and any(t.lower() in ("extrovert", "outgoing", "gregarious") for t in r.traits):
            if len(snap.clusters.get(r.location, [])) <= 1:
                signals.append(Signal("loner", 0.5, [r.name], r.location, f"{r.name} is alone and restless"))

    signals.sort(key=lambda s: s.score, reverse=True)
    return signals
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/worldgm/test_world_snapshot.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/worldgm/world_snapshot.py apps/backend/tests/unit/worldgm/test_world_snapshot.py
git commit -m "feat(worldgm): compute_signals — deterministic ranked story hooks"
```

---

### Task 3: `narrative_director` — Injection + author_kickoff/author_beat + hallucination guard

**Files:**
- Create: `apps/backend/worldgm/narrative_director.py`
- Test: `apps/backend/tests/unit/worldgm/test_narrative_director.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/worldgm/test_narrative_director.py
from unittest.mock import MagicMock

from worldgm.narrative_director import author_beat, author_kickoff
from worldgm.world_snapshot import ResidentView, Signal, WorldSnapshot


def _snap():
    return WorldSnapshot(
        residents=[
            ResidentView("a1", "Sherlock Holmes", "cafe", "town", 0, "neutral", 100, [], "", "idle", True),
            ResidentView("a2", "Mark Twain", "cafe", "town", 0, "neutral", 100, [], "", "idle", True),
        ],
        clusters={"cafe": ["Sherlock Holmes", "Mark Twain"]},
    )


def _model(text):
    m = MagicMock()
    m.sample_text.return_value = text
    return m


def test_author_kickoff_parses_structured_output():
    model = _model("LOCATION: cafe\nCAST: Sherlock Holmes, Mark Twain\n"
                   "NARRATIVE: Holmes eyes Twain over a cold coffee, a riddle forming.")
    inj = author_kickoff(model, _snap(), [Signal("cluster", 2.0, ["Sherlock Holmes", "Mark Twain"], "cafe", "n")])
    assert inj is not None
    assert set(inj.cast) == {"Sherlock Holmes", "Mark Twain"}
    assert inj.location == "cafe"
    assert "Holmes" in inj.narrative


def test_author_kickoff_rejects_hallucinated_cast():
    # Model invents 'Anna the librarian' who is not in the snapshot → cast filtered;
    # only real residents survive.
    model = _model("LOCATION: cafe\nCAST: Anna the Librarian, Sherlock Holmes\n"
                   "NARRATIVE: Anna interrupts Holmes.")
    inj = author_kickoff(model, _snap(), [])
    assert inj is not None
    assert inj.cast == ["Sherlock Holmes"]  # Anna dropped


def test_author_kickoff_all_hallucinated_returns_none():
    model = _model("LOCATION: cafe\nCAST: Anna, Clara\nNARRATIVE: two strangers chat.")
    assert author_kickoff(model, _snap(), []) is None


def test_author_kickoff_nothing_returns_none():
    assert author_kickoff(_model("NOTHING"), _snap(), []) is None


def test_author_kickoff_prompt_names_residents_and_forbids_invention():
    model = _model("NOTHING")
    author_kickoff(model, _snap(), [Signal("cluster", 2.0, ["Sherlock Holmes"], "cafe", "hooknote")])
    prompt = model.sample_text.call_args.args[0]
    assert "Sherlock Holmes" in prompt and "Mark Twain" in prompt
    assert "hooknote" in prompt
    assert "do not invent" in prompt.lower()


def test_author_beat_returns_none_on_done():
    arc = {"narrativeKickoff": "k", "cast": ["a1", "a2"]}
    assert author_beat(_model("DONE"), arc, _snap()) is None


def test_author_beat_returns_text():
    arc = {"narrativeKickoff": "k", "cast": ["a1", "a2"]}
    assert author_beat(_model("They argue bitterly."), arc, _snap()) == "They argue bitterly."
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/worldgm/test_narrative_director.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'worldgm.narrative_director'`.

- [ ] **Step 3: Write minimal implementation**

```python
# apps/backend/worldgm/narrative_director.py
"""LLM-backed narrative director: turn a WorldSnapshot + signals into a grounded
storyline injection (real cast validated) or nothing. See the design spec."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from worldgm.world_snapshot import Signal, WorldSnapshot

logger = logging.getLogger(__name__)

_MAX_SIGNALS = 5


@dataclass
class Injection:
    narrative: str
    cast: list[str]
    location: str | None


def _serialize_snapshot(snap: WorldSnapshot) -> str:
    lines = [f"Time: {snap.hour_utc:02d}:00 UTC", "Residents present:"]
    for r in snap.residents:
        bits = [f"- {r.name} at the {r.location}, feeling {r.mood_label}"]
        if r.persona:
            bits.append(f"({r.persona})")
        if r.activity and r.activity != "idle":
            bits.append(f"— {r.activity}")
        lines.append(" ".join(bits))
    if snap.relationships:
        lines.append("Relationships:")
        for rel in snap.relationships:
            lines.append(f"- {rel.a} & {rel.b}: {rel.rel_type} (affinity {rel.affinity})")
    if snap.recent_conversations:
        lines.append("Recently discussed:")
        for c in snap.recent_conversations[:5]:
            if c.topic:
                lines.append(f"- {', '.join(c.participants)}: {c.topic}")
    return "\n".join(lines)


def _real_names(snap: WorldSnapshot) -> set[str]:
    return {r.name for r in snap.residents}


def _parse(text: str) -> tuple[str | None, list[str], str]:
    """Parse 'LOCATION: x / CAST: a, b / NARRATIVE: ...' → (location, cast, narrative)."""
    location: str | None = None
    cast: list[str] = []
    narrative_parts: list[str] = []
    in_narrative = False
    for line in text.splitlines():
        upper = line.strip().upper()
        if upper.startswith("LOCATION:"):
            location = line.split(":", 1)[1].strip() or None
        elif upper.startswith("CAST:"):
            cast = [n.strip() for n in line.split(":", 1)[1].split(",") if n.strip()]
        elif upper.startswith("NARRATIVE:"):
            in_narrative = True
            narrative_parts.append(line.split(":", 1)[1].strip())
        elif in_narrative:
            narrative_parts.append(line.strip())
    return location, cast, " ".join(p for p in narrative_parts if p).strip()


def author_kickoff(model, snap: WorldSnapshot, signals: list[Signal]) -> Injection | None:
    """Author a grounded storyline kickoff, or None if the model declines or its
    cast contains no real residents (hallucination guard)."""
    if not snap.residents:
        return None
    hooks = "\n".join(f"- {s.note}" for s in signals[:_MAX_SIGNALS]) or "- (none; use the scene as-is)"
    prompt = (
        "You are the unseen game-master of a small AI town. You make it feel alive by "
        "introducing small, interesting, grounded happenings involving the REAL residents below.\n\n"
        f"{_serialize_snapshot(snap)}\n\n"
        f"Possible hooks you may use, combine, or ignore:\n{hooks}\n\n"
        "Author ONE short, vivid happening (2-3 sentences) featuring specific real residents by "
        "name. The cast may be together or apart; if apart, frame it as a letter, a rumor reaching "
        "them, or a memory surfacing. Only the residents listed above exist — do not invent any "
        "other named people, narrators, or visitors.\n"
        "Reply in EXACTLY this format:\n"
        "LOCATION: <one location or 'town'>\n"
        "CAST: <comma-separated real resident names>\n"
        "NARRATIVE: <2-3 sentences>\n"
        "If nothing interesting fits right now, reply with exactly: NOTHING"
    )
    text = model.sample_text(prompt, max_tokens=256)
    if "NOTHING" in text.upper() and len(text.strip()) < 40:
        return None
    location, cast, narrative = _parse(text)
    real = _real_names(snap)
    cast = [c for c in cast if c in real]
    if not cast or not narrative:
        logger.info("[director] kickoff rejected — no real cast or empty narrative")
        return None
    return Injection(narrative=narrative, cast=cast, location=location)


def author_beat(model, arc: dict, snap: WorldSnapshot) -> str | None:
    """Continue an active storyline from the fresh snapshot, or None when DONE."""
    prompt = (
        "Continue this town storyline.\n\n"
        f"Story so far:\n{arc.get('narrativeKickoff', '')}\n"
        f"{arc.get('lastBeatText', '')}\n\n"
        f"The town RIGHT NOW:\n{_serialize_snapshot(snap)}\n\n"
        "The residents may have responded since the last beat. Continue in ONE short paragraph "
        "reflecting what's actually happening now. Feature only the real residents above; do not "
        "introduce new named people. If the storyline has run its course, reply with exactly: DONE"
    )
    text = model.sample_text(prompt, max_tokens=256)
    if "DONE" in text.upper() and len(text.strip()) < 30:
        return None
    return text.strip() or None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/worldgm/test_narrative_director.py -q`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/worldgm/narrative_director.py apps/backend/tests/unit/worldgm/test_narrative_director.py
git commit -m "feat(worldgm): narrative_director — grounded LLM storyline author + hallucination guard"
```

---

### Task 4: Rewrite `arc_orchestrator` — cast-based arcs, snapshot generation, concurrency

**Files:**
- Rewrite: `apps/backend/worldgm/arc_orchestrator.py`
- Rewrite: `apps/backend/tests/unit/worldgm/test_arc_orchestrator.py`
- Delete: `apps/backend/worldgm/arc_templates.py`

- [ ] **Step 1: Write the failing tests** (replace the file contents)

```python
# tests/unit/worldgm/test_arc_orchestrator.py
import asyncio
from unittest.mock import MagicMock, patch

import pytest

from worldgm.narrative_director import Injection


@pytest.fixture(autouse=True)
def _stub_metric(monkeypatch):
    monkeypatch.setattr("worldgm.arc_orchestrator._emit_metric", lambda *a, **k: None)


def _state(residents_online=2):
    state = MagicMock()
    # last_known_locations: id -> location
    state.last_known_locations = {f"a{i}": "cafe" for i in range(residents_online)}
    return state


@patch("worldgm.arc_orchestrator.build_snapshot")
@patch("worldgm.arc_orchestrator.compute_signals", return_value=[])
@patch("worldgm.arc_orchestrator.author_kickoff")
@patch("worldgm.arc_orchestrator.emit_world_event")
@patch("worldgm.arc_orchestrator.dynamo.worldgm_arcs_table")
def test_kickoff_persists_cast_based_arc(mock_table, mock_emit, mock_author, mock_sigs, mock_snap):
    from worldgm.arc_orchestrator import maybe_kickoff_arc

    mock_table.return_value.query.return_value = {"Items": []}  # no active arcs
    snap = MagicMock()
    snap.residents = [MagicMock(agent_id="a0", name="A"), MagicMock(agent_id="a1", name="B")]
    mock_snap.return_value = snap
    mock_author.return_value = Injection(narrative="A and B spar.", cast=["A", "B"], location="cafe")

    state = _state()
    with patch("worldgm.arc_orchestrator.random.random", return_value=0.0):
        arc = asyncio.run(maybe_kickoff_arc(state))
    assert arc is not None
    item = mock_table.return_value.put_item.call_args.kwargs["Item"]
    assert item["cast"] == ["a0", "a1"]  # names mapped back to ids
    assert item["narrativeKickoff"] == "A and B spar."
    # delivered per-witness to the cast ids
    assert mock_emit.call_args.kwargs["witnesses"] == ["a0", "a1"]


@patch("worldgm.arc_orchestrator.build_snapshot")
@patch("worldgm.arc_orchestrator.compute_signals", return_value=[])
@patch("worldgm.arc_orchestrator.author_kickoff", return_value=None)
@patch("worldgm.arc_orchestrator.emit_world_event")
@patch("worldgm.arc_orchestrator.dynamo.worldgm_arcs_table")
def test_kickoff_noop_when_director_declines(mock_table, mock_emit, mock_author, mock_sigs, mock_snap):
    from worldgm.arc_orchestrator import maybe_kickoff_arc

    mock_table.return_value.query.return_value = {"Items": []}
    mock_snap.return_value = MagicMock(residents=[MagicMock(agent_id="a0", name="A")])
    with patch("worldgm.arc_orchestrator.random.random", return_value=0.0):
        arc = asyncio.run(maybe_kickoff_arc(_state()))
    assert arc is None
    mock_table.return_value.put_item.assert_not_called()


@patch("worldgm.arc_orchestrator.build_snapshot")
@patch("worldgm.arc_orchestrator.compute_signals", return_value=[])
@patch("worldgm.arc_orchestrator.author_kickoff")
@patch("worldgm.arc_orchestrator.emit_world_event")
@patch("worldgm.arc_orchestrator.dynamo.worldgm_arcs_table")
def test_kickoff_skips_agents_already_in_a_storyline(mock_table, mock_emit, mock_author, mock_sigs, mock_snap):
    from worldgm.arc_orchestrator import maybe_kickoff_arc

    # a0 already cast in an active arc → director must receive a snapshot WITHOUT a0.
    mock_table.return_value.query.return_value = {"Items": [{"arcId": "x", "status": "active", "cast": ["a0"]}]}
    snap = MagicMock()
    snap.residents = [MagicMock(agent_id="a0", name="A"), MagicMock(agent_id="a1", name="B")]
    mock_snap.return_value = snap
    mock_author.return_value = None
    with patch("worldgm.arc_orchestrator.random.random", return_value=0.0):
        asyncio.run(maybe_kickoff_arc(_state()))
    # The snapshot passed to author_kickoff has a0 filtered out.
    passed_snap = mock_author.call_args.args[1]
    assert all(r.agent_id != "a0" for r in passed_snap.residents)


@patch("worldgm.arc_orchestrator.build_snapshot")
@patch("worldgm.arc_orchestrator.author_beat", return_value="They reconcile.")
@patch("worldgm.arc_orchestrator.emit_world_event")
@patch("worldgm.arc_orchestrator.dynamo.worldgm_arcs_table")
def test_beat_advances_active_arc(mock_table, mock_emit, mock_beat, mock_snap):
    import time

    from worldgm.arc_orchestrator import process_arc_beats

    now = int(time.time() * 1000)
    arc = {"arcId": "x", "status": "active", "cast": ["a0", "a1"],
           "expiresAt": now + 3_600_000, "nextBeatAt": str(now - 100),
           "narrativeKickoff": "they fought"}
    mock_table.return_value.query.return_value = {"Items": [arc]}
    mock_snap.return_value = MagicMock()
    still = asyncio.run(process_arc_beats(_state()))
    assert len(still) == 1
    assert still[0]["lastBeatText"] == "They reconcile."


@patch("worldgm.arc_orchestrator.build_snapshot")
@patch("worldgm.arc_orchestrator.author_beat", return_value=None)  # DONE
@patch("worldgm.arc_orchestrator.emit_world_event")
@patch("worldgm.arc_orchestrator.dynamo.worldgm_arcs_table")
def test_beat_done_resolves_arc(mock_table, mock_emit, mock_beat, mock_snap):
    import time

    from worldgm.arc_orchestrator import process_arc_beats

    now = int(time.time() * 1000)
    arc = {"arcId": "x", "status": "active", "cast": ["a0"],
           "expiresAt": now + 3_600_000, "nextBeatAt": str(now - 100), "narrativeKickoff": "k"}
    mock_table.return_value.query.return_value = {"Items": [arc]}
    mock_snap.return_value = MagicMock()
    still = asyncio.run(process_arc_beats(_state()))
    assert still == []  # resolved → dropped
    resolved = [c for c in mock_table.return_value.update_item.call_args_list
                if "resolved" in str(c.kwargs.get("ExpressionAttributeValues", {}))]
    assert resolved
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/worldgm/test_arc_orchestrator.py -q`
Expected: FAIL — imports of `build_snapshot`/`author_kickoff` from arc_orchestrator don't exist yet.

- [ ] **Step 3: Rewrite the implementation**

```python
# apps/backend/worldgm/arc_orchestrator.py
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
    await _emit_arc_event(tag="arc.resolved", arc=arc, summary=arc.get("lastBeatText") or arc.get("narrativeKickoff", ""))


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
```

- [ ] **Step 4: Delete the obsolete templates module**

```bash
git rm apps/backend/worldgm/arc_templates.py
```

- [ ] **Step 5: Run tests + grep for stale references**

Run: `uv run pytest tests/unit/worldgm/test_arc_orchestrator.py -q`
Expected: PASS (5 passed).
Run: `grep -rn "arc_templates\|find_template\|ARC_TEMPLATES\|_pick_template" apps/backend/worldgm apps/backend/tests`
Expected: no matches (fix any that remain — e.g. an old import in `__main__.py` or a leftover test).

- [ ] **Step 6: Commit**

```bash
git add apps/backend/worldgm/arc_orchestrator.py apps/backend/tests/unit/worldgm/test_arc_orchestrator.py
git commit -m "feat(worldgm): cast-based emergent storylines (snapshot-driven, per-agent concurrency + rate cap)"
```

---

### Task 5: Cast-targeted resident perception in `__main__`

**Files:**
- Modify: `apps/backend/worldgm/__main__.py` (replace `_arc_text_for_location` with `_storyline_text_for_agent`; update `_tick_one_resident`)
- Test: `apps/backend/tests/unit/worldgm/test_tick_arcs.py` (adapt) + add a perception test

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/worldgm/test_storyline_perception.py
from worldgm.__main__ import _storyline_text_for_agent


def test_agent_perceives_storyline_when_in_cast_regardless_of_location():
    arcs = [{"arcId": "x", "cast": ["a1", "a2"], "narrativeKickoff": "K", "lastBeatText": "B"}]
    # a1 is in the cast → perceives the latest beat text, even from an apartment.
    assert _storyline_text_for_agent(arcs, "a1") == "B"
    # a3 not in any cast → perceives nothing.
    assert _storyline_text_for_agent(arcs, "a3") is None


def test_storyline_text_falls_back_to_kickoff_when_no_beat_yet():
    arcs = [{"arcId": "x", "cast": ["a1"], "narrativeKickoff": "K"}]
    assert _storyline_text_for_agent(arcs, "a1") == "K"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/worldgm/test_storyline_perception.py -q`
Expected: FAIL — `ImportError: cannot import name '_storyline_text_for_agent'`.

- [ ] **Step 3: Replace `_arc_text_for_location` with `_storyline_text_for_agent`**

In `apps/backend/worldgm/__main__.py`, delete the `_arc_text_for_location` function and add:

```python
def _storyline_text_for_agent(active_arcs: list[dict], agent_id: str) -> str | None:
    """Return the latest beat (or kickoff) of the storyline whose CAST contains this
    agent, regardless of location — so a resident in their apartment still perceives
    a thread they're part of. None if the agent is in no active storyline."""
    for arc in active_arcs:
        if agent_id in arc.get("cast", []):
            return arc.get("lastBeatText") or arc.get("narrativeKickoff")
    return None
```

Then update the line in `_tick_one_resident` (currently `__main__.py:281`):

```python
    # was: status["active_storyline"] = _arc_text_for_location(state.active_arcs, status.get("location", "unknown"))
    status["active_storyline"] = _storyline_text_for_agent(state.active_arcs, handle.config.agent_id if handle.config else "")
```

> Note: confirm how `_tick_one_resident` knows the resident's agent_id. The resident handle is `handle`; its `handle.config.agent_id` is set in `ResidentManager._supervise` (see `worldgm/residents/manager.py`). If `handle.config` is None (not yet provisioned), pass `""` → returns None.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/worldgm/test_storyline_perception.py tests/unit/worldgm/test_tick_arcs.py -q`
Expected: PASS. Fix `test_tick_arcs.py` if it referenced `_arc_text_for_location` — update it to the cast-based helper.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/worldgm/__main__.py apps/backend/tests/unit/worldgm/
git commit -m "feat(worldgm): cast-targeted storyline perception (apartments included)"
```

---

### Task 6: Full suite, lint, and CLAUDE.md env-var note

**Files:**
- Modify: `CLAUDE.md` (document the new `WORLDGM_*` env vars near the existing worldgm notes)

- [ ] **Step 1: Run the full backend suite + lint**

Run: `uv run pytest tests/ -q`
Expected: all pass. Investigate any failure referencing arcs/templates and fix.
Run: `uv run ruff check . && uv run ruff format --check .`
Expected: clean. Run `uv run ruff format .` if needed and re-commit.

- [ ] **Step 2: Document env vars in CLAUDE.md**

Add a short bullet list under the WorldGM/arc section noting: `WORLDGM_BEAT_INTERVAL_MIN` (~4), `WORLDGM_STORYLINE_DURATION_MIN` (~30), `WORLDGM_KICKOFF_COOLDOWN_MIN`/rate cap `WORLDGM_MAX_KICKOFFS_PER_MIN` (~2), `WORLDGM_SIGNAL_MIN_SCORE` (1.5) — emergent narrative tuning knobs.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(worldgm): document emergent-narrative env knobs"
```

---

## Self-review notes (resolved)

- **Spec coverage:** snapshot (T1), signals (T2), director + hallucination guard (T3), cast-based arcs + concurrency + rate cap + cadence (T4), cast-targeted delivery via `emit_world_event(witnesses=cast)` (T4) + perception (T5), templates removed (T4), env knobs (T4/T6). Affinity deltas / multi-scene-per-location / artifact delivery are explicitly deferred per the spec — no tasks, by design.
- **Type consistency:** `Injection.cast` is names from the director; `arc["cast"]` is agent_ids (mapped in T4 via `name_to_id`); `_storyline_text_for_agent` matches on ids — consistent. `mood_label` imported from `core.services.town_mood_engine`. `build_snapshot(state, *, now_utc_hour=None)` signature used identically in tests and T4.
- **Open follow-up for the implementer:** the `WORLDGM_KICKOFF_COOLDOWN_MIN` knob from the spec is folded into the rate cap (`_MAX_KICKOFFS_PER_MIN`) in v1; if a per-cast cooldown is later wanted, add it in `maybe_kickoff_arc`. Cadence defaults are intentionally fast (beats ~4 min) — tune via env after the first dev soak.
