import math, uuid
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
