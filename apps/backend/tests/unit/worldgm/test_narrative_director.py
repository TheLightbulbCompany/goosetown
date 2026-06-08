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
    model = _model(
        "LOCATION: cafe\nCAST: Sherlock Holmes, Mark Twain\n"
        "NARRATIVE: Holmes eyes Twain over a cold coffee, a riddle forming."
    )
    inj = author_kickoff(model, _snap(), [Signal("cluster", 2.0, ["Sherlock Holmes", "Mark Twain"], "cafe", "n")])
    assert inj is not None
    assert set(inj.cast) == {"Sherlock Holmes", "Mark Twain"}
    assert inj.location == "cafe"
    assert "Holmes" in inj.narrative


def test_author_kickoff_rejects_hallucinated_cast():
    # Model invents 'Anna the librarian' who is not in the snapshot → cast filtered;
    # only real residents survive.
    model = _model("LOCATION: cafe\nCAST: Anna the Librarian, Sherlock Holmes\nNARRATIVE: Anna interrupts Holmes.")
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
