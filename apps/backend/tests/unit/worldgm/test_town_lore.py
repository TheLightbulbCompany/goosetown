"""Unit tests for worldgm.town_lore — GM-side town-lore synthesis + warm boot.

All tests inject fakes (a fake state with a fake model, a fake recent-events
reader, and a fake emit) so no DynamoDB / Bedrock is touched.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from worldgm import town_lore
from worldgm.town_lore import (
    _extract_lore_lines,
    synthesize_town_lore,
    warm_boot_lore,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeModel:
    def __init__(self, reply="", raises=False):
        self.reply = reply
        self.raises = raises
        self.calls = []

    def sample_text(self, prompt, **kw):
        self.calls.append(prompt)
        if self.raises:
            raise RuntimeError("nova is down")
        return self.reply


def _state(model, town_lore_list=None):
    return SimpleNamespace(model_lite=model, town_lore=town_lore_list if town_lore_list is not None else [])


def _events(n=3):
    return [{"tag": "arc.beat", "summary": f"event {i}"} for i in range(n)]


def _emit_recorder():
    recorded = []

    def emit(line, now_ms):
        recorded.append((line, now_ms))

    return emit, recorded


# ---------------------------------------------------------------------------
# _extract_lore_lines — tolerant parse
# ---------------------------------------------------------------------------


def test_extract_json_list():
    out = _extract_lore_lines('["the beet-wine week", "the great library debate"]')
    assert out == ["the beet-wine week", "the great library debate"]


def test_extract_json_object_with_lore_key():
    out = _extract_lore_lines('{"lore": ["one", "two"]}')
    assert out == ["one", "two"]


def test_extract_json_embedded_in_prose():
    out = _extract_lore_lines('Sure: ["line a", "line b"] hope that helps')
    assert out == ["line a", "line b"]


def test_extract_newline_fallback_strips_markers():
    out = _extract_lore_lines("- first lore\n2. second lore\n* third lore")
    assert out == ["first lore", "second lore", "third lore"]


def test_extract_caps_at_three_lines():
    out = _extract_lore_lines('["a", "b", "c", "d", "e"]')
    assert out == ["a", "b", "c"]


def test_extract_empty_list_returns_empty():
    assert _extract_lore_lines("[]") == []


def test_extract_blank_returns_empty():
    assert _extract_lore_lines("   ") == []
    assert _extract_lore_lines("") == []


def test_extract_strips_wrapping_quotes_in_fallback():
    assert _extract_lore_lines('"just one durable thing"') == ["just one durable thing"]


# ---------------------------------------------------------------------------
# synthesize_town_lore — happy path
# ---------------------------------------------------------------------------


def test_synthesize_appends_lines_and_calls_emit():
    model = FakeModel('["the beet-wine week", "the library debate"]')
    state = _state(model)
    emit, recorded = _emit_recorder()

    out = asyncio.run(synthesize_town_lore(state, 1234, read_recent_events=lambda *a, **k: _events(), emit=emit))

    assert out == ["the beet-wine week", "the library debate"]
    assert state.town_lore == ["the beet-wine week", "the library debate"]
    # emit called once per new line, with the now_ms threaded through.
    assert recorded == [("the beet-wine week", 1234), ("the library debate", 1234)]
    # the model saw the recent events.
    assert model.calls and "event 0" in model.calls[0]


def test_synthesize_appends_to_existing_digest():
    model = FakeModel('["new lore"]')
    state = _state(model, town_lore_list=["old lore"])
    emit, _ = _emit_recorder()

    asyncio.run(synthesize_town_lore(state, 1, read_recent_events=lambda *a, **k: _events(), emit=emit))

    assert state.town_lore == ["old lore", "new lore"]


def test_synthesize_caps_digest_at_12_fifo():
    # 12 existing + 3 new → keep the newest 12 (drop the 3 oldest).
    existing = [f"old-{i}" for i in range(12)]
    model = FakeModel('["n1", "n2", "n3"]')
    state = _state(model, town_lore_list=list(existing))
    emit, _ = _emit_recorder()

    asyncio.run(synthesize_town_lore(state, 1, read_recent_events=lambda *a, **k: _events(), emit=emit))

    assert len(state.town_lore) == 12
    # oldest three dropped, three new appended at the tail.
    assert state.town_lore == existing[3:] + ["n1", "n2", "n3"]


def test_synthesize_no_events_is_noop():
    model = FakeModel('["should not happen"]')
    state = _state(model, town_lore_list=["keep"])
    emit, recorded = _emit_recorder()

    out = asyncio.run(synthesize_town_lore(state, 1, read_recent_events=lambda *a, **k: [], emit=emit))

    assert out == []
    assert state.town_lore == ["keep"]  # unchanged
    assert recorded == []
    assert model.calls == []  # model never called when there's nothing to chew on


# ---------------------------------------------------------------------------
# synthesize_town_lore — failure modes (best-effort, never raise)
# ---------------------------------------------------------------------------


def test_synthesize_model_failure_returns_empty_and_leaves_state():
    model = FakeModel(raises=True)
    state = _state(model, town_lore_list=["keep this"])
    emit, recorded = _emit_recorder()

    out = asyncio.run(synthesize_town_lore(state, 1, read_recent_events=lambda *a, **k: _events(), emit=emit))

    assert out == []
    assert state.town_lore == ["keep this"]  # untouched
    assert recorded == []


def test_synthesize_prose_reply_falls_back_to_one_line():
    """A no-JSON prose reply falls back to newline parsing — a single sentence
    becomes one lore line (graceful degradation, not a crash)."""
    model = FakeModel("The town remembers the great beet harvest.")
    state = _state(model, town_lore_list=["keep"])
    emit, recorded = _emit_recorder()

    out = asyncio.run(synthesize_town_lore(state, 7, read_recent_events=lambda *a, **k: _events(), emit=emit))

    assert out == ["The town remembers the great beet harvest."]
    assert state.town_lore == ["keep", "The town remembers the great beet harvest."]
    assert recorded == [("The town remembers the great beet harvest.", 7)]


def test_synthesize_empty_list_reply_is_noop():
    model = FakeModel("[]")
    state = _state(model, town_lore_list=["keep"])
    emit, recorded = _emit_recorder()

    out = asyncio.run(synthesize_town_lore(state, 1, read_recent_events=lambda *a, **k: _events(), emit=emit))

    assert out == []
    assert state.town_lore == ["keep"]
    assert recorded == []


def test_synthesize_read_failure_returns_empty():
    model = FakeModel('["x"]')
    state = _state(model, town_lore_list=["keep"])

    def boom(*a, **k):
        raise RuntimeError("DynamoDB down")

    out = asyncio.run(synthesize_town_lore(state, 1, read_recent_events=boom, emit=lambda *a: None))

    assert out == []
    assert state.town_lore == ["keep"]


def test_synthesize_emit_failure_still_appends_to_digest():
    """A failing emit must not lose the in-memory digest update or the others."""
    model = FakeModel('["a", "b"]')
    state = _state(model)

    def flaky_emit(line, now_ms):
        if line == "a":
            raise RuntimeError("transient")

    out = asyncio.run(synthesize_town_lore(state, 1, read_recent_events=lambda *a, **k: _events(), emit=flaky_emit))

    assert out == ["a", "b"]
    assert state.town_lore == ["a", "b"]  # digest updated despite emit failure


def test_synthesize_no_model_returns_empty():
    state = SimpleNamespace(model_lite=None, town_lore=[])
    out = asyncio.run(
        synthesize_town_lore(state, 1, read_recent_events=lambda *a, **k: _events(), emit=lambda *a: None)
    )
    assert out == []


def test_synthesize_cancelled_propagates():
    """CancelledError must propagate, not be swallowed by the best-effort guard."""

    class CancellingModel:
        def sample_text(self, prompt, **kw):
            raise asyncio.CancelledError()

    state = _state(CancellingModel())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(synthesize_town_lore(state, 1, read_recent_events=lambda *a, **k: _events(), emit=lambda *a: None))


# ---------------------------------------------------------------------------
# warm_boot_lore
# ---------------------------------------------------------------------------


def test_warm_boot_populates_digest_oldest_first():
    state = SimpleNamespace(town_lore=["stale"])
    # rows arrive newest-first from the GSI.
    rows = [
        {"tag": "town.lore", "summary": "newest"},
        {"tag": "town.lore", "summary": "middle"},
        {"tag": "town.lore", "summary": "oldest"},
    ]

    asyncio.run(warm_boot_lore(state, read_lore_events=lambda *a, **k: rows))

    # loaded oldest-first so the tail is the freshest line.
    assert state.town_lore == ["oldest", "middle", "newest"]


def test_warm_boot_caps_at_12():
    rows = [{"tag": "town.lore", "summary": f"lore-{i}"} for i in range(20)]
    state = SimpleNamespace(town_lore=[])

    asyncio.run(warm_boot_lore(state, read_lore_events=lambda *a, **k: rows))

    assert len(state.town_lore) == 12


def test_warm_boot_skips_blank_summaries():
    rows = [
        {"tag": "town.lore", "summary": "real"},
        {"tag": "town.lore", "summary": "  "},
        {"tag": "town.lore", "summary": None},
    ]
    state = SimpleNamespace(town_lore=[])

    asyncio.run(warm_boot_lore(state, read_lore_events=lambda *a, **k: rows))

    assert state.town_lore == ["real"]


def test_warm_boot_empty_rows_yields_empty_digest():
    state = SimpleNamespace(town_lore=["leftover"])
    asyncio.run(warm_boot_lore(state, read_lore_events=lambda *a, **k: []))
    assert state.town_lore == []


def test_warm_boot_read_failure_is_best_effort():
    state = SimpleNamespace(town_lore=["leftover"])

    def boom(*a, **k):
        raise RuntimeError("DynamoDB down")

    # Must not raise.
    asyncio.run(warm_boot_lore(state, read_lore_events=boom))
    # digest left as-is (the read never produced a replacement).
    assert state.town_lore == ["leftover"]


# ---------------------------------------------------------------------------
# Default reader filtering (no DynamoDB — patch the table accessor)
# ---------------------------------------------------------------------------


def test_default_read_recent_events_filters_personal_and_lore(monkeypatch):
    captured = {}

    class FakeTable:
        def query(self, **kw):
            captured.update(kw)
            return {
                "Items": [
                    {"tag": "arc.beat", "summary": "a beat"},
                    {"tag": "personal.x.observed", "summary": "private"},
                    {"tag": "town.lore", "summary": "old lore"},
                    {"tag": "weather.rain", "summary": "it rained"},
                ]
            }

    monkeypatch.setattr(town_lore.dynamo, "world_events_table", lambda: FakeTable())
    out = town_lore._default_read_recent_events()
    tags = [i["tag"] for i in out]
    assert "arc.beat" in tags
    assert "weather.rain" in tags
    assert "personal.x.observed" not in tags
    assert "town.lore" not in tags
    assert captured["IndexName"] == "byTimestamp"
    assert captured["ScanIndexForward"] is False


def test_default_read_lore_events_filters_to_lore(monkeypatch):
    class FakeTable:
        def query(self, **kw):
            return {
                "Items": [
                    {"tag": "arc.beat", "summary": "a beat"},
                    {"tag": "town.lore", "summary": "lore one"},
                    {"tag": "town.lore", "summary": "lore two"},
                ]
            }

    monkeypatch.setattr(town_lore.dynamo, "world_events_table", lambda: FakeTable())
    out = town_lore._default_read_lore_events()
    assert [i["summary"] for i in out] == ["lore one", "lore two"]
