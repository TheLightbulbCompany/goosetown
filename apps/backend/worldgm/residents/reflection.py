"""Reflection module — turns conversations into durable memory (Nova-Lite, best-effort).

Public API
----------
recall_about(store, my_id, other_name, now_iso, k=3) -> str
recall_self(store, my_id, now_iso, k=2) -> str
async reflect_on_conversation(model, store, town_service, my_id, other_id,
                               other_name, transcript, now_iso) -> bool
async synthesize_reflection(model, store, my_id, now_iso, min_new=6) -> int

Helper
------
_extract_json(raw: str) -> dict | None
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging

from worldgm.residents.memory_store import MemoryStore, retrieval_score

_log = logging.getLogger(__name__)

_CONV_MAX_TOKENS = 256
_SYNTH_MAX_TOKENS = 512
_TEMPERATURE = 0.3


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_json(raw: str) -> dict | None:
    """Tolerant JSON extraction: finds first '{' … last '}' and parses that slice."""
    try:
        start = raw.index("{")
        end = raw.rindex("}") + 1
        return json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError):
        return None


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# recall_about — synchronous, no model call
# ---------------------------------------------------------------------------


def recall_about(store: MemoryStore, my_id: str, other_name: str, now_iso: str, k: int = 3) -> str:
    """Return a rendered string of the top-k memories about other_name, or "" if none."""
    mems = store.about(my_id, other_name)
    if not mems:
        return ""
    ranked = sorted(mems, key=lambda m: retrieval_score(m, now_iso), reverse=True)[:k]
    lines = [f"- [{m['kind']}] {m['text']}" for m in ranked]
    return "\n".join(lines)


def recall_self(store: MemoryStore, my_id: str, now_iso: str, k: int = 2) -> str:
    """Return a rendered string of the top-k self-reflections (insights), or "" if none.

    Fetches memories stored with subject="self" (written by synthesize_reflection),
    ranks by retrieval_score (most salient first), and returns the top-k insight
    texts as a short string. Mirrors recall_about's structure.
    """
    mems = store.about(my_id, "self")
    if not mems:
        return ""
    ranked = sorted(mems, key=lambda m: retrieval_score(m, now_iso), reverse=True)[:k]
    lines = [m["text"] for m in ranked]
    return "; ".join(lines)


# ---------------------------------------------------------------------------
# reflect_on_conversation — one Nova-Lite call
# ---------------------------------------------------------------------------


async def reflect_on_conversation(
    model,
    store: MemoryStore,
    town_service,
    my_id: str,
    other_id: str,
    other_name: str,
    transcript: list[dict],
    now_iso: str,
) -> bool:
    """Write one observation memory + relationship update. Returns True iff written."""
    transcript_text = "\n".join(f"{turn['speaker']}: {turn['text']}" for turn in transcript)
    prompt = (
        f"You just had a conversation with {other_name}. Reflect on it.\n\n"
        f"Transcript:\n{transcript_text}\n\n"
        "Reply with ONLY valid JSON matching this schema exactly:\n"
        '{"summary": "<one sentence>", "importance": <1-10>, '
        '"affinity_delta": <-10 to 10>, "note": "<short stance label>"}'
    )
    try:
        raw = await asyncio.to_thread(model.sample_text, prompt, max_tokens=_CONV_MAX_TOKENS, temperature=_TEMPERATURE)
        parsed = _extract_json(raw)
        if not parsed:
            return False
        summary = str(parsed.get("summary", "")).strip()
        if not summary:
            return False
        importance = _clamp(int(parsed.get("importance", 5)), 1, 10)
        affinity_delta = _clamp(int(parsed.get("affinity_delta", 0)), -10, 10)
        note = str(parsed.get("note", "")).strip()
    except Exception:
        _log.exception("reflect_on_conversation: model/parse error")
        return False

    try:
        store.put(my_id, other_name, "observation", summary, importance, now_iso)
        town_service.update_relationship(my_id, other_id, affinity_delta=affinity_delta)
        town_service.upsert_relationship_stance(my_id, other_id, stance_a_to_b=note)
    except Exception:
        _log.exception("reflect_on_conversation: write error")
        return False

    return True


# ---------------------------------------------------------------------------
# synthesize_reflection — one Nova-Lite call over unreflected observations
# ---------------------------------------------------------------------------


async def synthesize_reflection(
    model,
    store: MemoryStore,
    my_id: str,
    now_iso: str,
    min_new: int = 6,
) -> int:
    """Distil unreflected observations into higher-order insights. Returns count stored."""
    all_mems = store.recent(my_id)
    unreflected = [m for m in all_mems if m.get("kind") == "observation" and not m.get("reflected")]
    if len(unreflected) < min_new:
        return 0

    snippets = "\n".join(f"- [{m.get('subject', '?')}] {m['text']}" for m in unreflected)
    prompt = (
        "Here are recent observations you have not yet reflected on:\n"
        f"{snippets}\n\n"
        "Synthesise them into higher-level insights about yourself or the people around you.\n"
        "Reply with ONLY valid JSON matching this schema exactly:\n"
        '{"insights": [{"insight": "<text>", "importance": <1-10>}, ...]}'
    )
    try:
        raw = await asyncio.to_thread(model.sample_text, prompt, max_tokens=_SYNTH_MAX_TOKENS, temperature=_TEMPERATURE)
        parsed = _extract_json(raw)
        if not parsed:
            return 0
        insights = parsed.get("insights", [])
        if not isinstance(insights, list):
            return 0
    except Exception:
        _log.exception("synthesize_reflection: model/parse error")
        return 0

    source_ids = [m["memoryId"] for m in unreflected]
    source_created_ats = [m["createdAt"] for m in unreflected]

    # Parse base time once; add microsecond offsets per insight so each SK is distinct.
    _use_z = now_iso.endswith("Z")
    _base_dt = datetime.datetime.fromisoformat(now_iso.replace("Z", "+00:00"))

    count = 0
    for i, item in enumerate(insights):
        try:
            text = str(item.get("insight", "")).strip()
            if not text:
                continue
            importance = max(6, _clamp(int(item.get("importance", 6)), 1, 10))
            insight_dt = _base_dt + datetime.timedelta(microseconds=i)
            if _use_z:
                insight_iso = insight_dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
            else:
                insight_iso = insight_dt.isoformat()
            store.put(my_id, "self", "reflection", text, importance, insight_iso, source_ids=source_ids)
            count += 1
        except Exception:
            _log.exception("synthesize_reflection: failed to store insight")

    if count > 0:
        try:
            store.mark_reflected(my_id, source_created_ats)
        except Exception:
            _log.exception("synthesize_reflection: failed to mark sources reflected")

    return count
