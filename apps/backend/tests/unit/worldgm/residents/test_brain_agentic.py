"""Integration tests for step_resident_brain with the agentic (LLM) path."""

from __future__ import annotations

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


def _runtime(location="cafe"):
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
    agent = dict(location="cafe", location_context="town", activity="idle")
    agent.update(over.pop("agent", {}))
    base = dict(
        agent=agent,
        nearby=[{"agent_id": "uuid-tesla", "name": "Nikola Tesla"}],
        pending_messages=[],
    )
    base.update(over)
    return base


def test_read_status_surfaces_nearby_and_pending():
    d = _FakeDaemon(_state())
    status = read_status(d)
    assert status["nearby"] == [{"agent_id": "uuid-tesla", "name": "Nikola Tesla"}]
    assert status["pending_messages"] == []


async def test_sleep_window_goes_to_bed_without_llm():
    d = _FakeDaemon(_state())
    model = _FakeModel('{"action":"chat","target":"Nikola Tesla"}')
    rt = _runtime()
    # 02:00 UTC is inside Ada's [23,7) window.
    await step_resident_brain(d, rt, read_status(d), model=model, persona=_PERSONA, now_utc_hour=2)
    assert model.calls == 0
    assert d.commands == [{"action": "act", "payload": {"action": "move", "destination": "bed"}}]


async def test_stale_moving_state_does_not_freeze_resident():
    """The blocking handle_tool_command await is the real movement gate, not
    runtime.state. A resident whose state was left "moving" by a prior
    *completed* move must still decide on the next tick — otherwise it would
    freeze forever after its first move (state stays "moving" once the
    finished move's await returns)."""
    d = _FakeDaemon(_state())
    model = _FakeModel('{"action": "chat", "target": "Nikola Tesla", "message": "Hello!"}')
    rt = _runtime()
    rt.state = "moving"  # left over from a prior, already-completed move
    await step_resident_brain(d, rt, read_status(d), model=model, persona=_PERSONA, now_utc_hour=12)
    assert model.calls == 1
    assert d.commands == [{"action": "act", "payload": {"action": "chat", "target": "uuid-tesla", "message": "Hello!"}}]
    assert rt.state == "idle"  # reset at top; chat is not a move


async def test_in_conversation_no_pending_noops_without_llm():
    d = _FakeDaemon(_state(active_conversation={"conv_id": "c1", "participants": ["Nikola Tesla"], "turns": []}))
    model = _FakeModel()
    await step_resident_brain(d, _runtime(), read_status(d), model=model, persona=_PERSONA, now_utc_hour=12)
    assert model.calls == 0
    assert d.commands == []


async def test_in_conversation_with_pending_calls_llm_and_says():
    state = _state(
        active_conversation={
            "conv_id": "c1",
            "participants": ["Nikola Tesla"],
            "turns": [{"speaker": "Nikola Tesla", "text": "A pattern?"}],
        },
        pending_messages=[{"from": "Nikola Tesla", "text": "A pattern?"}],
    )
    d = _FakeDaemon(state)
    model = _FakeModel('{"action": "say", "message": "Indeed, look closer."}')
    await step_resident_brain(d, _runtime(), read_status(d), model=model, persona=_PERSONA, now_utc_hour=12)
    assert model.calls == 1
    assert d.commands == [{"action": "act", "payload": {"action": "say", "message": "Indeed, look closer."}}]


async def test_awake_settled_nearby_calls_llm_and_chats():
    d = _FakeDaemon(_state())
    model = _FakeModel('{"action": "chat", "target": "Nikola Tesla", "message": "Hello!"}')
    await step_resident_brain(d, _runtime(), read_status(d), model=model, persona=_PERSONA, now_utc_hour=12)
    assert model.calls == 1
    assert d.commands == [{"action": "act", "payload": {"action": "chat", "target": "uuid-tesla", "message": "Hello!"}}]


async def test_llm_idle_dispatches_nothing():
    d = _FakeDaemon(_state())
    model = _FakeModel('{"action": "idle"}')
    await step_resident_brain(d, _runtime(), read_status(d), model=model, persona=_PERSONA, now_utc_hour=12)
    assert model.calls == 1
    assert d.commands == []


async def test_model_none_falls_back_to_fsm():
    # Awake, settled, not at hangout -> FSM moves toward hangout (library). No LLM.
    d = _FakeDaemon(_state())
    await step_resident_brain(
        d, _runtime(location="cafe"), read_status(d), model=None, persona=_PERSONA, now_utc_hour=12
    )
    assert d.commands == [{"action": "act", "payload": {"action": "move", "destination": "library"}}]


async def test_wedged_model_mid_conversation_ends_deterministically():
    """If the model returns unusable output while it's the resident's turn
    (legal actions are only say/end), the brain must NOT re-bill the LLM every
    tick — it ends the conversation deterministically."""
    state = _state(
        active_conversation={"conv_id": "c7", "participants": ["Nikola Tesla"], "turns": []},
        pending_messages=[{"from": "Nikola Tesla", "text": "Well?"}],
    )
    d = _FakeDaemon(state)
    model = _FakeModel("the model rambles without valid JSON")
    await step_resident_brain(d, _runtime(), read_status(d), model=model, persona=_PERSONA, now_utc_hour=12)
    assert model.calls == 1
    assert d.commands == [{"action": "act", "payload": {"action": "end_conversation", "conv_id": "c7"}}]
