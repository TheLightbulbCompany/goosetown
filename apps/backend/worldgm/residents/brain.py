"""Resident brain: reads daemon state and issues movement commands.

One brain tick = one call to step_resident_brain().

Phase 2 scope:
  - Movement: drift toward hangout, occasional wander.
  - Sleep cycle: each persona has a UTC sleep window; in-window residents go
    to bed and noop, costing nothing (no Bedrock until the chat brain ships).
  - Conversation is a NO-OP STUB. Bedrock-backed replies are Phase 3.

The rng and now_utc_hour parameters are injectable so tests can drive the
brain deterministically.
"""

import datetime
import logging
import random

from core.config import settings
from worldgm.residents import reflection
from worldgm.residents.agentic_brain import (
    _resolve_nearby_id,
    build_decision_context,
    decide_and_speak,
    to_command,
)
from worldgm.residents.behavior import _BED, ResidentRuntime, is_in_sleep_window, next_action
from worldgm.residents.memory_store import memory_store_from_env

logger = logging.getLogger(__name__)


def _recent_events(daemon, limit: int = 6) -> list[str]:
    """Tail the resident's TOWN_EVENTS.md — the same event stream an external
    agent reads. Returns the last ``limit`` event lines, or [] if the file is
    absent/unreadable (never raises)."""
    workspace = getattr(daemon, "_workspace_path", None)
    if workspace is None:
        return []
    try:
        path = workspace / "TOWN_EVENTS.md"
        if not path.exists():
            return []
        lines = [ln.strip() for ln in path.read_text().splitlines() if ln.strip().startswith("- ")]
        return [ln.lstrip("- ").strip() for ln in lines[-limit:]]
    except Exception:
        return []


def read_status(daemon) -> dict:
    """Extract the decision-relevant view from the daemon's live state.

    Surfaces the same world picture an external agent reads from the town
    files — mood, energy, nearby, activity, recent events — so the resident
    brain reasons over the full situation, not a thin subset.

    Returns a dict with keys:
        location           str   current agent location (or "unknown")
        location_context   str   "town" | "apartment" (default "town")
        activity           str   current activity string (or "idle")
        mood               int|str  current mood (raw from state)
        energy             int   current energy (default 100)
        active_conversation  dict | None
        nearby             list  nearby agents [{agent_id, name/display_name}]
        pending_messages   list  unread incoming conversation turns
        recent_events      list  last few TOWN_EVENTS.md lines
    """
    state = getattr(daemon, "state", None) or {}
    agent = state.get("agent") or {}
    return {
        "location": agent.get("location", "unknown"),
        "location_context": agent.get("location_context", "town"),
        "activity": agent.get("activity", "idle"),
        "mood": agent.get("mood"),
        "energy": agent.get("energy", 100),
        "active_conversation": state.get("active_conversation"),
        "nearby": state.get("nearby", []),
        "pending_messages": state.get("pending_messages", []),
        "recent_events": _recent_events(daemon),
    }


def _now_utc_hour() -> int:
    """Current UTC hour 0-23. Wrapper so tests can stub time without freezegun."""
    return datetime.datetime.now(datetime.UTC).hour


def _now_iso() -> str:
    """Current UTC time as ISO-8601. Wrapper so tests can stub the clock — the
    memory store stamps createdAt from a caller-supplied iso (deterministic)."""
    return datetime.datetime.now(datetime.UTC).isoformat()


def _other_participant_name(turns: list, participants: list, my_display_name: str) -> str | None:
    """Resolve the other participant's display name from the cached conversation.

    Prefers the participants list (display names); falls back to the first
    non-self speaker in the transcript. Returns None when nobody else is found
    (e.g. a solo/empty conversation) — caller then skips reflection.
    """
    mine = (my_display_name or "").strip().lower()
    for p in participants or []:
        if str(p).strip().lower() != mine:
            return str(p)
    _self_labels = {"you", "me", "i"}
    for turn in turns or []:
        speaker = str(turn.get("speaker", "")).strip()
        if speaker and speaker.lower() != mine and speaker.lower() not in _self_labels:
            return speaker
    return None


async def _maybe_reflect_on_ended_conversation(
    runtime: ResidentRuntime,
    parsed_status: dict,
    *,
    model,
    store,
    town_service,
    my_id: str,
    persona,
    now_iso: str,
) -> None:
    """If a cached conversation just ended (active_conversation went set→None),
    reflect on it exactly once. Best-effort: any failure logs and returns.

    Caches the live transcript while still in the conversation; on the tick
    where it disappears, writes one residue memory + relationship update via
    ``reflection.reflect_on_conversation``. Idempotent per conv_id via
    ``runtime.reflected_conv_ids``.
    """
    active = parsed_status.get("active_conversation")
    if active:
        # Still talking — keep the transcript and participants fresh for when it ends.
        runtime.last_conv_id = active.get("conv_id") or runtime.last_conv_id
        runtime.last_conv_turns = list(active.get("turns", []))
        runtime.last_conv_participants = list(active.get("participants", []))
        return

    conv_id = runtime.last_conv_id
    if not conv_id or conv_id in runtime.reflected_conv_ids:
        return

    # The conversation has ended and we have not reflected on it yet. Mark it
    # reflected up-front so a failure (or a missing other participant) never
    # retries on every subsequent tick — best-effort, one shot per conv.
    runtime.reflected_conv_ids.add(conv_id)
    turns = list(runtime.last_conv_turns)
    participants = list(runtime.last_conv_participants)
    runtime.last_conv_id = None
    runtime.last_conv_turns = []
    runtime.last_conv_participants = []

    # Resolve the other side. Prefer the cached participants list (display names
    # populated from the live active_conversation while it was in progress).
    # Fall back to scanning the transcript — the hardened fallback skips
    # daemon self-labels ("you", "me", "i") so it never returns "you" as the
    # other participant even when participants is empty.
    other_name = _other_participant_name(turns, participants, getattr(persona, "display_name", ""))
    if not other_name:
        return
    other_id = _resolve_nearby_id(other_name, parsed_status.get("nearby", [])) or ""

    try:
        ok = await reflection.reflect_on_conversation(
            model, store, town_service, my_id, other_id, other_name, turns, now_iso
        )
        if ok:
            runtime.memories_since_reflection += 1
    except Exception:
        logger.exception("reflect_on_conversation failed for %s; continuing", runtime.name)


def _recall_addressee_name(parsed_status: dict, persona) -> str | None:
    """Pick whom to recall about this tick: the conversation partner if mid-chat,
    otherwise the first nearby agent. Returns a display name, or None when there's
    no one to remember (so recall is skipped — no DDB read)."""
    active = parsed_status.get("active_conversation")
    if active:
        return _other_participant_name(
            active.get("turns", []),
            active.get("participants", []),
            getattr(persona, "display_name", ""),
        )
    for person in parsed_status.get("nearby", []) or []:
        name = person.get("display_name") or person.get("name")
        if name:
            return str(name)
    return None


def _inject_recall(parsed_status: dict, store, my_id: str, persona, now_iso: str) -> None:
    """Stuff recalled memory about the relevant other into ``parsed_status``.

    Best-effort and read-only on the world snapshot: resolves the addressee
    (conversation partner or nearest nearby), reads the top-k memories about
    them, and sets ``parsed_status["remembered"]``. Also sets
    ``parsed_status["beliefs"]`` from the resident's own self-reflections
    (synthesized insights stored with subject="self"). Skips (and reads
    nothing) when there is no addressee for the ``remembered`` path. Any
    failure logs and leaves the key unset."""
    addressee = _recall_addressee_name(parsed_status, persona)
    if addressee:
        try:
            remembered = reflection.recall_about(store, my_id, addressee, now_iso)
            if remembered:
                parsed_status["remembered"] = remembered
        except Exception:
            logger.exception("recall_about failed for %s; no recall this tick", my_id)
    try:
        beliefs = reflection.recall_self(store, my_id, now_iso)
        if beliefs:
            parsed_status["beliefs"] = beliefs
    except Exception:
        logger.exception("recall_self failed for %s; skipping beliefs this tick", my_id)


async def step_resident_brain(
    daemon,
    runtime: ResidentRuntime,
    parsed_status: dict,
    *,
    model=None,
    dialogue_model=None,
    persona=None,
    rng=None,
    now_utc_hour: int | None = None,
    agent_id: str = "",
    town_service=None,
    now_iso: str | None = None,
) -> None:
    """One brain tick for one resident: update runtime from status, decide, act.

    With ``model`` (and ``persona``) supplied, the LLM decides the action each
    tick from the town state — choosing move / chat / say / end / activity /
    idle. Without a model (or on any failure upstream), the deterministic FSM
    drives movement + sleep, preserving the legacy behavior so a Bedrock outage
    degrades gracefully.

    Deterministic pre-gates skip the LLM on non-decision ticks: asleep (go to
    bed), mid-move (the move await gates re-entry), and in-conversation-but-not-
    my-turn (wait for the other speaker). See the design spec.

    Args:
        daemon:        The resident's TownDaemon (handle_tool_command is async).
        runtime:       Mutable FSM state — updated in-place each tick.
        parsed_status: Output of read_status(daemon) for this tick.
        model:         Optional LLM (BedrockLanguageModel). None → FSM fallback.
        persona:       Optional Persona, required for the agentic prompt.

    A town storyline, if any, rides in ``parsed_status["active_storyline"]``
    (GM-injected) and is read like any other world fact in the snapshot.
        rng:           Optional zero-arg callable returning float in [0,1).
                       Defaults to random.random.  Injectable for tests.
        now_utc_hour:  Optional int 0-23. Defaults to the current UTC hour.
                       Injectable so tests don't need freezegun.
        agent_id:      The resident's own agent_id (UUID). Required for the
                       Phase-2 memory layer (rememberer key + relationship
                       writes). Empty → memory is skipped this tick.
        town_service:  Object exposing update_relationship / upsert_relationship_
                       stance — receives the conversation-residue affinity/stance
                       updates. Injectable for tests. None → relationship updates
                       are skipped (memory still written).
        now_iso:       Optional ISO-8601 timestamp used to stamp memories.
                       Defaults to the current UTC time.

    Phase-2 memory (all gated on settings.RESIDENT_MEMORY_ENABLED and only on
    the agentic path): caches the live transcript each in-conversation tick; on
    the tick where active_conversation goes set→None, reflects on the ended
    conversation (one Nova-Lite call → residue memory + relationship delta);
    and, before deciding, injects recalled memory about the nearby/addressee
    into ``parsed_status["remembered"]``. Best-effort — any failure logs and the
    tick proceeds exactly as Phase 1.
    """
    hour = now_utc_hour if now_utc_hour is not None else _now_utc_hour()

    # ------------------------------------------------------------------ #
    # No model: deterministic FSM fallback (verbatim legacy behavior).    #
    # ------------------------------------------------------------------ #
    if model is None or persona is None:
        if parsed_status.get("active_conversation"):
            return
        location = parsed_status.get("location", "unknown")
        if location != runtime.location:
            runtime.idle_ticks = 0  # just arrived somewhere new
        runtime.location = location
        runtime.location_context = parsed_status.get("location_context", "town")
        runtime.state = "idle"
        runtime.idle_ticks += 1
        cmd = next_action(runtime, (rng or random.random)(), now_utc_hour=hour)
        if cmd.get("action") == "noop":
            return
        runtime.state = "moving"
        runtime.idle_ticks = 0
        # Blocks until arrival/timeout — this await is the movement gate.
        await daemon.handle_tool_command(cmd)
        return

    # ------------------------------------------------------------------ #
    # Agentic path: the LLM decides.                                      #
    # ------------------------------------------------------------------ #
    # Reconcile runtime with reported state (dwell tracking). The blocking
    # handle_tool_command await below is the REAL movement gate: the worldgm
    # tick awaits each resident's brain to completion, so it cannot re-enter
    # this resident until the prior move/chat returns. We therefore reset
    # state to "idle" every tick rather than gating on state=="moving" — a
    # mid-move gate would freeze the resident after its first move, since
    # state stays "moving" once the (completed) move's await returns.
    location = parsed_status.get("location", "unknown")
    if location != runtime.location:
        runtime.idle_ticks = 0  # just arrived somewhere new
    runtime.location = location
    runtime.location_context = parsed_status.get("location_context", "town")
    runtime.state = "idle"
    runtime.idle_ticks += 1

    # ---- Phase 2 memory: conversation-residue + recall injection (flag-gated). #
    # Done before the sleep / not-my-turn gates so a conversation that ended on a
    # tick where the resident is now asleep (or has nothing pending) is still
    # reflected on. Pure read/write side-channel — touches neither the move/chat
    # dispatch nor the tick gates. Entirely best-effort.
    if settings.RESIDENT_MEMORY_ENABLED and agent_id:
        store = None
        try:
            store = memory_store_from_env()
        except Exception:
            logger.exception("memory_store_from_env failed for %s; skipping memory this tick", runtime.name)
        if store is not None:
            iso = now_iso if now_iso is not None else _now_iso()
            await _maybe_reflect_on_ended_conversation(
                runtime,
                parsed_status,
                model=model,
                store=store,
                town_service=town_service,
                my_id=agent_id,
                persona=persona,
                now_iso=iso,
            )
            _inject_recall(parsed_status, store, agent_id, persona, iso)

    # Pre-gate: sleep window (deterministic, no LLM spend).
    if is_in_sleep_window(hour, runtime.sleep_hour, runtime.wake_hour):
        if runtime.location == _BED:
            return
        runtime.state = "moving"
        runtime.idle_ticks = 0
        await daemon.handle_tool_command({"action": "act", "payload": {"action": "move", "destination": _BED}})
        return

    # Pre-gate: in a conversation but it isn't my turn → wait (no LLM spend).
    if parsed_status.get("active_conversation") and not parsed_status.get("pending_messages"):
        return

    # Decide via the LLM, then dispatch.
    ctx = build_decision_context(persona, parsed_status)
    # Split-brain: ``model`` (cheap) picks the action; ``dialogue_model`` (better,
    # e.g. Nova Pro) writes the spoken line for chat/say. dialogue_model=None →
    # the decision model's own line is used (graceful fallback).
    action = await decide_and_speak(model, dialogue_model, ctx)
    cmd = to_command(action)
    if cmd is None:
        # Mid-conversation the only legal actions are say/end, so an idle here
        # means the model produced nothing usable. Don't re-bill the LLM every
        # tick waiting for it to recover — end the conversation deterministically
        # (the backend's inactivity timer would do this eventually anyway).
        active_conv = parsed_status.get("active_conversation")
        if active_conv:
            await daemon.handle_tool_command(
                {
                    "action": "act",
                    "payload": {"action": "end_conversation", "conv_id": active_conv.get("conv_id", "")},
                }
            )
        return  # idle
    if cmd["payload"]["action"] == "move":
        runtime.state = "moving"
        runtime.idle_ticks = 0
    # Blocks until arrival/verdict — this await gates re-entry for moves/chats.
    await daemon.handle_tool_command(cmd)
