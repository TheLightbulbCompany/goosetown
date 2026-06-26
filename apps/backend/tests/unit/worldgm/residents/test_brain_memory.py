"""Integration tests for memory wiring in step_resident_brain (Phase 2).

Covers conversation-end residue, recall injection, and the flag gate. Mirrors
``test_brain_agentic.py``'s fake-daemon setup; injects a fake store / model /
town_service and flips ``settings.RESIDENT_MEMORY_ENABLED`` via monkeypatch.
"""

from __future__ import annotations

import worldgm.residents.brain as brain_mod
from core.config import settings
from worldgm.residents.behavior import ResidentRuntime
from worldgm.residents.brain import read_status, step_resident_brain
from worldgm.residents.personas import Persona

_PERSONA = Persona(
    name="ada_lovelace",
    display_name="Ada Lovelace",
    personality="Pattern-lover.",
    appearance="Green gown.",
    traits=("curious",),
    hangout="library",
    sleep_hour=23,
    wake_hour=7,
)

_MY_ID = "uuid-ada"
_OTHER_ID = "uuid-tesla"
_OTHER_NAME = "Nikola Tesla"


class _FakeDaemon:
    def __init__(self, state):
        self.state = state
        self.commands: list[dict] = []

    async def handle_tool_command(self, cmd):
        self.commands.append(cmd)
        return {"status": "ok"}


class _FakeModel:
    def __init__(self, text="", raises=False):
        self._text, self._raises, self.calls = text, raises, 0

    def sample_text(self, prompt, **kw):
        self.calls += 1
        if self._raises:
            raise RuntimeError("x")
        return self._text


class _FakeStore:
    """Records put() calls; serves about() from a seeded list."""

    def __init__(self, seeded_about=None):
        self.puts: list[dict] = []
        self._about = seeded_about or {}

    def put(self, agent_id, subject, kind, text, importance, created_at, source_ids=()):
        item = {
            "agentId": agent_id,
            "subject": subject,
            "kind": kind,
            "text": text,
            "importance": importance,
            "createdAt": created_at,
        }
        self.puts.append(item)
        return item

    def about(self, agent_id, subject, limit=20):
        return self._about.get(subject, [])

    def recent(self, agent_id, limit=100):
        return []

    def mark_reflected(self, agent_id, created_ats):
        pass


class _FakeTownService:
    def __init__(self):
        self.affinity_updates: list[tuple] = []
        self.stance_updates: list[tuple] = []

    def update_relationship(self, a, b, affinity_delta=0):
        self.affinity_updates.append((a, b, affinity_delta))

    def upsert_relationship_stance(self, a, b, stance_a_to_b=""):
        self.stance_updates.append((a, b, stance_a_to_b))


def _runtime(location="library"):
    return ResidentRuntime(
        name="ada_lovelace",
        location=location,
        state="idle",
        idle_ticks=2,
        hangout="library",
        location_context="town",
        sleep_hour=23,
        wake_hour=7,
    )


def _state(**over):
    agent = dict(location="library", location_context="town", activity="idle")
    agent.update(over.pop("agent", {}))
    base = dict(
        agent=agent,
        nearby=[{"agent_id": _OTHER_ID, "name": _OTHER_NAME, "display_name": _OTHER_NAME}],
        pending_messages=[],
    )
    base.update(over)
    return base


def _conv(turns=None):
    return {
        "conv_id": "c1",
        "participants": ["Ada Lovelace", _OTHER_NAME],
        "turns": turns
        if turns is not None
        else [
            {"speaker": _OTHER_NAME, "text": "A pattern?", "speakerType": "agent"},
            {"speaker": "Ada Lovelace", "text": "Indeed.", "speakerType": "agent"},
        ],
    }


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
# (d) flag OFF → no memory writes, behavior unchanged
# ---------------------------------------------------------------------------


async def test_flag_off_writes_no_memory(monkeypatch):
    monkeypatch.setattr(settings, "RESIDENT_MEMORY_ENABLED", False)
    store = _FakeStore()
    ts = _FakeTownService()
    monkeypatch.setattr(brain_mod, "memory_store_from_env", lambda: store)
    rt = _runtime()
    rt.last_conv_id = "c1"
    rt.last_conv_turns = _conv()["turns"]
    # active_conversation gone → would reflect if enabled; flag is off so it must not.
    model = _FakeModel('{"action": "idle"}')
    d = _FakeDaemon(_state())
    await _step(d, rt, read_status(d), model, town_service=ts)
    assert store.puts == []
    assert ts.affinity_updates == []
    assert "c1" not in rt.reflected_conv_ids


# ---------------------------------------------------------------------------
# (b1) while IN a conversation → transcript cached, no residue yet
# ---------------------------------------------------------------------------


async def test_in_conversation_caches_transcript_no_residue(monkeypatch):
    store = _FakeStore()
    ts = _FakeTownService()
    _enable_memory(monkeypatch, store, ts)
    conv = _conv()
    # in conversation + a pending message so the brain proceeds to the LLM path
    d = _FakeDaemon(
        _state(
            active_conversation=conv,
            pending_messages=[{"from": _OTHER_NAME, "text": "A pattern?"}],
        )
    )
    model = _FakeModel('{"action": "say", "message": "Indeed, look closer."}')
    rt = _runtime()
    await _step(d, rt, read_status(d), model, town_service=ts)
    assert rt.last_conv_id == "c1"
    assert rt.last_conv_turns == conv["turns"]
    assert store.puts == []  # no residue while still talking
    assert "c1" not in rt.reflected_conv_ids


# ---------------------------------------------------------------------------
# (b2)/(c) conversation just ended → reflect once, not twice
# ---------------------------------------------------------------------------


async def test_conversation_end_triggers_one_reflection(monkeypatch):
    store = _FakeStore()
    ts = _FakeTownService()
    _enable_memory(monkeypatch, store, ts)

    calls = {"n": 0}

    async def _fake_reflect(model, st, town_service, my_id, other_id, other_name, transcript, now_iso):
        calls["n"] += 1
        st.put(my_id, other_name, "observation", "they were sharp", 5, now_iso)
        town_service.update_relationship(my_id, other_id, affinity_delta=3)
        return True

    monkeypatch.setattr(brain_mod.reflection, "reflect_on_conversation", _fake_reflect)

    rt = _runtime()
    rt.last_conv_id = "c1"
    rt.last_conv_turns = _conv()["turns"]
    model = _FakeModel('{"action": "idle"}')

    # Tick 1: active_conversation is None → reflect.
    d = _FakeDaemon(_state())  # nobody nearby, no conversation
    await _step(d, rt, read_status(d), model, town_service=ts)
    assert calls["n"] == 1
    assert "c1" in rt.reflected_conv_ids
    assert rt.memories_since_reflection == 1
    assert len(store.puts) == 1
    assert store.puts[0]["subject"] == _OTHER_NAME
    assert ts.affinity_updates == [(_MY_ID, _OTHER_ID, 3)]

    # Tick 2: same ended conv → must NOT reflect again.
    rt.last_conv_id = "c1"
    rt.last_conv_turns = _conv()["turns"]
    await _step(d, rt, read_status(d), model, town_service=ts)
    assert calls["n"] == 1  # unchanged
    assert rt.memories_since_reflection == 1


# ---------------------------------------------------------------------------
# reflection resolves other_id from nearby when not given by transcript
# ---------------------------------------------------------------------------


async def test_reflection_resolves_other_from_nearby(monkeypatch):
    store = _FakeStore()
    ts = _FakeTownService()
    _enable_memory(monkeypatch, store, ts)

    captured = {}

    async def _fake_reflect(model, st, town_service, my_id, other_id, other_name, transcript, now_iso):
        captured.update(other_id=other_id, other_name=other_name)
        return True

    monkeypatch.setattr(brain_mod.reflection, "reflect_on_conversation", _fake_reflect)

    rt = _runtime()
    rt.last_conv_id = "c1"
    rt.last_conv_turns = _conv()["turns"]
    model = _FakeModel('{"action": "idle"}')
    # Other is still nearby so we can resolve its agent_id.
    d = _FakeDaemon(_state())
    await _step(d, rt, read_status(d), model, town_service=ts)
    assert captured["other_name"] == _OTHER_NAME
    assert captured["other_id"] == _OTHER_ID


# ---------------------------------------------------------------------------
# (e) recall injected when an addressee with prior memories is present
# ---------------------------------------------------------------------------


async def test_recall_injected_into_parsed_status(monkeypatch):
    seeded = {
        _OTHER_NAME: [
            {
                "kind": "observation",
                "text": "He fixates on resonance.",
                "createdAt": "2026-06-25T11:00:00+00:00",
                "importance": 7,
            },
        ]
    }
    store = _FakeStore(seeded_about=seeded)
    ts = _FakeTownService()
    _enable_memory(monkeypatch, store, ts)

    recall_calls = {"n": 0}
    real_recall = brain_mod.reflection.recall_about

    def _spy_recall(st, my_id, other_name, now_iso, k=3):
        recall_calls["n"] += 1
        return real_recall(st, my_id, other_name, now_iso, k=k)

    monkeypatch.setattr(brain_mod.reflection, "recall_about", _spy_recall)

    rt = _runtime()
    model = _FakeModel('{"action": "idle"}')
    d = _FakeDaemon(_state())  # Tesla nearby, no conversation
    status = read_status(d)
    await _step(d, rt, status, model, town_service=ts)
    assert recall_calls["n"] == 1
    assert "He fixates on resonance." in status["remembered"]


async def test_recall_skipped_when_no_one_nearby(monkeypatch):
    store = _FakeStore()
    ts = _FakeTownService()
    _enable_memory(monkeypatch, store, ts)

    recall_calls = {"n": 0}

    def _spy_recall(st, my_id, other_name, now_iso, k=3):
        recall_calls["n"] += 1
        return ""

    monkeypatch.setattr(brain_mod.reflection, "recall_about", _spy_recall)

    rt = _runtime()
    model = _FakeModel('{"action": "idle"}')
    d = _FakeDaemon(_state(nearby=[]))  # nobody around
    await _step(d, rt, read_status(d), model, town_service=ts)
    assert recall_calls["n"] == 0


# ---------------------------------------------------------------------------
# memory wiring is best-effort: a reflect failure does not abort the tick
# ---------------------------------------------------------------------------


async def test_reflection_failure_does_not_break_tick(monkeypatch):
    store = _FakeStore()
    ts = _FakeTownService()
    _enable_memory(monkeypatch, store, ts)

    async def _boom(*a, **kw):
        raise RuntimeError("ddb down")

    monkeypatch.setattr(brain_mod.reflection, "reflect_on_conversation", _boom)

    rt = _runtime()
    rt.last_conv_id = "c1"
    rt.last_conv_turns = _conv()["turns"]
    model = _FakeModel('{"action": "move", "destination": "cafe"}')
    d = _FakeDaemon(_state())
    # Must not raise; the normal decision still dispatches.
    await _step(d, rt, read_status(d), model, town_service=ts)
    assert d.commands == [{"action": "act", "payload": {"action": "move", "destination": "cafe"}}]
