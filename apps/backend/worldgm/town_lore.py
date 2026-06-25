"""Town lore synthesis (GM-side, Nova-Lite, best-effort).

Phase 3 sub-feature B (see docs/superpowers/specs/2026-06-25-town-culture-phase3-design.md).
Periodically distils recent notable ``world-events`` (arc beats/resolutions +
other high-signal events) into 1-3 short durable "town lore" lines everyone in
town can reference, so residents share cultural touchstones instead of running
19 parallel monologues.

Reuses, rather than reinventing:
  - the existing ``world-events`` table for both reading recent events and
    persisting lore (each lore line is a ``tag="town.lore"`` event via
    ``emit_world_event``), so a GM restart re-warms the digest by reading recent
    ``tag="town.lore"`` rows — no new DynamoDB table.
  - the arc/reflection ``asyncio.to_thread`` + tolerant-parse + best-effort shape.

Public API
----------
async synthesize_town_lore(state, now_ms, *, read_recent_events=None, emit=None) -> list[str]
async warm_boot_lore(state, *, read_lore_events=None) -> None

Both are best-effort: any failure logs and returns ([]/None) — never raises
(CancelledError propagates).

Dependency injection: the table-read and event-emit functions default to the
real ones but are injectable so tests need no DynamoDB.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from core import dynamo

from .event_publisher import emit_world_event

logger = logging.getLogger(__name__)

_ENV = os.environ.get("ENVIRONMENT", "dev")

# How many recent events the synthesis call reasons over, and how many lore
# lines the rolling digest keeps (FIFO — newest 12 survive).
_RECENT_EVENTS_LIMIT = 25
_LORE_CAP = 12

_MAX_TOKENS = 256
_TEMPERATURE = 0.5

# Lore lines are short, durable touchstones — not a paragraph.
_MAX_LORE_LINES = 3
_MAX_LORE_LEN = 200


# ---------------------------------------------------------------------------
# Default dependency implementations (real DynamoDB). Injected for tests.
# ---------------------------------------------------------------------------


def _default_read_recent_events(limit: int = _RECENT_EVENTS_LIMIT) -> list[dict]:
    """Read the most recent ``world-events`` rows via the byTimestamp GSI.

    Mirrors the read in ``routers/town.py`` (the public feed): query the
    ``byTimestamp`` GSI keyed on ``envBucket=_ENV``, newest first, capped small.
    Personal (``personal.*``) and prior lore (``town.lore``) rows are filtered
    out so synthesis reasons over fresh public happenings, not its own output.
    """
    table = dynamo.world_events_table()
    resp = table.query(
        IndexName="byTimestamp",
        KeyConditionExpression="envBucket = :env",
        ExpressionAttributeValues={":env": _ENV},
        ScanIndexForward=False,  # newest first
        Limit=limit * 2,  # over-fetch since we filter
    )
    items = resp.get("Items", [])
    notable = [i for i in items if not str(i.get("tag", "")).startswith("personal.") and i.get("tag") != "town.lore"]
    return notable[:limit]


def _default_read_lore_events(limit: int = _LORE_CAP) -> list[dict]:
    """Read the most recent ``tag="town.lore"`` rows via the byTimestamp GSI.

    Used by ``warm_boot_lore`` to re-warm the in-memory digest after a GM
    restart. Over-fetches and filters to ``town.lore`` (a tag-keyed GSI would be
    cheaper, but the digest is tiny and this runs once at boot).
    """
    table = dynamo.world_events_table()
    resp = table.query(
        IndexName="byTimestamp",
        KeyConditionExpression="envBucket = :env",
        ExpressionAttributeValues={":env": _ENV},
        ScanIndexForward=False,  # newest first
        Limit=limit * 4,  # over-fetch since most recent rows are not lore
    )
    items = resp.get("Items", [])
    return [i for i in items if i.get("tag") == "town.lore"][:limit]


def _default_emit(line: str, now_ms: int) -> None:
    """Persist one lore line as a ``tag="town.lore"`` world event."""
    emit_world_event(
        tag="town.lore",
        detail={"synthesizedAtMs": now_ms},
        summary=line,
    )


# ---------------------------------------------------------------------------
# Tolerant lore extraction
# ---------------------------------------------------------------------------


def _extract_lore_lines(raw: str) -> list[str]:
    """Pull 1-3 short lore lines from a model reply, tolerant of format.

    Accepts (in order of preference):
      - a JSON list:  ``["line one", "line two"]``
      - a JSON object with a ``lore`` list:  ``{"lore": ["line one", ...]}``
      - newline-separated plain lines (the fallback) — leading bullet/number
        markers stripped.

    Returns at most ``_MAX_LORE_LINES`` non-empty, length-capped lines.
    """
    if not raw or not raw.strip():
        return []

    candidates: list | None = None  # None = "no structured lore found yet"

    # 1) Try a JSON list or {"lore": [...]} object anywhere in the reply. A valid
    # but EMPTY list/object is authoritative ("no lore this cycle") — it must NOT
    # fall through to newline parsing, which would treat the literal "[]" as text.
    parsed = _try_json(raw)
    if isinstance(parsed, list):
        candidates = parsed
    elif isinstance(parsed, dict):
        lore = parsed.get("lore")
        if isinstance(lore, list):
            candidates = lore

    # 2) Fallback: newline-separated plain text — only when no JSON was found.
    if candidates is None:
        candidates = raw.splitlines()

    out: list[str] = []
    for item in candidates:
        line = _clean_lore_line(item)
        if line:
            out.append(line)
        if len(out) >= _MAX_LORE_LINES:
            break
    return out


def _try_json(raw: str):
    """Best-effort JSON parse of the first ``[`` / ``{`` span. Returns the parsed
    value, or ``None`` if no valid JSON list/object is found."""
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        start = raw.find(open_ch)
        end = raw.rfind(close_ch)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except (ValueError, TypeError):
                continue
    return None


def _clean_lore_line(item) -> str:
    """Normalise one candidate to a bare lore line, or "" if empty."""
    s = str(item).strip()
    # Strip a leading bullet/number marker the model sometimes adds.
    for marker in ("- ", "* ", "• "):
        if s.startswith(marker):
            s = s[len(marker) :].strip()
            break
    # Strip a leading "1. " / "2) " style numbering.
    if len(s) >= 3 and s[0].isdigit() and s[1] in ".)" and s[2] == " ":
        s = s[3:].strip()
    # Strip wrapping quotes.
    if len(s) >= 2 and s[0] in "\"'“”" and s[-1] in "\"'“”":
        s = s[1:-1].strip()
    return s[:_MAX_LORE_LEN]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def synthesize_town_lore(
    state,
    now_ms: int,
    *,
    read_recent_events=None,
    emit=None,
) -> list[str]:
    """Distil recent notable events into 1-3 durable town-lore lines.

    One Nova-Lite call over the recent ``world-events``; appends the new lines to
    ``state.town_lore`` (capped at the most-recent ``_LORE_CAP``, FIFO) and
    persists each as a ``tag="town.lore"`` event so a GM restart can re-warm the
    digest. Best-effort: any failure logs and returns ``[]`` (state untouched).

    ``read_recent_events`` / ``emit`` default to the real DynamoDB-backed
    implementations; tests inject fakes.
    """
    read_recent_events = read_recent_events or _default_read_recent_events
    emit = emit or _default_emit
    model = getattr(state, "model_lite", None)
    if model is None:
        return []

    try:
        events = await asyncio.to_thread(read_recent_events)
        if not events:
            return []

        prompt = _build_prompt(events, list(getattr(state, "town_lore", []) or []))
        raw = await asyncio.to_thread(model.sample_text, prompt, max_tokens=_MAX_TOKENS, temperature=_TEMPERATURE)
        new_lines = _extract_lore_lines(raw)
        if not new_lines:
            return []

        # Append + FIFO cap on the in-memory digest.
        digest = getattr(state, "town_lore", None)
        if digest is None:
            digest = []
            state.town_lore = digest
        digest.extend(new_lines)
        if len(digest) > _LORE_CAP:
            del digest[: len(digest) - _LORE_CAP]

        # Persist each new line. A single emit failure must not lose the others.
        for line in new_lines:
            try:
                await asyncio.to_thread(emit, line, now_ms)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("synthesize_town_lore: failed to persist lore line")

        logger.info("Synthesized %d town-lore line(s)", len(new_lines))
        return new_lines
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("synthesize_town_lore: best-effort failure; skipping this cycle")
        return []


async def warm_boot_lore(state, *, read_lore_events=None) -> None:
    """Re-warm ``state.town_lore`` from recent persisted ``tag="town.lore"`` rows.

    Reads the most-recent (up to ``_LORE_CAP``) lore rows and loads their
    summaries oldest-first into ``state.town_lore`` (so the freshest line ends up
    last, matching the synthesis append order). Best-effort: any failure logs and
    leaves the digest empty.
    """
    read_lore_events = read_lore_events or _default_read_lore_events
    try:
        rows = await asyncio.to_thread(read_lore_events)
        # Rows arrive newest-first; load oldest-first so the digest's tail is the
        # most recent line (parity with synthesize_town_lore's append order).
        lines: list[str] = []
        for row in reversed(rows or []):
            summary = str(row.get("summary") or "").strip()
            if summary:
                lines.append(summary)
        if len(lines) > _LORE_CAP:
            lines = lines[-_LORE_CAP:]
        state.town_lore = lines
        logger.info("Warm-booted %d town-lore line(s)", len(lines))
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("warm_boot_lore: best-effort failure; starting with empty digest")


def _build_prompt(events: list[dict], existing_lore: list[str]) -> str:
    """Render the synthesis prompt over recent events + the existing digest."""
    event_lines = []
    for e in events:
        summary = str(e.get("summary") or "").strip()
        if summary:
            tag = str(e.get("tag") or "").strip()
            event_lines.append(f"- ({tag}) {summary}" if tag else f"- {summary}")
    events_block = "\n".join(event_lines) if event_lines else "- (nothing notable yet)"

    parts = [
        "You are the chronicler of GooseTown, a small town of AI residents.",
        "Here are recent notable happenings:",
        events_block,
    ]
    if existing_lore:
        parts += [
            "",
            "Lore the town already remembers (don't repeat these):",
            *[f"- {line}" for line in existing_lore],
        ]
    parts += [
        "",
        "Distil the happenings above into 1-3 short, durable pieces of town lore — "
        "the kind of shared memory residents would reference later "
        '(e.g. "the week Dwight and LeBron launched beet-wine"). Each line is one '
        "sentence, concrete, and references real residents/events above — invent nothing.",
        "If nothing rises to lasting lore, reply with an empty JSON list: []",
        'Reply with ONLY a JSON list of strings, e.g. ["line one", "line two"].',
    ]
    return "\n".join(parts)
