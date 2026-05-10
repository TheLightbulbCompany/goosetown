"""Goal-relevance check — does an event advance any witness's currentGoal?

One LLM call per event with all witnesses' goals batched. Returns mapping
agent_id → relevance_reason for matched witnesses.

Cost: ~1 Nova Micro call per event with active goals. At Nova Micro pricing
($0.035/Mtok input) this is negligible — the prompt is ~500 tokens, so
each check is ~$0.00002.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


# Import lazily inside goal_match to avoid a circular-import risk at module level
# (event_publisher imports dynamo; goal_relevance is imported from arc_orchestrator
# which imports event_publisher — keeping it local keeps the import graph clean).
def _lazy_emit(name: str) -> None:
    try:
        from .event_publisher import _emit_metric  # noqa: PLC0415

        _emit_metric(name)
    except Exception:
        logger.debug("goal_relevance: metric emit skipped (%s)", name)


def _build_prompt(event_summary: str, witnesses_with_goals: list[tuple[str, str]]) -> str:
    """Build a structured prompt asking which goals are advanced by the event."""
    lines = [
        f"Event: {event_summary}",
        "",
        "Online agents and their goals:",
    ]
    for agent_id, goal in witnesses_with_goals:
        lines.append(f'- {agent_id}: "{goal}"')
    lines.append("")
    lines.append(
        'Reply with a JSON array of objects {"agent_id": "<id>", "reason": "<short reason>"} '
        "for each agent whose goal is advanced by this event. If none, reply with []. "
        "Do not include agents whose goal is unrelated."
    )
    return "\n".join(lines)


def _parse_response(text: str) -> dict[str, str]:
    """Parse the LLM's JSON response, tolerant of common formatting artifacts."""
    text = text.strip()
    # Strip code fence if present
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text else text
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("goal_relevance: failed to parse JSON: %r", text[:200])
        return {}

    if not isinstance(data, list):
        return {}

    result: dict[str, str] = {}
    for item in data:
        if isinstance(item, dict) and "agent_id" in item:
            result[item["agent_id"]] = item.get("reason", "")
    return result


def goal_match(
    model_micro,
    event_summary: str,
    witnesses_with_goals: list[tuple[str, str]],
) -> dict[str, str]:
    """Single LLM call; returns {agent_id: reason} for witnesses whose goals are advanced.

    No-op (returns {}) when no witnesses have goals.
    """
    if not witnesses_with_goals:
        return {}
    prompt = _build_prompt(event_summary, witnesses_with_goals)
    try:
        response = model_micro.sample_text(prompt, max_tokens=512, temperature=0.2)
    except Exception:
        logger.exception("goal_relevance LLM call failed")
        return {}
    result = _parse_response(response)
    if result:
        _lazy_emit("GoalOpportunities")
    return result
