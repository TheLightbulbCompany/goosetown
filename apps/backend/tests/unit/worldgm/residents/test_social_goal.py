import asyncio
from worldgm.residents import social_goal
from worldgm.residents.memory_store import MemoryStore
from worldgm.residents.personas import Persona


# ---------------------------------------------------------------------------
# Shared test fixtures
# ---------------------------------------------------------------------------

class FakeModel:
    def __init__(self, reply): self.reply = reply; self.calls = []
    def sample_text(self, prompt, **kw): self.calls.append(prompt); return self.reply


class RaisingModel:
    def sample_text(self, prompt, **kw): raise RuntimeError("nova is down")


class FakeTable:
    def __init__(self): self.items = []
    def put_item(self, Item): self.items.append(Item)
    def query(self, **kw):
        v = kw.get("ExpressionAttributeValues", {})
        out = [i for i in self.items if i["agentId"] == v.get(":a")]
        if ":s" in v: out = [i for i in out if i.get("subject") == v[":s"]]
        out.sort(key=lambda i: i["createdAt"], reverse=not kw.get("ScanIndexForward", True))
        return {"Items": out[:kw.get("Limit", len(out))]}
    def update_item(self, **kw):
        for i in self.items:
            if i["agentId"] == kw["Key"]["agentId"] and i["createdAt"] == kw["Key"]["createdAt"]:
                i["reflected"] = True


_PERSONA = Persona(
    name="test_resident",
    display_name="Test Resident",
    personality="A curious soul who loves debate.",
    appearance="Tall person with glasses.",
    traits=("curious", "talkative"),
    hangout="plaza",
    role="the town contrarian",
)

_NOW = "2026-06-25T10:00:00Z"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_generate_social_goal_returns_cleaned_goal():
    """Normal reply → returns the single-line goal string (stripped)."""
    model = FakeModel('  "hold court at the café"  ')
    store = MemoryStore(FakeTable())
    result = asyncio.run(
        social_goal.generate_social_goal(model, store, "agent-1", _PERSONA, "no relationships yet", _NOW)
    )
    assert result == "hold court at the café"


def test_generate_social_goal_returns_empty_on_model_exception():
    """Raising model → returns "" (best-effort, never propagates)."""
    store = MemoryStore(FakeTable())
    result = asyncio.run(
        social_goal.generate_social_goal(RaisingModel(), store, "agent-1", _PERSONA, "some context", _NOW)
    )
    assert result == ""


def test_generate_social_goal_returns_empty_on_blank_reply():
    """Empty / whitespace model reply → returns ""."""
    model = FakeModel("   ")
    store = MemoryStore(FakeTable())
    result = asyncio.run(
        social_goal.generate_social_goal(model, store, "agent-1", _PERSONA, "some context", _NOW)
    )
    assert result == ""


def test_generate_social_goal_prompt_includes_role():
    """The prompt handed to the model must mention persona.role."""
    model = FakeModel("find someone worth debating")
    store = MemoryStore(FakeTable())
    asyncio.run(
        social_goal.generate_social_goal(model, store, "agent-1", _PERSONA, "no prior bonds", _NOW)
    )
    assert model.calls, "expected model to be called"
    assert _PERSONA.role in model.calls[0], (
        f"persona.role '{_PERSONA.role}' not found in prompt:\n{model.calls[0]}"
    )


def test_generate_social_goal_prompt_includes_self_reflection_when_present():
    """When self-reflections exist in the store they appear in the prompt."""
    store = MemoryStore(FakeTable())
    store.put("agent-1", "self", "reflection", "I talk too much at parties", 7, _NOW)

    model = FakeModel("patch things up with Dwight")
    asyncio.run(
        social_goal.generate_social_goal(model, store, "agent-1", _PERSONA, "some context", _NOW)
    )
    assert model.calls, "expected model to be called"
    assert "I talk too much at parties" in model.calls[0], (
        f"self-reflection text not found in prompt:\n{model.calls[0]}"
    )


def test_generate_social_goal_caps_at_140_chars():
    """Goals longer than 140 chars are truncated to exactly 140."""
    long_reply = "x" * 200
    model = FakeModel(long_reply)
    store = MemoryStore(FakeTable())
    result = asyncio.run(
        social_goal.generate_social_goal(model, store, "agent-1", _PERSONA, "context", _NOW)
    )
    assert len(result) <= 140


def test_generate_social_goal_strips_only_first_line():
    """Multi-line reply → only the first non-empty line is returned."""
    model = FakeModel("seek out Tesla\nignore the rest\nand more")
    store = MemoryStore(FakeTable())
    result = asyncio.run(
        social_goal.generate_social_goal(model, store, "agent-1", _PERSONA, "context", _NOW)
    )
    assert result == "seek out Tesla"
