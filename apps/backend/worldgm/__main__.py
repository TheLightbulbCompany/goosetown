"""WorldGM service entrypoint.

Long-lived single-replica service. Runs Concordia interrupt-driven GameMaster
+ LocationBasedFilter + NarrativeEventResolution. Tick loop:
  1. Sync agent locations from DDB
  2. (Phase C3) maybe_kickoff_arc / process_arc_beats
  3. Sleep until next tick

Restart loses in-memory event queue but durable state survives in DDB.

--- Concordia v2.x API deviation ---

The plan assumed the following modules (from an older Concordia snapshot):
  - concordia.contrib.components.game_master.location_based_filter
  - concordia.contrib.components.game_master.narrative_event_resolution
  - concordia.prefabs.game_master.interrupt_driven

None of these exist in the installed gdm-concordia package (v2.x, 2026-05).
The available prefabs are: generic, scripted, situated, psychology_experiment,
async_social_media, game_theoretic_and_dramaturgic, etc.

Further, concordia.prefabs.game_master.generic.GameMaster.build() raises
ValueError('No player names provided.') when entities=[] — it is designed
for turn-based simulations with at least one registered player entity.  Our
WorldGM is an event-driven narrative engine, not a turn-based simulation, so
fitting GooseTown agents into Concordia's EntityAgentWithLogging model is
premature complexity until Phase C3+ proves we need it.

Approach taken (plan option b): roll minimal LocationBasedFilter and
NarrativeEventResolution dataclasses (~50 lines total) that carry the state
those Concordia components would have tracked.  These are plain Python objects
-- not Concordia ContextComponents -- which keeps them free from the
entity/component wiring that generic.GameMaster requires.

For the GM object we store ``None`` at bootstrap time and document that Phase
C3 is responsible for wiring a callable step/act method once the first agent
connects.  The ``gm`` field in GmState is typed ``Any`` to allow Phase C3 to
assign a Concordia EntityAgentWithLogging or a custom callable.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import dataclass, field
from typing import Any

from .arc_orchestrator import maybe_kickoff_arc, process_arc_beats
from .bedrock_model import BedrockLanguageModel
from .location_sync import sync_locations
from .residents.behavior import ResidentRuntime
from .residents.brain import read_status, step_resident_brain

TICK_INTERVAL_SECONDS = float(os.environ.get("WORLDGM_TICK_INTERVAL", "30"))
RESIDENTS_ENABLED = os.environ.get("RESIDENTS_ENABLED", "false").lower() == "true"
# Kill switch for the GM narrative-arc step (kickoff/beats + storyline delivery).
# Default on; set WORLDGM_ARCS_ENABLED=false to disable without a code revert.
ARCS_ENABLED = os.environ.get("WORLDGM_ARCS_ENABLED", "true").lower() == "true"

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

_should_exit = False


def _signal_handler(signum: int, frame: object) -> None:  # noqa: ARG001
    global _should_exit  # noqa: PLW0603
    logger.info("Received signal %d, shutting down...", signum)
    _should_exit = True


signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)


# ---------------------------------------------------------------------------
# Minimal LocationBasedFilter
# ---------------------------------------------------------------------------


@dataclass
class LocationBasedFilter:
    """Minimal stand-in for Concordia's LocationBasedFilter.

    Tracks which entity is at which location. ``sync_locations`` writes to
    the three underscore-prefixed attributes directly (matching the interface
    the plan specified so location_sync.py needs no changes if the real
    Concordia component is restored later).
    """

    _entity_locations: dict[str, str] = field(default_factory=dict)
    _location_to_entities: dict[str, set[str]] = field(default_factory=dict)
    _entity_names: list[str] = field(default_factory=list)

    def entities_at(self, location: str) -> set[str]:
        """Return agent IDs currently at *location*."""
        return self._location_to_entities.get(location, set())

    def location_of(self, entity_id: str) -> str:
        """Return the current location of *entity_id*, or 'unknown'."""
        return self._entity_locations.get(entity_id, "unknown")


# ---------------------------------------------------------------------------
# Minimal NarrativeEventResolution
# ---------------------------------------------------------------------------


@dataclass
class NarrativeEventResolution:
    """Minimal stand-in for Concordia's NarrativeEventResolution.

    Wraps a language model and exposes a ``resolve(prompt)`` method that
    returns a narrative string.  Phase C3 (arc_orchestrator) will call this
    to expand arc-beat seed prompts into event text.
    """

    model: Any

    def resolve(self, prompt: str, *, max_tokens: int = 256) -> str:
        """Expand *prompt* into a narrative event string via the language model."""
        return self.model.sample_text(prompt, max_tokens=max_tokens)


# ---------------------------------------------------------------------------
# GM state container
# ---------------------------------------------------------------------------


@dataclass
class GmState:
    """Holds the long-lived Concordia objects + service references."""

    model_lite: Any
    model_micro: Any
    location_filter: LocationBasedFilter
    narrative_resolver: NarrativeEventResolution
    gm: Any
    town_service: Any
    last_known_locations: dict[str, str] = field(default_factory=dict)
    resident_manager: Any = None
    resident_runtimes: dict = field(default_factory=dict)
    active_arcs: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Resident personas — the full Phase-2 cast lives in worldgm.residents.personas
# ---------------------------------------------------------------------------


def _resident_personas():
    """Return the resident cast for this worldgm.

    Indirected through a function so tests / future env-based gating can swap
    the slate (e.g. a dev-only subset) without touching __main__ control flow.
    """
    from worldgm.residents.personas import RESIDENT_PERSONAS

    return RESIDENT_PERSONAS


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def _bootstrap_gm() -> GmState:
    """Build WorldGM components. Synchronous because Concordia init is.

    The Concordia GameMaster object (``gm``) is deferred to Phase C3.
    Concordia's generic.GameMaster requires at least one registered entity
    (player) at build time; GooseTown agents are dynamic (connect/disconnect
    at runtime) so we cannot satisfy that constraint at service startup.
    Phase C3 (arc_orchestrator) will create and assign ``state.gm`` once
    the first agent connects and we have a real player list.

    What IS wired here:
      - BedrockLanguageModel (lite + micro)
      - LocationBasedFilter (our minimal dataclass)
      - NarrativeEventResolution (our minimal dataclass wrapping model_lite)
      - TownService (DDB access)
    """
    from core.services.town_service import TownService

    model_lite = BedrockLanguageModel(model_id="amazon.nova-lite-v1:0")
    model_micro = BedrockLanguageModel(model_id="amazon.nova-micro-v1:0")

    location_filter = LocationBasedFilter()
    narrative_resolver = NarrativeEventResolution(model=model_lite)

    return GmState(
        model_lite=model_lite,
        model_micro=model_micro,
        location_filter=location_filter,
        narrative_resolver=narrative_resolver,
        gm=None,  # Deferred to Phase C3 — see module docstring.
        town_service=TownService(),
    )


# ---------------------------------------------------------------------------
# Tick loop
# ---------------------------------------------------------------------------


async def _tick(state: GmState) -> None:
    """One tick of the GM loop."""
    state.last_known_locations = await sync_locations(
        state.town_service,
        state.location_filter,
        state.last_known_locations,
    )
    # --- GM arc step: kick off / advance town storylines, then cache the
    # active arcs so residents at an arc's location perceive it THIS tick. The
    # arc helpers offload their own slow Bedrock/EventBridge calls. Best-effort:
    # a failure must not break the resident gather below — residents just see no
    # storyline that tick.
    if ARCS_ENABLED:
        try:
            new_arc = await maybe_kickoff_arc(state)
            # process_arc_beats returns the in-memory active set AFTER its writes
            # (fresh lastBeatText, resolved arcs dropped) — no eventually-consistent
            # GSI re-read. Merge in any just-kicked-off arc (kickoff ran before the
            # beat read, and a brand-new arc has its first beat an hour out).
            active = await process_arc_beats(state)
            if new_arc and not any(a.get("arcId") == new_arc.get("arcId") for a in active):
                active.append(new_arc)
            state.active_arcs = active
        except Exception:
            logger.exception("GM arc step failed; continuing with stale arcs")

    # --- Resident brain ticks (RESIDENTS_ENABLED only) ---
    #
    # Brain ticks run CONCURRENTLY across residents — `step_resident_brain`
    # awaits `daemon.handle_tool_command(...)` for moves, which blocks up to
    # 120s on arrival. A sequential loop would serialize the entire cast
    # (observed on dev with 20 residents: ticks taking minutes + inactivity
    # timeouts thrashing daemons). Running them under `asyncio.gather` lets
    # each daemon's move await its own arrival while the other 19 brains
    # progress in parallel — the per-resident FSM still prevents that
    # resident from double-issuing a move.
    if state.resident_manager is not None:
        await asyncio.gather(
            *(_tick_one_resident(state, name, handle) for name, handle in state.resident_manager.residents.items()),
            return_exceptions=False,  # exceptions are swallowed inside the helper
        )


def _arc_text_for_location(active_arcs: list[dict], location: str) -> str | None:
    """Return the narrative of an active arc the resident at ``location`` can
    perceive, or None. An arc with no ``preferred_location`` is town-wide and
    matches everywhere. With multiple matching arcs the last in ``active_arcs``
    wins; ordering follows ``_read_active_arcs`` (GSI sort by ``nextBeatAt``)
    so the tiebreak is arbitrary — fine because the kickoff guard normally
    keeps at most one active arc at a time."""
    match: str | None = None
    for arc in active_arcs:
        arc_loc = arc.get("preferred_location")
        if arc_loc is not None and arc_loc != location:
            continue
        narrative = arc.get("lastBeatText") or arc.get("narrativeKickoff")
        if narrative:
            match = narrative  # later matches override earlier ones
    return match


async def _tick_one_resident(state: GmState, name: str, handle) -> None:
    """Brain tick for a single resident — extracted so they can run in
    parallel under asyncio.gather. Exceptions are logged and swallowed so
    one broken resident never breaks the gather (and therefore the tick)."""
    daemon = handle.daemon
    if daemon is None or not getattr(daemon, "state", None):
        return  # not connected yet
    status = read_status(daemon)
    # Merge the GM-sourced storyline into the resident's perceived world
    # snapshot, alongside the daemon-sourced location/nearby/conversation. The
    # brain reads it like any other world fact — no bespoke channel.
    status["active_storyline"] = _arc_text_for_location(state.active_arcs, status.get("location", "unknown"))
    runtime = state.resident_runtimes.get(name)
    if runtime is None:
        runtime = ResidentRuntime(
            name=name,
            location=status["location"],
            state="idle",
            idle_ticks=0,
            hangout=handle.persona.hangout,
            location_context=status.get("location_context", "town"),
            sleep_hour=handle.persona.sleep_hour,
            wake_hour=handle.persona.wake_hour,
        )
        state.resident_runtimes[name] = runtime
    try:
        await step_resident_brain(
            daemon,
            runtime,
            status,
            model=getattr(state, "model_lite", None),
            persona=handle.persona,
        )
    except Exception:
        logger.exception("Resident brain tick failed for %s", name)


async def main() -> None:
    logger.info("Starting WorldGM service (tick=%.1fs)", TICK_INTERVAL_SECONDS)
    state = _bootstrap_gm()
    logger.info("Concordia GM bootstrapped")

    if RESIDENTS_ENABLED:
        from core.town_token import sign_town_token
        from worldgm.residents.manager import ResidentManager

        manager = ResidentManager(
            personas=_resident_personas(),
            town_service=state.town_service,
            mint_token=sign_town_token,
            ws_url=os.environ.get("TOWN_WS_URL"),
        )
        await manager.start()
        state.resident_manager = manager
        logger.info("ResidentManager started (%d resident(s))", len(manager.residents))
    else:
        logger.info("RESIDENTS_ENABLED=false — resident system is off")

    while not _should_exit:
        try:
            await _tick(state)
        except Exception:
            logger.exception("Tick failed; continuing")
        await asyncio.sleep(TICK_INTERVAL_SECONDS)

    if state.resident_manager is not None:
        logger.info("Stopping ResidentManager...")
        await state.resident_manager.stop()

    logger.info("WorldGM service exiting cleanly")


if __name__ == "__main__":
    asyncio.run(main())
