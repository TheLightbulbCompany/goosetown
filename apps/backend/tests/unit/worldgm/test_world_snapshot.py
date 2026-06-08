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
