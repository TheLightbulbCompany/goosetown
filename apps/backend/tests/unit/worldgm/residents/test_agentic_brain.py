"""Unit tests for the agentic resident brain (pure functions + decide)."""

from __future__ import annotations

from worldgm.residents.agentic_brain import (
    DecisionContext,
    build_decision_context,
    build_prompt,
    decide,
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
