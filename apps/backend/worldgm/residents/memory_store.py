"""Subject-indexed resident memory store (DynamoDB). No embeddings — retrieval is
recency·importance over a subject GSI. All methods best-effort at the call site."""

from __future__ import annotations

import datetime as _dt
import uuid


def _parse_iso(s: str) -> _dt.datetime:
    return _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def retrieval_score(mem: dict, now_iso: str, half_life_hours: float = 24.0) -> float:
    """Normalized recency (exponential decay, given half-life) + importance/10."""
    try:
        age_h = (_parse_iso(now_iso) - _parse_iso(str(mem["createdAt"]))).total_seconds() / 3600.0
    except Exception:
        age_h = 0.0
    recency = 0.5 ** (max(age_h, 0.0) / half_life_hours)
    importance = float(mem.get("importance", 1)) / 10.0
    return recency + importance


class MemoryStore:
    def __init__(self, table):
        self._t = table

    def put(self, agent_id, subject, kind, text, importance, created_at, source_ids=()):
        item = {
            "agentId": agent_id,
            "createdAt": created_at,
            "memoryId": str(uuid.uuid4()),
            "subject": subject,
            "kind": kind,
            "text": text,
            "importance": int(importance),
            "reflected": False,
            "sourceIds": list(source_ids),
        }
        self._t.put_item(Item=item)
        return item

    def recent(self, agent_id, limit=100):
        resp = self._t.query(
            KeyConditionExpression="agentId = :a",
            ExpressionAttributeValues={":a": agent_id},
            ScanIndexForward=False,
            Limit=limit,
        )
        return resp.get("Items", [])

    def about(self, agent_id, subject, limit=20):
        resp = self._t.query(
            IndexName="agentId-subject-index",
            KeyConditionExpression="agentId = :a AND subject = :s",
            ExpressionAttributeValues={":a": agent_id, ":s": subject},
            Limit=limit,
        )
        return resp.get("Items", [])

    def mark_reflected(self, agent_id, created_ats):
        for ca in created_ats:
            self._t.update_item(
                Key={"agentId": agent_id, "createdAt": ca},
                UpdateExpression="SET reflected = :t",
                ExpressionAttributeValues={":t": True},
            )


_store_cache: dict[str, "MemoryStore"] = {}


def memory_store_from_env():
    from core.config import settings

    name = settings.RESIDENT_MEMORIES_TABLE
    if not name:
        return None
    cached = _store_cache.get(name)
    if cached is not None:
        return cached
    import boto3

    store = MemoryStore(boto3.resource("dynamodb").Table(name))
    _store_cache[name] = store
    return store
