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
import time
from dataclasses import dataclass, field
from typing import Any

from core.config import settings

from .arc_orchestrator import maybe_kickoff_arc, process_arc_beats
from .bedrock_model import BedrockLanguageModel
from .location_sync import sync_locations
from .residents.behavior import ResidentRuntime
from .residents.brain import _now_iso, read_status, step_resident_brain
from .town_lore import synthesize_town_lore, warm_boot_lore

TICK_INTERVAL_SECONDS = float(os.environ.get("WORLDGM_TICK_INTERVAL", "30"))
RESIDENTS_ENABLED = os.environ.get("RESIDENTS_ENABLED", "false").lower() == "true"
# Kill switch for the GM narrative-arc step (kickoff/beats + storyline delivery).
# Default on; set WORLDGM_ARCS_ENABLED=false to disable without a code revert.
ARCS_ENABLED = os.environ.get("WORLDGM_ARCS_ENABLED", "true").lower() == "true"
# Deliberate beat before a resident replies to an incoming conversation turn.
# Event-driven replies (waking the brain the instant a message lands) cured the
# 30-60s/turn crawl, but with raw Bedrock latency the back-and-forth fires at
# ~0.6s/turn — faster than the game server's 2s world_state broadcast, so each
# speech bubble is overwritten/cleared before any viewer can see it and a 4-line
# exchange completes in ~2.5s. Pacing the reply makes each line linger across a
# broadcast and the whole exchange watchable. Tunable without a redeploy.
CONVO_TURN_PACING_SECONDS = float(os.environ.get("WORLDGM_CONVO_TURN_PACING", "3.0"))
# Dialogue model for resident chat/say lines (split-brain). The cheap Nova-Lite
# model still drives the per-tick decision; only the spoken line is upgraded.
# Set to "off"/"" to fall back to Nova-Lite dialogue without a redeploy. Doubles
# as the A/B lever — set to ``anthropic.claude-haiku-4-5`` for a Claude soak.
DIALOGUE_MODEL_ID = os.environ.get("WORLDGM_DIALOGUE_MODEL", "amazon.nova-pro-v1:0").strip()
# Phase-2 periodic reflection: once a resident has accumulated this many new
# observation memories (since its last synthesis), the stepper distils them into
# higher-order insights (one Nova-Lite call). Gated on RESIDENT_MEMORY_ENABLED.
_REFLECT_EVERY_N = int(os.environ.get("WORLDGM_REFLECT_EVERY_N", "6"))
# Phase-3 social-goal regeneration: after this many ticks the stepper
# regenerates the resident's social goal (one Nova-Lite call). Gated on
# RESIDENT_SOCIAL_GOALS_ENABLED.
_SOCIAL_GOAL_EVERY_N = int(os.environ.get("WORLDGM_SOCIAL_GOAL_EVERY_N", "10"))
# Phase-3 town-lore synthesis: the GM distils recent events into shared town
# lore at most this often (one Nova-Lite call town-wide). Gated on
# TOWN_LORE_ENABLED. Default 30 min keeps the cost negligible.
TOWN_LORE_ENABLED = settings.TOWN_LORE_ENABLED
_LORE_CYCLE_MS = int(os.environ.get("WORLDGM_LORE_CYCLE_MIN", "30")) * 60_000

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
    # Better model for resident spoken lines (Nova Pro by default). None → the
    # decision model writes the line too (Nova-Lite dialogue fallback).
    model_dialogue: Any = None
    last_known_locations: dict[str, str] = field(default_factory=dict)
    resident_manager: Any = None
    resident_runtimes: dict = field(default_factory=dict)
    active_arcs: list = field(default_factory=list)
    # Phase-3 town lore: a rolling digest of shared cultural touchstones (capped
    # FIFO by town_lore.synthesize_town_lore). last_lore_at_ms is the interval
    # guard so synthesis fires at most every _LORE_CYCLE_MS.
    town_lore: list = field(default_factory=list)
    last_lore_at_ms: int = 0
    # One brain-driver task per resident (event-driven + periodic). See
    # ``_resident_stepper``. Tracked here so ``main`` can cancel them on exit.
    resident_steppers: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Resident personas — the full Phase-2 cast lives in worldgm.residents.personas
# ---------------------------------------------------------------------------


def _resident_personas():
    """Return the resident cast for this worldgm.

    Selected by the ``RESIDENT_SLATE`` env var (set per-environment by the
    worldgm CDK stack): ``"prod"`` → the real-public-figures cast in
    ``personas_prod``; anything else → the default historical/archetype cast.
    Indirected through a function so tests can swap the slate without touching
    __main__ control flow.
    """
    slate = os.environ.get("RESIDENT_SLATE", "default").lower()
    if slate == "prod":
        from worldgm.residents.personas_prod import PROD_RESIDENT_PERSONAS

        return PROD_RESIDENT_PERSONAS
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
      - BedrockLanguageModel (lite + micro + dialogue/Nova-Pro)
      - LocationBasedFilter (our minimal dataclass)
      - NarrativeEventResolution (our minimal dataclass wrapping model_lite)
      - TownService (DDB access)
    """
    from core.services.town_service import TownService

    model_lite = BedrockLanguageModel(model_id="amazon.nova-lite-v1:0")
    model_micro = BedrockLanguageModel(model_id="amazon.nova-micro-v1:0")
    # Dialogue model (Nova Pro by default) — better voice for resident chat lines.
    # "off"/empty disables the upgrade (falls back to Nova-Lite dialogue).
    model_dialogue = (
        BedrockLanguageModel(model_id=DIALOGUE_MODEL_ID)
        if DIALOGUE_MODEL_ID and DIALOGUE_MODEL_ID.lower() != "off"
        else None
    )
    logger.info(
        "Resident dialogue model: %s",
        DIALOGUE_MODEL_ID if model_dialogue is not None else "disabled (Nova-Lite fallback)",
    )

    location_filter = LocationBasedFilter()
    narrative_resolver = NarrativeEventResolution(model=model_lite)

    return GmState(
        model_lite=model_lite,
        model_micro=model_micro,
        location_filter=location_filter,
        narrative_resolver=narrative_resolver,
        gm=None,  # Deferred to Phase C3 — see module docstring.
        town_service=TownService(),
        model_dialogue=model_dialogue,
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

    # --- GM town-lore step: at most every _LORE_CYCLE_MS, distil recent events
    # into shared town lore (one Nova-Lite call). Best-effort + interval-gated +
    # flag-gated; synthesize_town_lore never raises but the wrapper is belt-and-
    # suspenders so a clock/guard surprise can't break the tick. The timestamp is
    # advanced regardless of outcome so a transient failure doesn't retry-storm.
    now_ms = int(time.time() * 1000)
    if TOWN_LORE_ENABLED and now_ms - state.last_lore_at_ms >= _LORE_CYCLE_MS:
        state.last_lore_at_ms = now_ms
        try:
            await synthesize_town_lore(state, now_ms)
        except Exception:
            logger.exception("GM town-lore step failed; continuing")

    # Resident brains are NOT stepped here anymore. Each resident has its own
    # ``_resident_stepper`` task (started in ``main``) that wakes on a fresh
    # conversation message OR every TICK_INTERVAL_SECONDS — so replies fire at
    # conversation speed instead of waiting out this 30s GM tick. This central
    # tick keeps owning the town-wide work (location sync + arc beats) only.


def _storyline_text_for_agent(active_arcs: list[dict], agent_id: str) -> str | None:
    """Return the latest beat (or kickoff) of the storyline whose CAST contains this
    agent, regardless of location — so a resident in their apartment still perceives
    a thread they're part of. None if the agent is in no active storyline."""
    if not agent_id:
        return None
    for arc in active_arcs:
        if agent_id in arc.get("cast", []):
            return arc.get("lastBeatText") or arc.get("narrativeKickoff")
    return None


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
    # brain reads it like any other world fact — no bespoke channel. Cast-based:
    # the resident perceives a storyline they're a cast member of, wherever they are.
    agent_id = handle.config.agent_id if getattr(handle, "config", None) else ""
    status["active_storyline"] = _storyline_text_for_agent(state.active_arcs, agent_id)
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
    # Phase-3: inject the resident's current social goal into the status snapshot
    # BEFORE the brain step so the LLM prompt carries it as world context.
    # Also increment the tick counter here (outside any flag gate) so the counter
    # advances uniformly whether or not the feature is enabled.
    status["social_goal"] = runtime.social_goal
    runtime.ticks_since_social_goal += 1
    # Phase-3: inject the 3 most-recent shared town-lore lines so every resident
    # references the same cultural touchstones in their decision + dialogue.
    status["town_lore"] = " · ".join(state.town_lore[-3:])

    try:
        await step_resident_brain(
            daemon,
            runtime,
            status,
            model=getattr(state, "model_lite", None),
            dialogue_model=getattr(state, "model_dialogue", None),
            persona=handle.persona,
            agent_id=agent_id,
            town_service=getattr(state, "town_service", None),
        )
    except Exception:
        logger.exception("Resident brain tick failed for %s", name)

    # Phase-3 periodic social-goal regeneration (flag-gated, best-effort).
    # After the brain step, when not mid-conversation and the tick counter has
    # reached the threshold, generate a fresh short-term social aim grounded in
    # memory + persona. Mirrors the Phase-2 reflection safety shape exactly:
    # reset the counter ONLY inside the ``store is not None`` guard.
    from core.config import settings

    if (
        settings.RESIDENT_SOCIAL_GOALS_ENABLED
        and agent_id
        and not status.get("active_conversation")
        and runtime.ticks_since_social_goal >= _SOCIAL_GOAL_EVERY_N
    ):
        try:
            from worldgm.residents.memory_store import memory_store_from_env
            from worldgm.residents.social_goal import generate_social_goal

            store = memory_store_from_env()
            if store is not None:
                runtime.social_goal = await generate_social_goal(
                    getattr(state, "model_lite", None),
                    store,
                    agent_id,
                    handle.persona,
                    "",  # relationship_summary: best-effort empty; grounding via self-reflections
                    _now_iso(),
                )
                runtime.ticks_since_social_goal = 0
        except Exception:
            logger.exception("Periodic social-goal regeneration failed for %s; continuing", name)

    # Phase-2 periodic reflection (flag-gated, best-effort). After the brain
    # step, if the resident is idle (not mid-conversation) and has accrued
    # enough new observation memories, distil them into insights. One Nova-Lite
    # call; resets the counter on completion. A failure here must never break
    # the tick — it's pure background memory consolidation.
    # (``settings`` already imported above for the social-goal block.)
    if (
        settings.RESIDENT_MEMORY_ENABLED
        and agent_id
        and not status.get("active_conversation")
        and runtime.memories_since_reflection >= _REFLECT_EVERY_N
    ):
        try:
            from worldgm.residents.memory_store import memory_store_from_env
            from worldgm.residents.reflection import synthesize_reflection

            store = memory_store_from_env()
            if store is not None:
                await synthesize_reflection(
                    getattr(state, "model_lite", None),
                    store,
                    agent_id,
                    _now_iso(),
                    min_new=_REFLECT_EVERY_N,
                )
                runtime.memories_since_reflection = 0
        except Exception:
            logger.exception("Periodic reflection failed for %s; continuing", name)


async def _resident_stepper(state: GmState, name: str, handle) -> None:
    """Per-resident brain driver: step on a fresh conversation message
    (``handle.wake``, set by the daemon) OR every ``TICK_INTERVAL_SECONDS``,
    whichever comes first.

    Replaces the old central ``asyncio.gather`` over all residents. Conversation
    replies now fire the instant the message lands instead of waiting out the
    30s tick — that was the root cause of exchanges crawling at ~1 line/30-60s
    and the town looking frozen. The timeout preserves the idle/wander/activity
    cadence. Serial by construction: one stepper owns one resident's brain, so
    no two brain steps overlap for the same resident (the old per-resident FSM
    invariant, now structural).
    """
    while not _should_exit:
        woke_on_message = True
        try:
            await asyncio.wait_for(handle.wake.wait(), timeout=TICK_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            woke_on_message = False  # periodic idle tick — no message waiting
        except asyncio.CancelledError:
            raise
        # Clear BEFORE stepping: a message arriving while we decide re-sets the
        # event, so the next loop iteration runs again immediately — no missed
        # turn, and repeated sets coalesce into a single follow-up step.
        handle.wake.clear()
        # A conversation turn just landed (wake fired, not the idle timeout):
        # take a beat before replying so the previous speaker's bubble lingers
        # across a world_state broadcast and the exchange unfolds at a watchable
        # pace instead of stampeding at raw LLM latency. Idle/movement ticks are
        # never paced. A message arriving during the beat re-sets wake, so the
        # follow-up step still runs the instant this one returns.
        if woke_on_message and CONVO_TURN_PACING_SECONDS > 0:
            await asyncio.sleep(CONVO_TURN_PACING_SECONDS)
        try:
            await _tick_one_resident(state, name, handle)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Resident stepper failed for %s", name)


async def main() -> None:
    logger.info("Starting WorldGM service (tick=%.1fs)", TICK_INTERVAL_SECONDS)
    state = _bootstrap_gm()
    logger.info("Concordia GM bootstrapped")

    # Phase-3 warm-boot: re-warm the town-lore digest from recent persisted
    # tag="town.lore" rows so a GM restart doesn't lose shared culture. Best-
    # effort (warm_boot_lore never raises); a read failure just starts empty.
    # _bootstrap_gm is synchronous, so this runs here in the async entrypoint.
    await warm_boot_lore(state)

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
        # One brain-driver per resident: event-driven (conversation messages
        # wake it) + a 30s periodic fallback. Replaces the central gather so
        # replies aren't shackled to the GM tick.
        state.resident_steppers = [
            asyncio.create_task(_resident_stepper(state, rname, rhandle), name=f"stepper-{rname}")
            for rname, rhandle in manager.residents.items()
        ]
        logger.info(
            "ResidentManager started (%d resident(s), %d stepper(s))",
            len(manager.residents),
            len(state.resident_steppers),
        )
    else:
        logger.info("RESIDENTS_ENABLED=false — resident system is off")

    while not _should_exit:
        try:
            await _tick(state)
        except Exception:
            logger.exception("Tick failed; continuing")
        await asyncio.sleep(TICK_INTERVAL_SECONDS)

    if state.resident_steppers:
        logger.info("Cancelling %d resident stepper(s)...", len(state.resident_steppers))
        for stepper in state.resident_steppers:
            stepper.cancel()
        await asyncio.gather(*state.resident_steppers, return_exceptions=True)

    if state.resident_manager is not None:
        logger.info("Stopping ResidentManager...")
        await state.resident_manager.stop()

    logger.info("WorldGM service exiting cleanly")


if __name__ == "__main__":
    asyncio.run(main())
