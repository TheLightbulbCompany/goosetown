"""social_goal.py — memory-grounded resident goal generation (Nova-Lite, best-effort).

Public API
----------
async generate_social_goal(model, store, my_id, persona, relationship_summary, now_iso) -> str

Returns a single short-term social aim (≤140 chars, plain text), e.g.
  "patch things up with Dwight"
  "hold court at the café"
  "find someone worth debating"

Returns "" on any error or empty output (never propagates).
"""
from __future__ import annotations

import asyncio
import logging

from worldgm.residents import reflection as _reflection
from worldgm.residents.memory_store import MemoryStore
from worldgm.residents.personas import Persona

_log = logging.getLogger(__name__)

_MAX_TOKENS = 60
_TEMPERATURE = 0.8
_MAX_GOAL_LEN = 140


async def generate_social_goal(
    model,
    store: MemoryStore,
    my_id: str,
    persona: Persona,
    relationship_summary: str,
    now_iso: str,
) -> str:
    """Generate one short concrete social aim grounded in memory + persona.

    Calls model.sample_text via asyncio.to_thread. Returns "" on any failure.
    """
    self_reflections = _reflection.recall_self(store, my_id, now_iso)

    prompt_parts = [
        f"You are {persona.display_name}, {persona.role}.",
        f"Personality: {persona.personality}",
    ]
    if self_reflections:
        prompt_parts.append(f"What you know about yourself: {self_reflections}")
    if relationship_summary:
        prompt_parts.append(f"Your current relationships: {relationship_summary}")
    prompt_parts += [
        "",
        "Based on who you are and where things stand with the people around you, "
        "state ONE short concrete social aim you want to pursue right now.",
        "Reply with plain text only — a single sentence, no quotes, no bullet points, ≤140 characters.",
        "Examples: patch things up with Dwight | hold court at the café | find someone worth debating",
    ]
    prompt = "\n".join(prompt_parts)

    try:
        raw: str = await asyncio.to_thread(
            model.sample_text, prompt, max_tokens=_MAX_TOKENS, temperature=_TEMPERATURE
        )
    except Exception:
        _log.debug("generate_social_goal: model call failed", exc_info=True)
        return ""

    # Clean: take first non-empty line, strip surrounding whitespace/quotes, cap length.
    first_line = ""
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped:
            first_line = stripped
            break

    if not first_line:
        return ""

    # Strip wrapping quotes (single or double).
    if len(first_line) >= 2 and first_line[0] in ('"', "'") and first_line[-1] == first_line[0]:
        first_line = first_line[1:-1].strip()

    if not first_line:
        return ""

    return first_line[:_MAX_GOAL_LEN]
