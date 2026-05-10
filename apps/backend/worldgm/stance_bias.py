"""Stance-pair bias for arc template selection.

Hostile-stance pairs at the same location bias toward 'tension' templates;
affectionate pairs bias toward 'friendship' templates.

The hostile/affectionate classification uses simple keyword matching for v1.
LLM-based classification is a future improvement.
"""

from __future__ import annotations

from typing import Any

_HOSTILE_KEYWORDS = (
    "annoying",
    "hate",
    "wary",
    "distrust",
    "cold",
    "harsh",
    "argument",
    "fight",
    "betrayal",
    "rude",
    "dismissive",
    "manipulative",
)

_AFFECTIONATE_KEYWORDS = (
    "warm",
    "kind",
    "love",
    "fond",
    "trust",
    "close",
    "friend",
    "support",
    "kindred",
    "tender",
    "affectionate",
)


def _classify(stance: str | None) -> str:
    if not stance:
        return "neutral"
    lower = stance.lower()
    if any(k in lower for k in _HOSTILE_KEYWORDS):
        return "hostile"
    if any(k in lower for k in _AFFECTIONATE_KEYWORDS):
        return "affectionate"
    return "neutral"


def is_hostile_pair(stance_a_to_b: str | None, stance_b_to_a: str | None) -> bool:
    return _classify(stance_a_to_b) == "hostile" or _classify(stance_b_to_a) == "hostile"


def is_affectionate_pair(stance_a_to_b: str | None, stance_b_to_a: str | None) -> bool:
    return _classify(stance_a_to_b) == "affectionate" and _classify(stance_b_to_a) == "affectionate"


def bias_templates_by_stances(
    templates: list[dict[str, Any]],
    active_pairs: list[tuple[str, str, str | None, str | None]],
) -> list[tuple[float, dict[str, Any]]]:
    """Return [(score, template), ...] biased by stance pairs.

    Hostile pairs +1.0 to 'tension' templates.
    Affectionate pairs +1.0 to 'friendship' templates.

    ``active_pairs`` is a list of (agent_a_id, agent_b_id, stance_a_to_b, stance_b_to_a)
    tuples — typically pairs of agents at the same location.
    """
    scored: list[tuple[float, dict]] = []
    for tpl in templates:
        score = 1.0  # base
        tags = tpl.get("tags", [])
        for _, _, sab, sba in active_pairs:
            if is_hostile_pair(sab, sba) and "tension" in tags:
                score += 1.0
            if is_affectionate_pair(sab, sba) and "friendship" in tags:
                score += 1.0
        scored.append((score, tpl))
    return scored
