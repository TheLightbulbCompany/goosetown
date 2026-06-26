"""TDD tests for the reflect-subject bug fix.

Bug: _maybe_reflect_on_ended_conversation passes participants=[] to
_other_participant_name, causing the fallback to pick "you" (the daemon's
self-label for the initiator's own turns) as the other participant →
subject="you" in every stored observation memory → recall_about("Baba Yaga")
never hits them.

Fix 1: Harden _other_participant_name to skip daemon self-labels ("you",
        "me", "i") in the transcript fallback.
Fix 2: Cache participants alongside turns in ResidentRuntime.last_conv_participants
        and pass them instead of [] when reflecting on an ended conversation.
"""

from __future__ import annotations

import worldgm.residents.brain as brain_mod
from core.config import settings
from worldgm.residents.behavior import ResidentRuntime
from worldgm.residents.brain import (
    _other_participant_name,
    read_status,
    step_resident_brain,
)
from worldgm.residents.personas import Persona

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PERSONA = Persona(
    name="tesla",
    display_name="Tesla",
    personality="Electrifying.",
    appearance="Black coat.",
    traits=("curious",),
    hangout="plaza",
    sleep_hour=23,
    wake_hour=7,
)

_MY_ID = "uuid-tesla"
_OTHER_ID = "uuid-baba"
_OTHER_NAME = "Baba Yaga"


class _FakeDaemon:
    def __init__(self, state):
        self.state = state
        self.commands: list[dict] = []

    async def handle_tool_command(self, cmd):
        self.commands.append(cmd)
        return {"status": "ok"}


class _FakeModel:
    def __init__(self, text='{"action": "idle"}'):
        self._text = text

    def sample_text(self, prompt, **kw):
        return self._text


class _FakeStore:
    def __init__(self):
        self.puts: list[dict] = []

    def put(self, agent_id, subject, kind, text, importance, created_at, source_ids=()):
        item = {"agentId": agent_id, "subject": subject, "kind": kind, "text": text}
        self.puts.append(item)
        return item

    def about(self, agent_id, subject, limit=20):
        return []

    def recent(self, agent_id, limit=100):
        return []

    def mark_reflected(self, agent_id, created_ats):
        pass


class _FakeTownService:
    def update_relationship(self, a, b, affinity_delta=0):
        pass

    def upsert_relationship_stance(self, a, b, stance_a_to_b=""):
        pass


def _runtime():
    return ResidentRuntime(
        name="tesla",
        location="plaza",
        state="idle",
        idle_ticks=2,
        hangout="plaza",
        location_context="town",
        sleep_hour=23,
        wake_hour=7,
    )


def _state(**over):
    agent = dict(location="plaza", location_context="town", activity="idle")
    base = dict(agent=agent, nearby=[], pending_messages=[])
    base.update(over)
    return base


def _enable_memory(monkeypatch, store, town_service):
    monkeypatch.setattr(settings, "RESIDENT_MEMORY_ENABLED", True)
    monkeypatch.setattr(brain_mod, "memory_store_from_env", lambda: store)


async def _step(daemon, runtime, status, model, **kw):
    return await step_resident_brain(
        daemon,
        runtime,
        status,
        model=model,
        persona=_PERSONA,
        now_utc_hour=12,
        agent_id=_MY_ID,
        **kw,
    )


# ---------------------------------------------------------------------------
# Unit tests for _other_participant_name
# ---------------------------------------------------------------------------


def test_picks_other_from_participants_list():
    """Participants list is present: returns the non-self name."""
    result = _other_participant_name(
        turns=[],
        participants=["Tesla", "Baba Yaga"],
        my_display_name="Tesla",
    )
    assert result == "Baba Yaga"


def test_transcript_fallback_skips_daemon_you_label():
    """With empty participants, the transcript fallback must skip 'you' and
    return the real other speaker's name."""
    turns = [
        {"speaker": "you", "text": "Hi there."},
        {"speaker": "Baba Yaga", "text": "Hello, dear."},
    ]
    result = _other_participant_name(
        turns=turns,
        participants=[],
        my_display_name="Tesla",
    )
    assert result == "Baba Yaga"


def test_transcript_fallback_returns_none_when_only_you():
    """When the only transcript speaker is 'you' (and participants is empty),
    returns None so reflection is skipped — must NOT return 'you'."""
    turns = [{"speaker": "you", "text": "Anybody there?"}]
    result = _other_participant_name(
        turns=turns,
        participants=[],
        my_display_name="Tesla",
    )
    assert result is None


def test_transcript_fallback_case_insensitive_self_skip():
    """'You', 'ME', 'I' in any capitalisation are treated as self-labels and skipped."""
    turns = [
        {"speaker": "You", "text": "Hello."},
        {"speaker": "ME", "text": "Also me."},
        {"speaker": "I", "text": "Still me."},
        {"speaker": "Baba Yaga", "text": "Yes?"},
    ]
    result = _other_participant_name(
        turns=turns,
        participants=[],
        my_display_name="Tesla",
    )
    assert result == "Baba Yaga"


# ---------------------------------------------------------------------------
# Integration: reflect uses cached participants so subject is real name
# ---------------------------------------------------------------------------


async def test_reflect_subject_is_real_name_not_you(monkeypatch):
    """_maybe_reflect_on_ended_conversation must use runtime.last_conv_participants
    so the stored observation memory has subject=<real other name>, not 'you'."""
    store = _FakeStore()
    ts = _FakeTownService()
    _enable_memory(monkeypatch, store, ts)

    captured: dict = {}

    async def _fake_reflect(model, st, town_service, my_id, other_id, other_name, transcript, now_iso):
        captured["other_name"] = other_name
        return True

    monkeypatch.setattr(brain_mod.reflection, "reflect_on_conversation", _fake_reflect)

    rt = _runtime()
    rt.last_conv_id = "c-baba"
    # Turns where the initiator's first turn is labelled "you" (daemon self-label).
    rt.last_conv_turns = [
        {"speaker": "you", "text": "Greetings, Baba Yaga."},
        {"speaker": "Baba Yaga", "text": "Welcome, child."},
    ]
    # Cache the participants so the reflect path can use them.
    rt.last_conv_participants = [_PERSONA.display_name, _OTHER_NAME]

    model = _FakeModel()
    d = _FakeDaemon(_state())
    await _step(d, rt, read_status(d), model, town_service=ts)

    assert "other_name" in captured, "reflect_on_conversation was never called"
    assert captured["other_name"] == _OTHER_NAME, (
        f"expected '{_OTHER_NAME}', got {captured['other_name']!r} — subject='you' bug is still present"
    )


async def test_in_conversation_caches_participants(monkeypatch):
    """While still in a conversation, the runtime must cache the participants list
    so it is available after the conversation ends."""
    store = _FakeStore()
    ts = _FakeTownService()
    _enable_memory(monkeypatch, store, ts)

    conv = {
        "conv_id": "c-baba",
        "participants": [_PERSONA.display_name, _OTHER_NAME],
        "turns": [
            {"speaker": "you", "text": "Hi."},
            {"speaker": _OTHER_NAME, "text": "Hi back."},
        ],
    }
    d = _FakeDaemon(
        _state(
            active_conversation=conv,
            pending_messages=[{"from": _OTHER_NAME, "text": "Hi back."}],
        )
    )
    model = _FakeModel('{"action": "say", "message": "How are you?"}')
    rt = _runtime()

    await _step(d, rt, read_status(d), model, town_service=ts)

    assert rt.last_conv_participants == [_PERSONA.display_name, _OTHER_NAME]
