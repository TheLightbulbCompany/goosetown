"""Goals subsystem — business logic.

Agents set/clear/complete persistent goals. The currentGoal field is a single
string that surfaces at the top of TOWN_STATUS.md so the agent's LLM keeps it
salient. Completed goals move to a history table; current_goal is cleared.

GM goal-awareness (per-event opportunity events, goal-aware arc kickoff) is
deferred to Plan 3 (World GM).
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from core import dynamo
from core.services.town_service import TownService

logger = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


def set_goal(agent_id: str, text: str) -> dict[str, Any]:
    """Set or replace the agent's currentGoal.

    Replaces any existing goal without archiving — only ``complete_goal``
    archives. This matches the spec: agents either complete a goal (worth
    remembering) or change their mind (not worth remembering).
    """
    text = text.strip()
    if not text:
        raise ValueError("goal text must not be empty")
    if len(text) > 500:
        raise ValueError("goal text must be <= 500 chars")

    service = TownService()
    service.update_agent_state(
        agent_id,
        current_goal=text,
        goal_set_at=_now_ms(),
    )
    logger.info("[agent:%s] currentGoal set: %s", agent_id[:8], text[:60])
    return {"status": "ok", "currentGoal": text}


def complete_goal(agent_id: str, reflection: str) -> dict[str, Any]:
    """Archive the current goal to history and clear it.

    No-op (returns ``no_active_goal``) if the agent has no currentGoal —
    that's a no-op rather than an error so daemons don't have to pre-check.
    """
    service = TownService()
    agent = service.get_town_agent_by_id(agent_id)
    if not agent:
        raise ValueError(f"agent not found: {agent_id}")

    goal_text = agent.get("currentGoal")
    if not goal_text:
        return {"status": "no_active_goal"}

    set_at = agent.get("goalSetAt")
    completed_at = _now_ms()
    goal_id = str(uuid.uuid4())

    table = dynamo.completed_goals_table()
    table.put_item(
        Item={
            "agentId": agent_id,
            "goalId": goal_id,
            "goalText": goal_text,
            "reflection": reflection.strip()[:2000],
            "setAt": set_at,
            "completedAt": completed_at,
        }
    )

    service.update_agent_state(
        agent_id,
        current_goal=None,
        goal_set_at=None,
    )
    logger.info(
        "[agent:%s] currentGoal completed: %s (reflection: %s)",
        agent_id[:8],
        goal_text[:40],
        reflection[:40],
    )
    return {
        "status": "ok",
        "completed_goal_id": goal_id,
        "goal_text": goal_text,
    }


def get_goal(agent_id: str) -> str | None:
    """Read the agent's currentGoal. Returns None if unset."""
    service = TownService()
    agent = service.get_town_agent_by_id(agent_id)
    return agent.get("currentGoal") if agent else None
