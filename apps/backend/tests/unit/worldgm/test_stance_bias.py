"""Tests for stance_bias."""


def test_classify_hostile():
    from worldgm.stance_bias import _classify

    assert _classify("she's annoying and dismissive") == "hostile"
    assert _classify("Don't really like him, he's so manipulative") == "hostile"


def test_classify_affectionate():
    from worldgm.stance_bias import _classify

    assert _classify("she's a warm friend") == "affectionate"
    assert _classify("kind soul, I trust him completely") == "affectionate"


def test_classify_neutral():
    from worldgm.stance_bias import _classify

    assert _classify(None) == "neutral"
    assert _classify("") == "neutral"
    assert _classify("we have not spoken much") == "neutral"


def test_is_hostile_pair_either_side():
    from worldgm.stance_bias import is_hostile_pair

    # one-sided is enough for "hostile pair"
    assert is_hostile_pair("annoying person", "kind warm friend") is True
    assert is_hostile_pair("warm friend", "annoying person") is True
    assert is_hostile_pair("warm friend", "warm friend") is False


def test_is_affectionate_pair_requires_both_sides():
    from worldgm.stance_bias import is_affectionate_pair

    # affectionate is only true when both sides agree
    assert is_affectionate_pair("kind", "warm") is True
    assert is_affectionate_pair("kind", None) is False
    assert is_affectionate_pair("kind", "neutral") is False


def test_bias_templates_increases_tension_score_for_hostile_pair():
    from worldgm.stance_bias import bias_templates_by_stances

    templates = [
        {"id": "tension_arc", "tags": ["tension"]},
        {"id": "friend_arc", "tags": ["friendship"]},
        {"id": "weather", "tags": ["weather"]},
    ]
    pairs = [("alice", "bob", "annoying", "warm")]  # hostile pair
    scored = bias_templates_by_stances(templates, pairs)
    by_id = {tpl["id"]: score for score, tpl in scored}
    assert by_id["tension_arc"] == 2.0
    assert by_id["friend_arc"] == 1.0
    assert by_id["weather"] == 1.0


def test_bias_templates_increases_friendship_score_for_affectionate_pair():
    from worldgm.stance_bias import bias_templates_by_stances

    templates = [
        {"id": "tension_arc", "tags": ["tension"]},
        {"id": "friend_arc", "tags": ["friendship"]},
    ]
    pairs = [("alice", "bob", "kind warm friend", "trusted close")]  # affectionate pair
    scored = bias_templates_by_stances(templates, pairs)
    by_id = {tpl["id"]: score for score, tpl in scored}
    assert by_id["friend_arc"] == 2.0
    assert by_id["tension_arc"] == 1.0
