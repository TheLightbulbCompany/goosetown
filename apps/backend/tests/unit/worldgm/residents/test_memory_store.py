from unittest.mock import MagicMock, patch

import worldgm.residents.memory_store as memory_store_mod
from worldgm.residents.memory_store import MemoryStore, retrieval_score


class FakeTable:
    def __init__(self): self.items = []; self.last_query = None
    def put_item(self, Item): self.items.append(Item)
    def query(self, **kw):
        self.last_query = kw
        # crude emulation: filter by KeyConditionExpression captured via ExpressionAttributeValues
        vals = kw.get("ExpressionAttributeValues", {})
        out = [i for i in self.items if i["agentId"] == vals.get(":a")]
        if ":s" in vals:
            out = [i for i in out if i.get("subject") == vals[":s"]]
        out.sort(key=lambda i: i["createdAt"], reverse=not kw.get("ScanIndexForward", True))
        return {"Items": out[: kw.get("Limit", len(out))]}
    def update_item(self, **kw):
        key = kw["Key"]
        for i in self.items:
            if i["agentId"] == key["agentId"] and i["createdAt"] == key["createdAt"]:
                i["reflected"] = True


def test_put_then_about_and_recent():
    t = FakeTable(); s = MemoryStore(t)
    s.put("me", subject="LeBron", kind="observation", text="he's all hustle", importance=7, created_at="2026-06-25T10:00:00Z")
    s.put("me", subject="Dwight", kind="observation", text="beets again", importance=4, created_at="2026-06-25T11:00:00Z")
    assert len(t.items) == 2
    about = s.about("me", "LeBron")
    assert len(about) == 1 and about[0]["text"] == "he's all hustle"
    recent = s.recent("me", limit=1)
    assert recent[0]["subject"] == "Dwight"  # most recent first


def test_retrieval_score_recency_and_importance():
    fresh = {"createdAt": "2026-06-25T12:00:00Z", "importance": 5}
    old = {"createdAt": "2026-06-24T12:00:00Z", "importance": 5}
    now = "2026-06-25T12:00:00Z"
    assert retrieval_score(fresh, now) > retrieval_score(old, now)
    hi = {"createdAt": now, "importance": 10}; lo = {"createdAt": now, "importance": 1}
    assert retrieval_score(hi, now) > retrieval_score(lo, now)


def test_mark_reflected():
    t = FakeTable(); s = MemoryStore(t)
    s.put("me", "self", "observation", "x", 5, "2026-06-25T10:00:00Z")
    s.mark_reflected("me", ["2026-06-25T10:00:00Z"])
    assert t.items[0]["reflected"] is True


# --- memory_store_from_env cache tests ---

def test_memory_store_from_env_empty_table_name_returns_none(monkeypatch):
    memory_store_mod._store_cache.clear()
    monkeypatch.setattr("core.config.settings.RESIDENT_MEMORIES_TABLE", "")
    from worldgm.residents.memory_store import memory_store_from_env
    assert memory_store_from_env() is None


def test_memory_store_from_env_caches_instance(monkeypatch):
    memory_store_mod._store_cache.clear()
    monkeypatch.setattr("core.config.settings.RESIDENT_MEMORIES_TABLE", "goosetown-dev-memories")
    fake_table = MagicMock()
    fake_resource = MagicMock()
    fake_resource.return_value.Table.return_value = fake_table

    from worldgm.residents.memory_store import memory_store_from_env
    with patch("boto3.resource", fake_resource) as mock_resource:
        first = memory_store_from_env()
        second = memory_store_from_env()

    assert first is second, "Same object must be returned on repeated calls"
    mock_resource.assert_called_once_with("dynamodb")


def test_memory_store_from_env_different_names_build_separate_stores(monkeypatch):
    memory_store_mod._store_cache.clear()
    fake_resource = MagicMock()
    fake_resource.return_value.Table.side_effect = lambda name: MagicMock(name=name)

    from worldgm.residents.memory_store import memory_store_from_env
    with patch("boto3.resource", fake_resource):
        monkeypatch.setattr("core.config.settings.RESIDENT_MEMORIES_TABLE", "table-a")
        store_a = memory_store_from_env()
        monkeypatch.setattr("core.config.settings.RESIDENT_MEMORIES_TABLE", "table-b")
        store_b = memory_store_from_env()

    assert store_a is not store_b
    assert fake_resource.call_count == 2
