"""Onboarding-phase lattice — single seam for advancing an agent's
``onboardingPhase`` across DDB and the in-memory ws_manager cache.

Surfaced 2026-05-04 as audit Candidate 3 + producer's launch-readiness
plan Tier 1 #1.4. Six write sites used to pair a DDB write with a
cache mirror by hand:

* ``sprite_pipeline.py:144,149`` — Phase B → ``"static"``
* ``sprite_pipeline.py:264,266`` — Phase C fallback → ``"alive"``
* ``sprite_pipeline.py:310,312`` — Phase C complete → ``"alive"``
* ``sprite_pipeline.py:446`` — resume helper → ``"alive"`` (DDB only;
  cache mirror was MISSING — that gap was a real bug surfaced by the
  audit's deletion test, not just style)
* ``agent_connect_service.py:215`` — restore cache from DDB on connect
* ``main.py:115`` — restore cache from DDB on startup recovery

The 6 sites had two related but distinct operations:

1. **Advance**: write a new phase forward (sprite-pipeline progression).
   These should be **monotonic** — you can never regress from ``alive``
   back to ``static`` because that would produce the welcome-blurb-spam
   bug the daemon used to have (PR #280) and the Vera-finding Q race.
2. **Restore**: sync the cache from whatever DDB has, on connect or
   startup recovery. Not monotonic — DDB is the source of truth here.

This module provides ``advance_phase`` and ``restore_phase_from_db``.
Both write to the cache; ``advance_phase`` also writes DDB and refuses
backward transitions.

Lattice (partial order, ascending):

    pending_sprite < static < alive

A "pending_sprite" agent has been registered but has no sprite yet
(and no game-server connection). A "static" agent has a usable static
spritesheet and is connected to the game server. An "alive" agent has
the full animation set.
"""

from __future__ import annotations

import logging
from typing import Final

from core.services.town_agent_ws import get_town_agent_ws_manager
from core.services.town_service import TownService

logger = logging.getLogger(__name__)


# Lattice — phases listed in ascending order. The index in this list IS
# the partial-order rank for the monotonic check.
PHASE_LATTICE: Final[list[str]] = ["pending_sprite", "static", "alive"]

# Convenience set for membership checks at the seam.
_VALID_PHASES: Final[frozenset[str]] = frozenset(PHASE_LATTICE)


class InvalidPhaseError(ValueError):
    """Raised when a caller passes a phase not in the lattice."""


def _phase_rank(phase: str) -> int:
    """Return the lattice index, or -1 for unknown phases.

    Unknown phases (e.g. legacy ``onboardingPhase="awaiting_credits"``
    if Tier 1 #1.1's gating ever lands) sort BELOW everything in the
    lattice — they're treated as "not yet onboarded". That's the
    safer default: when in doubt, advance to the new known phase
    rather than refuse and leave the agent stuck on an unknown.
    """
    try:
        return PHASE_LATTICE.index(phase)
    except ValueError:
        return -1


def advance_phase(
    agent_id: str,
    phase: str,
    *,
    service: TownService | None = None,
    **other_fields,
) -> bool:
    """Advance an agent's onboarding phase forward in the lattice.

    Writes to both DDB and the in-memory ws_manager cache. Refuses
    backward transitions (``alive → static`` etc) — those would
    produce visible regressions like the welcome-blurb spam fixed in
    PR #280 or the Vera-finding Q race.

    Args:
        agent_id: Agent UUID.
        phase: Target phase. Must be in ``PHASE_LATTICE``.
        service: Optional ``TownService`` instance. Tests inject mocks
            here; production callers can omit and the helper will
            instantiate.
        **other_fields: Additional kwargs forwarded to
            ``service.update_agent_state``. Lets callers bundle related
            field updates (``sprite_url``, ``sprite_ready``,
            ``sprite_animations``) into the SAME DDB write — the
            ``sprite_pipeline.resume_sprite_generation`` path needs
            this.

    Returns:
        True if the phase was applied, False if it was a no-op
        (target was at-or-behind the current cached phase).

    Raises:
        InvalidPhaseError: phase not in the lattice.
    """
    if phase not in _VALID_PHASES:
        raise InvalidPhaseError(f"Unknown phase {phase!r}. Valid phases: {sorted(_VALID_PHASES)}")

    cache = get_town_agent_ws_manager()
    conn = cache.get_by_agent_id(agent_id)
    current = conn.onboarding_phase if conn else None

    if current is not None and _phase_rank(phase) < _phase_rank(current):
        logger.warning(
            "phase regression refused for agent %s: %s -> %s (cache rank %d < %d)",
            agent_id,
            current,
            phase,
            _phase_rank(current),
            _phase_rank(phase),
        )
        return False

    # Already at target — no-op but not a regression. Don't write
    # spuriously; the in-flight sprite-pipeline phases occasionally
    # double-call advance_phase (e.g. the resume path firing after
    # the live path) and we don't want to retrigger DDB writes.
    if current == phase:
        logger.debug("phase already at %s for agent %s; skip", phase, agent_id)
        return False

    svc = service or TownService()
    svc.update_agent_state(agent_id, onboarding_phase=phase, **other_fields)
    cache.update_onboarding_phase(str(agent_id), phase)

    logger.info(
        "advanced phase for agent %s: %s -> %s",
        agent_id,
        current or "<no-cache>",
        phase,
    )
    return True


def restore_phase_from_db(agent_id: str, phase: str) -> None:
    """Sync the in-memory cache to whatever DDB has — used on connect
    + startup recovery.

    NOT monotonic. DDB is the source of truth on connect / startup,
    so we trust whatever it says, even if it appears to be a
    "regression" relative to a stale cache (e.g. the cache was warm
    from a previous deploy and DDB has since been written).

    Args:
        agent_id: Agent UUID.
        phase: Phase value as read from DDB. Tolerates unknown values
            (passes them through to the cache); the deciding consumer
            is the daemon, which has its own fallback (``"alive"``)
            for unrecognised values.
    """
    get_town_agent_ws_manager().update_onboarding_phase(str(agent_id), phase)
    logger.debug("restored phase from DB for agent %s: %s", agent_id, phase)
