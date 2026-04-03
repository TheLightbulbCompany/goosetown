"""Tests for the state translator (agent_state + enrichment → TOWN_STATUS.md)."""

from core.services.state_translator import translate_agent_state


def _make_state(**overrides):
    base = {
        "event": "agent_state",
        "position": [18.0, 12.0],
        "location": "plaza",
        "context": "town",
        "activity": "idle",
        "available_actions": [
            {"cmd": "move", "destinations": ["library", "cafe"]},
            {"cmd": "idle", "activities": ["sit on bench", "watch fountain"]},
        ],
        "nearby_agents": [],
        "environment": {"time": "afternoon"},
    }
    base.update(overrides)
    return base


def _make_enrichment(**overrides):
    base = {
        "mood": "happy",
        "energy": 72,
        "relationships": {},
    }
    base.update(overrides)
    return base


def test_translate_basic_state():
    state = _make_state()
    enrichment = _make_enrichment()
    result = translate_agent_state(state, enrichment)

    assert "**Location:** Plaza (town)" in result
    assert "**Mood:** happy" in result
    assert "**Energy:** 72%" in result
    assert "`town_act move library`" in result
    assert "`town_act move cafe`" in result


def test_translate_with_nearby_agent():
    state = _make_state(
        nearby_agents=[{"id": "uuid2", "name": "Luna", "distance": 2.1, "activity": "reading"}],
        available_actions=[
            {"cmd": "move", "destinations": ["cafe"]},
            {"cmd": "idle", "activities": ["browse shelves"]},
            {"cmd": "interact", "targets": ["uuid2"]},
        ],
    )
    enrichment = _make_enrichment(
        relationships={
            "uuid2": {
                "name": "Luna",
                "type": "acquaintance",
                "interactions": 3,
                "last_topic": "philosophy",
            }
        }
    )
    result = translate_agent_state(state, enrichment)

    assert "Luna" in result
    assert "2 tiles away" in result
    assert "acquaintance" in result
    assert "talked 3 times" in result
    assert "last topic: philosophy" in result


def test_translate_with_no_enrichment():
    state = _make_state(nearby_agents=[{"id": "uuid3", "name": "Cosmo", "distance": 1.5, "activity": "walking"}])
    enrichment = _make_enrichment(relationships={})
    result = translate_agent_state(state, enrichment)

    assert "Cosmo (2 tiles away, walking)" in result
    # No relationship detail, so no dash separator for enrichment
    assert "acquaintance" not in result


def test_translate_includes_chat_option_for_nearby():
    state = _make_state(nearby_agents=[{"id": "uuid2", "name": "Luna", "distance": 2.0, "activity": "idle"}])
    enrichment = _make_enrichment()
    result = translate_agent_state(state, enrichment)

    assert "`town_act chat luna <message>`" in result
    assert "Talk to Luna" in result


def test_translate_always_includes_disconnect():
    # With nearby agents
    state = _make_state(nearby_agents=[{"id": "uuid2", "name": "Luna", "distance": 2.0, "activity": "idle"}])
    result = translate_agent_state(state, _make_enrichment())
    assert "`town_disconnect <HH:MM>`" in result

    # Without nearby agents
    state_empty = _make_state()
    result_empty = translate_agent_state(state_empty, _make_enrichment())
    assert "`town_disconnect <HH:MM>`" in result_empty


def test_translate_location_underscore_formatting():
    state = _make_state(location="activity_center")
    result = translate_agent_state(state, _make_enrichment())
    assert "**Location:** Activity Center (town)" in result


def test_translate_no_nearby_shows_nobody():
    state = _make_state(nearby_agents=[])
    result = translate_agent_state(state, _make_enrichment())
    assert "**Nearby:** nobody" in result
