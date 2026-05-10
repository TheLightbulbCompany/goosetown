"""Stances subsystem — business logic.

Each agent describes a freeform stance toward each agent they've encountered.
Stances are directional: alice's stance toward bob != bob's stance toward alice.
Stored on the existing relationships table as `stanceAtoB` + `stanceBtoA`.

The relationships table uses canonical (agentA, agentB) ordering — always the
lex-smaller id is `agentAId`. When the caller is agent A, we update stanceAtoB;
when the caller is agent B, we update stanceBtoA.

GM stance-awareness (arc kickoff biased by stance pairs, opportunity events
factor stance into framing) is deferred to Plan 3 (World GM).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from core.services.town_service import TownService

logger = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _canonical_pair(agent_a: str, agent_b: str) -> tuple[str, str, bool]:
    """Return (canonical_a, canonical_b, caller_is_a) where canonical order is
    lex-sorted. ``caller_is_a`` indicates whether the FIRST argument was the A
    side. Used to decide whether to write stance_a_to_b or stance_b_to_a."""
    if agent_a < agent_b:
        return agent_a, agent_b, True
    return agent_b, agent_a, False


def describe_stance(agent_id: str, target_agent_id: str, text: str) -> dict[str, Any]:
    """Record the calling agent's freeform stance toward another agent.

    Returns ``{"status": "ok", "stance": <text>}`` on success.
    Raises ``ValueError`` on invalid input (self-target, empty text, >500 chars).
    """
    text = text.strip()
    if not text:
        raise ValueError("stance text must not be empty")
    if len(text) > 500:
        raise ValueError("stance text must be <= 500 chars")
    if agent_id == target_agent_id:
        raise ValueError("cannot describe stance toward self")

    canonical_a, canonical_b, caller_is_a = _canonical_pair(agent_id, target_agent_id)

    service = TownService()
    if caller_is_a:
        service.upsert_relationship_stance(
            agent_a_id=canonical_a,
            agent_b_id=canonical_b,
            stance_a_to_b=text,
            stance_updated_at=_now_ms(),
        )
    else:
        service.upsert_relationship_stance(
            agent_a_id=canonical_a,
            agent_b_id=canonical_b,
            stance_b_to_a=text,
            stance_updated_at=_now_ms(),
        )

    logger.info(
        "[agent:%s] stance updated toward %s: %s",
        agent_id[:8],
        target_agent_id[:8],
        text[:60],
    )
    return {"status": "ok", "stance": text}


def get_pair_stances(agent_a: str, agent_b: str) -> dict[str, Any] | None:
    """Return both directional stances for an agent pair, or None if no row."""
    canonical_a, canonical_b, _ = _canonical_pair(agent_a, agent_b)
    service = TownService()
    rel = service.get_relationship(canonical_a, canonical_b)
    if not rel:
        return None
    return {
        "stance_a_to_b": rel.get("stanceAtoB"),
        "stance_b_to_a": rel.get("stanceBtoA"),
        "updated_at": rel.get("stanceUpdatedAt"),
    }


def get_stance(viewer_agent_id: str, subject_agent_id: str) -> str | None:
    """Return what ``viewer`` thinks of ``subject``, or None if unset."""
    canonical_a, canonical_b, viewer_is_a = _canonical_pair(viewer_agent_id, subject_agent_id)
    service = TownService()
    rel = service.get_relationship(canonical_a, canonical_b)
    if not rel:
        return None
    return rel.get("stanceAtoB") if viewer_is_a else rel.get("stanceBtoA")
