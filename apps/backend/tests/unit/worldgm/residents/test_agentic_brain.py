"""Unit tests for the agentic resident brain (pure functions + decide)."""

from __future__ import annotations

from worldgm.residents.agentic_brain import (
    DecisionContext,
    _affinity_label,
    _clean_line,
    _relationship_phrase,
    build_decision_context,
    build_dialogue_prompt,
    build_prompt,
    decide,
    decide_and_speak,
    generate_dialogue,
    parse_action,
    to_command,
    validate_action,
)
from worldgm.residents.personas import Persona


def _ctx(**over):
    base = dict(
        name="sherlock_holmes",
        display_name="Sherlock Holmes",
        personality="Deductive savant.",
        traits=("brilliant", "aloof"),
        location="library",
        location_context="library",
        nearby=[{"agent_id": "uuid-ada", "display_name": "Ada Lovelace"}],
        active_conversation=None,
        pending_messages=[],
        available_activities=["read", "study"],
        legal_actions=frozenset({"move", "idle", "chat", "activity"}),
    )
    base.update(over)
    return DecisionContext(**base)


_PERSONA = Persona(
    name="sherlock_holmes",
    display_name="Sherlock Holmes",
    personality="Deductive savant.",
    appearance="Deerstalker cap.",
    traits=("brilliant", "aloof"),
    hangout="library",
    sleep_hour=3,
    wake_hour=11,
)


def _status(**over):
    base = dict(
        location="cafe",
        location_context="town",
        activity="idle",
        active_conversation=None,
        nearby=[{"agent_id": "uuid-ada", "name": "Ada Lovelace"}],
        pending_messages=[],
    )
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# parse_action
# ---------------------------------------------------------------------------


def test_parse_action_clean_json():
    assert parse_action('{"action": "idle"}') == {"action": "idle"}


def test_parse_action_json_embedded_in_prose():
    raw = 'Sure, here you go: {"action": "move", "destination": "cafe"} hope that helps'
    assert parse_action(raw) == {"action": "move", "destination": "cafe"}


def test_parse_action_malformed_returns_idle():
    assert parse_action("{not valid json") == {"action": "idle"}


def test_parse_action_no_json_returns_idle():
    assert parse_action("I think I will go to the cafe.") == {"action": "idle"}


def test_parse_action_missing_action_key_returns_idle():
    assert parse_action('{"destination": "cafe"}') == {"action": "idle"}


def test_parse_action_empty_returns_idle():
    assert parse_action("") == {"action": "idle"}


# ---------------------------------------------------------------------------
# validate_action
# ---------------------------------------------------------------------------


def test_validate_chat_to_nearby_ok():
    ctx = _ctx()
    action = {"action": "chat", "target": "Ada Lovelace", "message": "A word, Miss Lovelace?"}
    out = validate_action(action, ctx)
    assert out["action"] == "chat"
    assert out["_agent_id"] == "uuid-ada"
    assert out["message"] == "A word, Miss Lovelace?"


def test_validate_chat_to_absent_name_becomes_idle():
    ctx = _ctx()
    out = validate_action({"action": "chat", "target": "Nikola Tesla"}, ctx)
    assert out == {"action": "idle"}


def test_validate_say_requires_conversation():
    ctx = _ctx(active_conversation=None, legal_actions=frozenset({"move", "idle"}))
    assert validate_action({"action": "say", "message": "hi"}, ctx) == {"action": "idle"}


def test_validate_say_in_conversation_ok():
    ctx = _ctx(
        active_conversation={"conv_id": "c1", "participants": ["Ada Lovelace"], "turns": []},
        pending_messages=[{"from": "Ada Lovelace", "text": "Hello?"}],
        legal_actions=frozenset({"say", "end"}),
    )
    out = validate_action({"action": "say", "message": "Indeed."}, ctx)
    assert out == {"action": "say", "message": "Indeed.", "_conv_id": "c1"}


def test_validate_chat_empty_message_becomes_idle():
    # Backend _handle_chat rejects a blank opening; the brain must idle instead.
    ctx = _ctx()
    assert validate_action({"action": "chat", "target": "Ada Lovelace", "message": "   "}, ctx) == {"action": "idle"}
    assert validate_action({"action": "chat", "target": "Ada Lovelace"}, ctx) == {"action": "idle"}


def test_validate_end_stamps_conv_id():
    ctx = _ctx(
        active_conversation={"conv_id": "c9", "participants": ["Ada Lovelace"], "turns": []},
        legal_actions=frozenset({"say", "end"}),
    )
    assert validate_action({"action": "end"}, ctx) == {"action": "end", "_conv_id": "c9"}


def test_validate_move_invalid_destination_becomes_idle():
    ctx = _ctx()
    assert validate_action({"action": "move", "destination": "moon"}, ctx) == {"action": "idle"}


def test_validate_activity_not_here_becomes_idle():
    ctx = _ctx(available_activities=["read"])
    assert validate_action({"action": "activity", "name": "exercise"}, ctx) == {"action": "idle"}


def test_validate_idle_always_ok():
    assert validate_action({"action": "idle"}, _ctx()) == {"action": "idle"}


def test_validate_say_truncates_to_500_chars():
    ctx = _ctx(
        active_conversation={"conv_id": "c1", "participants": ["Ada Lovelace"], "turns": []},
        legal_actions=frozenset({"say", "end"}),
    )
    out = validate_action({"action": "say", "message": "x" * 800}, ctx)
    assert len(out["message"]) == 500


def test_validate_chat_truncates_message_to_500_chars():
    out = validate_action({"action": "chat", "target": "Ada Lovelace", "message": "y" * 800}, _ctx())
    assert len(out["message"]) == 500


def test_validate_illegal_action_kind_becomes_idle():
    # say is not in legal_actions for a non-conversation context.
    ctx = _ctx(legal_actions=frozenset({"move", "idle"}))
    assert validate_action({"action": "say", "message": "x"}, ctx) == {"action": "idle"}


# ---------------------------------------------------------------------------
# to_command
# ---------------------------------------------------------------------------


def test_to_command_chat():
    action = {"action": "chat", "_agent_id": "uuid-ada", "message": "Hello"}
    assert to_command(action) == {
        "action": "act",
        "payload": {"action": "chat", "target": "uuid-ada", "message": "Hello"},
    }


def test_to_command_say():
    assert to_command({"action": "say", "message": "Indeed.", "_conv_id": "c1"}) == {
        "action": "act",
        "payload": {"action": "say", "message": "Indeed.", "conv_id": "c1"},
    }


def test_to_command_end():
    assert to_command({"action": "end", "_conv_id": "c1"}) == {
        "action": "act",
        "payload": {"action": "end_conversation", "conv_id": "c1"},
    }


def test_to_command_move():
    assert to_command({"action": "move", "destination": "cafe"}) == {
        "action": "act",
        "payload": {"action": "move", "destination": "cafe"},
    }


def test_to_command_activity():
    assert to_command({"action": "activity", "name": "read"}) == {
        "action": "act",
        "payload": {"action": "activity", "activity_id": "read"},
    }


def test_to_command_idle_returns_none():
    assert to_command({"action": "idle"}) is None


# ---------------------------------------------------------------------------
# build_decision_context + build_prompt
# ---------------------------------------------------------------------------


def test_context_not_in_conversation_offers_chat_move_activity_idle():
    ctx = build_decision_context(_PERSONA, _status())
    assert "chat" in ctx.legal_actions  # someone nearby
    assert "move" in ctx.legal_actions
    assert "idle" in ctx.legal_actions
    assert "say" not in ctx.legal_actions
    # cafe has order_coffee -> activity offered
    assert "activity" in ctx.legal_actions
    assert "order_coffee" in ctx.available_activities


def test_context_in_conversation_offers_only_say_end():
    status = _status(active_conversation={"conv_id": "c1", "participants": ["Ada Lovelace"], "turns": []})
    ctx = build_decision_context(_PERSONA, status)
    assert ctx.legal_actions == frozenset({"say", "end"})


def test_context_no_nearby_no_chat():
    ctx = build_decision_context(_PERSONA, _status(nearby=[]))
    assert "chat" not in ctx.legal_actions


def test_context_nearby_normalized_to_display_name():
    ctx = build_decision_context(_PERSONA, _status())
    assert ctx.nearby[0]["display_name"] == "Ada Lovelace"
    assert ctx.nearby[0]["agent_id"] == "uuid-ada"


def test_context_carries_storyline_from_snapshot():
    ctx = build_decision_context(_PERSONA, _status(active_storyline="A lost dog wandered into the cafe."))
    assert ctx.active_storyline == "A lost dog wandered into the cafe."


def test_context_storyline_defaults_none():
    ctx = build_decision_context(_PERSONA, _status())
    assert ctx.active_storyline is None


def test_build_prompt_renders_storyline_when_present():
    ctx = build_decision_context(_PERSONA, _status(active_storyline="A lost dog wandered into the cafe."))
    prompt = build_prompt(ctx)
    assert "A lost dog wandered into the cafe." in prompt
    assert "happening" in prompt.lower()


def test_build_prompt_omits_storyline_when_absent():
    ctx = build_decision_context(_PERSONA, _status())
    prompt = build_prompt(ctx)
    assert "happening here" not in prompt.lower()


def test_build_prompt_marks_human_visitor_turn():
    """A turn tagged ``speakerType == "human"`` is rendered so the model reads
    it as a real person, plus a one-line nudge to respond directly."""
    ctx = _ctx(
        active_conversation={
            "conv_id": "c1",
            "participants": ["Ada Lovelace"],
            "turns": [
                {"speaker": "Ada Lovelace", "text": "Lovely day."},
                {"speaker": "Visitor-ab12", "text": "Hi! Can you hear me?", "speakerType": "human"},
            ],
        },
        legal_actions=frozenset({"say", "end"}),
    )
    prompt = build_prompt(ctx)
    # The human line is flagged inline...
    assert "Visitor-ab12 (a real person visiting GooseTown): Hi! Can you hear me?" in prompt
    # ...and the resident-line is rendered plainly (no human marker).
    assert "Ada Lovelace: Lovely day." in prompt
    # ...and the direct-response nudge fires.
    assert "real person" in prompt.lower()
    assert "directly" in prompt.lower()


def test_build_prompt_no_human_nudge_for_resident_only_turns():
    """Resident-only conversations must NOT get the human-visitor nudge."""
    ctx = _ctx(
        active_conversation={
            "conv_id": "c1",
            "participants": ["Ada Lovelace"],
            "turns": [{"speaker": "Ada Lovelace", "text": "Good morning."}],
        },
        legal_actions=frozenset({"say", "end"}),
    )
    prompt = build_prompt(ctx)
    assert "a real person watching the town" not in prompt.lower()
    assert "(a real person visiting GooseTown)" not in prompt


def test_context_carries_mood_energy_events():
    ctx = build_decision_context(
        _PERSONA, _status(mood=-20, energy=35, recent_events=["You finished reading.", "Ada arrived."])
    )
    assert ctx.mood == "sad (-20)"  # rendered like TOWN_STATUS.md
    assert ctx.energy == 35
    assert ctx.recent_events == ["You finished reading.", "Ada arrived."]


def test_context_mood_defaults_to_neutral_when_absent():
    ctx = build_decision_context(_PERSONA, _status())  # no mood key
    assert ctx.mood == "neutral"
    assert ctx.energy == 100


def test_build_prompt_shows_mood_energy_and_social_framing():
    ctx = build_decision_context(_PERSONA, _status(mood=-15, energy=20, nearby=[]))
    prompt = build_prompt(ctx)
    assert "sad (-15)" in prompt
    assert "energy 20/100" in prompt
    # Alone → the prompt nudges going where people gather, and the LLM can move.
    assert "no one" in prompt.lower()
    assert "where people gather" in prompt.lower()
    assert '"action": "move"' in prompt


def test_build_prompt_renders_recent_events():
    ctx = build_decision_context(_PERSONA, _status(recent_events=["A merchant set up a stall."]))
    prompt = build_prompt(ctx)
    assert "A merchant set up a stall." in prompt
    assert "recently" in prompt.lower()


def test_build_prompt_includes_persona_and_legal_actions_only():
    status = _status(
        active_conversation={
            "conv_id": "c1",
            "participants": ["Ada Lovelace"],
            "turns": [{"speaker": "Ada Lovelace", "text": "A puzzle for you."}],
        }
    )
    ctx = build_decision_context(_PERSONA, status)
    prompt = build_prompt(ctx)
    assert "Sherlock Holmes" in prompt
    assert "Deductive savant." in prompt
    assert '"action": "say"' in prompt  # legal
    assert '"action": "move"' not in prompt  # not legal mid-conversation
    assert "A puzzle for you." in prompt  # transcript rendered


# ---------------------------------------------------------------------------
# decide
# ---------------------------------------------------------------------------


class _FakeModel:
    """Stand-in for BedrockLanguageModel. sample_text is sync (like boto3)."""

    def __init__(self, text="", raises=False):
        self._text = text
        self._raises = raises
        self.calls = 0

    def sample_text(self, prompt, **kw):
        self.calls += 1
        if self._raises:
            raise RuntimeError("bedrock throttled")
        return self._text


async def test_decide_returns_validated_chat():
    ctx = build_decision_context(_PERSONA, _status())
    model = _FakeModel('{"action": "chat", "target": "Ada Lovelace", "message": "A word?"}')
    out = await decide(model, ctx)
    assert out["action"] == "chat"
    assert out["_agent_id"] == "uuid-ada"
    assert model.calls == 1


async def test_decide_model_error_returns_idle():
    ctx = build_decision_context(_PERSONA, _status())
    out = await decide(_FakeModel(raises=True), ctx)
    assert out == {"action": "idle"}


async def test_decide_illegal_output_returns_idle():
    ctx = build_decision_context(_PERSONA, _status())  # not in conversation
    out = await decide(_FakeModel('{"action": "say", "message": "hi"}'), ctx)
    assert out == {"action": "idle"}


# ---------------------------------------------------------------------------
# Relationship surfacing (Layer 3) + character bible + split-brain dialogue
# ---------------------------------------------------------------------------


_RICH_PERSONA = Persona(
    name="lebron",
    display_name="LeBron James",
    personality="Relentless competitor.",
    appearance="Tall.",
    traits=("competitive", "driven"),
    hangout="activity_center",
    voice="Calls people 'young king'. Wine and Akron.",
    obsessions=("legacy", "wine"),
    opinions=(("Ada Lovelace", "sharp mind, I respect that"),),
    current_life="Building the empire.",
)


def test_normalize_nearby_preserves_relationship_fields():
    # The backend (daemon_bridge) injects these; the brain must not discard them.
    status = _status(
        nearby=[
            {
                "agent_id": "uuid-ada",
                "display_name": "Ada Lovelace",
                "currentGoal": "finish the engine",
                "stanceFromMe": "intrigued",
                "stanceFromThem": "wary",
                "affinity": 35,
                "interactionCount": 6,
                "relationshipType": "friend",
            }
        ]
    )
    near = build_decision_context(_PERSONA, status).nearby[0]
    assert near["affinity"] == 35
    assert near["interactionCount"] == 6
    assert near["relationshipType"] == "friend"
    assert near["stanceFromMe"] == "intrigued"
    assert near["currentGoal"] == "finish the engine"


def test_affinity_label_bands():
    assert _affinity_label(80) == "close"
    assert _affinity_label(30) == "friendly"
    assert _affinity_label(0) == "neutral"
    assert _affinity_label(-30) == "frosty"
    assert _affinity_label(-80) == "hostile"


def test_relationship_phrase_combines_affinity_history_stance():
    phrase = _relationship_phrase({"affinity": 35, "interactionCount": 6, "stanceFromMe": "amused"})
    assert "friendly" in phrase
    assert "6" in phrase
    assert "amused" in phrase


def test_relationship_phrase_empty_when_nothing_set():
    assert _relationship_phrase({"agent_id": "x", "display_name": "Y"}) == ""


def test_build_prompt_renders_nearby_relationship():
    status = _status(
        nearby=[{"agent_id": "uuid-ada", "display_name": "Ada Lovelace", "affinity": 60, "interactionCount": 9}]
    )
    prompt = build_prompt(build_decision_context(_PERSONA, status))
    assert "Ada Lovelace" in prompt
    assert "close" in prompt  # affinity 60 -> close
    assert "9" in prompt


def test_context_carries_persona_bible_fields():
    ctx = build_decision_context(_RICH_PERSONA, _status())
    assert ctx.voice.startswith("Calls people")
    assert ctx.obsessions == ("legacy", "wine")
    assert dict(ctx.opinions)["Ada Lovelace"].startswith("sharp")


def test_clean_line_strips_prefix_and_quotes():
    assert _clean_line('LeBron James: "Hey now"', "LeBron James") == "Hey now"
    assert _clean_line('"Just a quote"', "LeBron James") == "Just a quote"
    assert _clean_line("plain line", "LeBron James") == "plain line"


def test_clean_line_caps_at_max():
    assert len(_clean_line("z" * 800, "X")) == 500


def _rich_ctx(**over):
    base = dict(
        name="lebron",
        display_name="LeBron James",
        personality="Relentless competitor.",
        traits=("competitive",),
        location="activity_center",
        location_context="town",
        nearby=[{"agent_id": "uuid-ada", "display_name": "Ada Lovelace", "affinity": 40, "interactionCount": 3}],
        active_conversation=None,
        pending_messages=[],
        available_activities=[],
        legal_actions=frozenset({"chat", "move", "idle"}),
        voice="Calls people 'young king'.",
        obsessions=("legacy", "wine"),
        opinions=(("Ada Lovelace", "sharp mind"),),
        current_life="Building the empire.",
    )
    base.update(over)
    return DecisionContext(**base)


def test_build_dialogue_prompt_chat_opening_uses_bible():
    ctx = _rich_ctx()
    action = {"action": "chat", "_agent_id": "uuid-ada", "target": "Ada Lovelace", "message": "fallback"}
    prompt = build_dialogue_prompt(ctx, action)
    assert "LeBron James" in prompt
    assert "young king" in prompt  # voice
    assert "sharp mind" in prompt  # opinion of the addressee
    assert "Ada Lovelace" in prompt
    assert "walking up" in prompt.lower()  # opening framing
    assert "ONLY the words you speak" in prompt
    assert "don't reuse these lines" not in prompt  # sample_lines scaffold removed


def test_build_dialogue_prompt_say_includes_history():
    ctx = _rich_ctx(
        active_conversation={
            "conv_id": "c1",
            "participants": ["LeBron James", "Ada Lovelace"],
            "turns": [{"speaker": "Ada Lovelace", "text": "What drives you?"}],
        },
        legal_actions=frozenset({"say", "end"}),
    )
    action = {"action": "say", "message": "fallback", "_conv_id": "c1"}
    prompt = build_dialogue_prompt(ctx, action)
    assert "What drives you?" in prompt
    assert "react to what they just said" in prompt.lower()


async def test_generate_dialogue_returns_cleaned_line():
    ctx = _rich_ctx()
    action = {"action": "chat", "_agent_id": "uuid-ada", "target": "Ada Lovelace", "message": "fallback"}
    line = await generate_dialogue(_FakeModel('LeBron James: "Earn it, young king."'), ctx, action)
    assert line == "Earn it, young king."


async def test_generate_dialogue_model_error_returns_empty():
    ctx = _rich_ctx()
    action = {"action": "chat", "_agent_id": "uuid-ada", "target": "Ada Lovelace", "message": "fb"}
    assert await generate_dialogue(_FakeModel(raises=True), ctx, action) == ""


async def test_decide_and_speak_dialogue_model_overrides_line():
    ctx = build_decision_context(_RICH_PERSONA, _status())
    decision = _FakeModel('{"action": "chat", "target": "Ada Lovelace", "message": "weak line"}')
    dialogue = _FakeModel("Strive for greatness, young king.")
    out = await decide_and_speak(decision, dialogue, ctx)
    assert out["action"] == "chat"
    assert out["message"] == "Strive for greatness, young king."
    assert dialogue.calls == 1


async def test_decide_and_speak_no_dialogue_model_keeps_decision_line():
    ctx = build_decision_context(_RICH_PERSONA, _status())
    decision = _FakeModel('{"action": "chat", "target": "Ada Lovelace", "message": "decision line"}')
    out = await decide_and_speak(decision, None, ctx)
    assert out["message"] == "decision line"


async def test_decide_and_speak_dialogue_failure_falls_back_to_decision_line():
    ctx = build_decision_context(_RICH_PERSONA, _status())
    decision = _FakeModel('{"action": "chat", "target": "Ada Lovelace", "message": "decision line"}')
    out = await decide_and_speak(decision, _FakeModel(raises=True), ctx)
    assert out["message"] == "decision line"


async def test_decide_and_speak_move_skips_dialogue_model():
    ctx = build_decision_context(_PERSONA, _status(nearby=[]))
    dialogue = _FakeModel("should not be called")
    out = await decide_and_speak(_FakeModel('{"action": "move", "destination": "cafe"}'), dialogue, ctx)
    assert out["action"] == "move"
    assert dialogue.calls == 0


# ---------------------------------------------------------------------------
# Task 1: sample_lines removal
# ---------------------------------------------------------------------------


def test_dialogue_prompt_has_no_sample_lines_section():
    from worldgm.residents.agentic_brain import DecisionContext, build_dialogue_prompt
    ctx = DecisionContext(
        name="dwight", display_name="Dwight Schrute", personality="beet farmer",
        traits=("intense",), location="cafe", location_context="town",
        nearby=[{"agent_id": "a1", "display_name": "LeBron James"}],
        active_conversation=None, pending_messages=[], available_activities=[],
        legal_actions=frozenset({"chat"}), voice="clipped, declarative",
    )
    prompt = build_dialogue_prompt(ctx, {"action": "chat", "_agent_id": "a1", "target": "LeBron James"})
    assert "how" in prompt.lower()  # voice still rendered
    assert "don't reuse these lines" not in prompt  # the sample-lines scaffold is gone


def test_decision_context_has_no_sample_lines_field():
    from worldgm.residents.agentic_brain import DecisionContext
    assert "sample_lines" not in DecisionContext.__dataclass_fields__
