import asyncio
import json

from worldgm.residents import reflection
from worldgm.residents.memory_store import MemoryStore


class FakeModel:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def sample_text(self, prompt, **kw):
        self.calls.append(prompt)
        return self.reply


class FakeTownService:
    def __init__(self):
        self.affinity = []
        self.stance = []

    def update_relationship(self, a, b, affinity_delta=0, new_type=None):
        self.affinity.append((a, b, affinity_delta))

    def upsert_relationship_stance(self, a, b, stance_a_to_b=None, stance_b_to_a=None, stance_updated_at=None):
        self.stance.append((a, b, stance_a_to_b))


class FakeTable:  # minimal, see test_memory_store for the fuller version
    def __init__(self):
        self.items = []

    def put_item(self, Item):
        self.items.append(Item)

    def query(self, **kw):
        v = kw.get("ExpressionAttributeValues", {})
        out = [i for i in self.items if i["agentId"] == v.get(":a")]
        if ":s" in v:
            out = [i for i in out if i.get("subject") == v[":s"]]
        out.sort(
            key=lambda i: i["createdAt"],
            reverse=not kw.get("ScanIndexForward", True),
        )
        return {"Items": out[: kw.get("Limit", len(out))]}

    def update_item(self, **kw):
        for i in self.items:
            if i["agentId"] == kw["Key"]["agentId"] and i["createdAt"] == kw["Key"]["createdAt"]:
                i["reflected"] = True


def test_reflect_on_conversation_writes_memory_and_relationship():
    store = MemoryStore(FakeTable())
    ts = FakeTownService()
    model = FakeModel(
        json.dumps(
            {
                "summary": "LeBron only talks legacy",
                "importance": 6,
                "affinity_delta": -3,
                "note": "exhausting",
            }
        )
    )
    wrote = asyncio.run(
        reflection.reflect_on_conversation(
            model,
            store,
            ts,
            "me",
            "lebron-id",
            "LeBron",
            transcript=[{"speaker": "LeBron", "text": "legacy"}, {"speaker": "Me", "text": "ok"}],
            now_iso="2026-06-25T10:00:00Z",
        )
    )
    assert wrote is True
    mems = store.about("me", "LeBron")
    assert mems and mems[0]["text"] == "LeBron only talks legacy" and mems[0]["importance"] == 6
    assert ts.affinity == [("me", "lebron-id", -3)]
    assert ts.stance and ts.stance[0][2] == "exhausting"


def test_reflect_on_conversation_tolerates_bad_json():
    store = MemoryStore(FakeTable())
    ts = FakeTownService()
    wrote = asyncio.run(
        reflection.reflect_on_conversation(
            FakeModel("not json"),
            store,
            ts,
            "me",
            "x",
            "X",
            transcript=[{"speaker": "X", "text": "hi"}],
            now_iso="2026-06-25T10:00:00Z",
        )
    )
    assert wrote is False and store.about("me", "X") == [] and ts.affinity == []


def test_recall_about_renders_top_k():
    store = MemoryStore(FakeTable())
    store.put("me", "X", "observation", "first thing", 3, "2026-06-25T09:00:00Z")
    store.put("me", "X", "observation", "big thing", 9, "2026-06-25T08:00:00Z")
    out = reflection.recall_about(store, "me", "X", "2026-06-25T10:00:00Z", k=2)
    assert "big thing" in out and "first thing" in out
    assert reflection.recall_about(store, "me", "NoOne", "2026-06-25T10:00:00Z") == ""


def test_synthesize_reflection_stores_insights_and_marks_sources():
    store = MemoryStore(FakeTable())
    for i in range(6):
        store.put("me", "X", "observation", f"obs{i}", 5, f"2026-06-25T0{i}:00:00Z")
    model = FakeModel(json.dumps({"insights": [{"insight": "X is exhausting", "importance": 7}]}))
    n = asyncio.run(reflection.synthesize_reflection(model, store, "me", "2026-06-25T10:00:00Z", min_new=6))
    assert n == 1
    refl = [m for m in store.recent("me") if m["kind"] == "reflection"]
    assert refl and refl[0]["text"] == "X is exhausting" and refl[0]["importance"] >= 6
    assert all(m["reflected"] for m in store.recent("me") if m["kind"] == "observation")
    assert refl[0].get("sourceIds")


def test_synthesize_reflection_distinct_timestamps_for_multiple_insights():
    """Bug 1 regression: two insights from one call must get distinct createdAt SKs."""
    store = MemoryStore(FakeTable())
    for i in range(6):
        store.put("me", "Y", "observation", f"obs{i}", 5, f"2026-06-25T0{i}:00:00Z")
    two_insights = json.dumps(
        {
            "insights": [
                {"insight": "insight alpha", "importance": 7},
                {"insight": "insight beta", "importance": 8},
            ]
        }
    )
    model = FakeModel(two_insights)
    n = asyncio.run(reflection.synthesize_reflection(model, store, "me", "2026-06-25T10:00:00Z", min_new=6))
    assert n == 2
    refl = [m for m in store.recent("me") if m["kind"] == "reflection"]
    assert len(refl) == 2, f"Expected 2 reflection rows, got {len(refl)} (SK collision overwrites on real DDB)"
    created_ats = {m["createdAt"] for m in refl}
    assert len(created_ats) == 2, f"createdAt values must differ; got {created_ats}"


def test_synthesize_reflection_noop_below_threshold():
    store = MemoryStore(FakeTable())
    store.put("me", "X", "observation", "only one", 5, "2026-06-25T01:00:00Z")
    assert (
        asyncio.run(reflection.synthesize_reflection(FakeModel("{}"), store, "me", "2026-06-25T10:00:00Z", min_new=6))
        == 0
    )


def test_recall_self_returns_top_reflection_text():
    """recall_self surfaces the highest-scored reflection when reflections exist."""
    store = MemoryStore(FakeTable())
    store.put("me", "self", "reflection", "I thrive in solitude", 8, "2026-06-25T09:00:00Z")
    store.put("me", "self", "reflection", "Coffee is overrated", 4, "2026-06-25T08:00:00Z")
    out = reflection.recall_self(store, "me", "2026-06-25T10:00:00Z", k=2)
    assert "I thrive in solitude" in out
    assert "Coffee is overrated" in out


def test_recall_self_returns_empty_when_none():
    """recall_self returns "" when there are no self-reflections."""
    store = MemoryStore(FakeTable())
    assert reflection.recall_self(store, "me", "2026-06-25T10:00:00Z") == ""


def test_reflection_loop_observations_synthesize_then_recalled_via_recall_self():
    """Seed 6 unreflected observations → synthesize_reflection → recall_self surfaces the insight.

    This proves the observation→reflection→recall arm is no longer write-only.
    """
    store = MemoryStore(FakeTable())
    for i in range(6):
        store.put("me", "Y", "observation", f"obs{i}", 5, f"2026-06-25T0{i}:00:00Z")

    model = FakeModel(json.dumps({"insights": [{"insight": "I talk too much about myself", "importance": 7}]}))
    n = asyncio.run(reflection.synthesize_reflection(model, store, "me", "2026-06-25T10:00:00Z", min_new=6))
    assert n == 1

    # The insight must be retrievable via recall_self (not just sitting in DDB with no read path).
    out = reflection.recall_self(store, "me", "2026-06-25T10:00:00Z")
    assert "I talk too much about myself" in out
