from unittest.mock import MagicMock

from worldgm.world_snapshot import (
    RelationshipView,
    ResidentView,
    WorldSnapshot,
    build_snapshot,
    compute_signals,
)


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
        {
            "id": "a1",
            "displayName": "Sherlock Holmes",
            "currentLocation": "library",
            "location_context": "town",
            "mood": 20,
            "energy": 80,
            "traits": ["analytical"],
            "personalitySummary": "a detective",
            "activity": "read",
        },
        {
            "id": "a2",
            "displayName": "Mark Twain",
            "currentLocation": "library",
            "location_context": "town",
            "mood": -30,
            "energy": 50,
            "traits": ["wry"],
            "personalitySummary": "a humorist",
            "activity": "idle",
        },
    ]
    snap = build_snapshot(_state(agents), now_utc_hour=14)
    assert {r.name for r in snap.residents} == {"Sherlock Holmes", "Mark Twain"}
    holmes = next(r for r in snap.residents if r.name == "Sherlock Holmes")
    assert holmes.mood_label == "happy"  # mood 20 → happy
    assert sorted(snap.clusters["library"]) == ["Mark Twain", "Sherlock Holmes"]
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


def _resident(name, location="cafe", mood=0, traits=("neutral",), activity="idle"):
    return ResidentView(
        agent_id=name,
        name=name,
        location=location,
        context="town",
        mood=mood,
        mood_label="neutral",
        energy=100,
        traits=list(traits),
        persona="",
        activity=activity,
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
