"""Cast-based storyline perception: a resident perceives the storyline whose CAST
contains their agent id, regardless of location (apartments included)."""

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


def test_storyline_text_none_for_empty_agent_id():
    arcs = [{"arcId": "x", "cast": ["a1"], "narrativeKickoff": "K"}]
    assert _storyline_text_for_agent(arcs, "") is None
