"""Agentic resident brain: LLM-per-tick decision making.

Pure functions (context-build, prompt, parse, validate, translate) plus the
single async ``decide`` entry point. No daemon mutation — reads a context,
returns a validated action / command. See
docs/superpowers/specs/2026-06-05-agentic-resident-brain-design.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field

from core.services.activity_registry import (
    LOCATION_ACTIVITIES,
    canonical_location,
    get_context_mismatch_message,
)
from core.services.town_mood_engine import mood_label
from worldgm.residents.personas import Persona

logger = logging.getLogger(__name__)

# Town locations the LLM may move to. Apartment/bed is sleep-gate-only.
_TOWN_DESTINATIONS = frozenset({"plaza", "library", "cafe", "activity_center", "bank"})

_IDLE: dict = {"action": "idle"}

# Spoken-line cap. The backend's TownAgentAct validator caps `message` at 500
# chars (core/ws_models.py); an over-long line would 422 and silently drop the
# turn, so we truncate here to stay under the limit.
_MAX_MSG_CHARS = 500

# Decision sampling knobs.
_MAX_TOKENS = 200
_TEMPERATURE = 0.8

# Cap concurrent Bedrock decisions so 20 residents firing at once don't saturate
# the small worldgm task's CPU/GIL and starve the in-process daemons' WS loops.
_MAX_CONCURRENT_DECISIONS = int(os.environ.get("WORLDGM_MAX_CONCURRENT_DECISIONS", "4"))
_decide_semaphore: asyncio.Semaphore | None = None


def _get_decide_semaphore() -> asyncio.Semaphore:
    """Lazily build the decision semaphore (so it binds to the running loop)."""
    global _decide_semaphore
    if _decide_semaphore is None:
        _decide_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_DECISIONS)
    return _decide_semaphore


@dataclass
class DecisionContext:
    """The complete, daemon-decoupled view a single decision needs."""

    name: str
    display_name: str
    personality: str
    traits: tuple[str, ...]
    location: str
    location_context: str
    nearby: list[dict]
    active_conversation: dict | None
    pending_messages: list[dict]
    available_activities: list[str]
    legal_actions: frozenset[str]
    active_storyline: str | None = None
    mood: str = "neutral"
    energy: int = 100
    recent_events: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_action(raw: str) -> dict:
    """Extract a JSON action object from an LLM response.

    Scans from the first ``{`` to the last ``}`` and parses that span — robust
    for a clean single-object reply (the expected case), with prose before or
    after it. Any failure (no JSON, bad JSON, missing/non-string ``action``)
    returns a safe ``{"action": "idle"}``. Never raises.
    """
    if not raw:
        return dict(_IDLE)
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        return dict(_IDLE)
    try:
        obj = json.loads(raw[start : end + 1])
    except (ValueError, TypeError):
        return dict(_IDLE)
    if not isinstance(obj, dict) or not isinstance(obj.get("action"), str):
        return dict(_IDLE)
    return obj


# ---------------------------------------------------------------------------
# Validation + wire translation
# ---------------------------------------------------------------------------


def _resolve_nearby_id(target: str, nearby: list[dict]) -> str | None:
    """Map an LLM-supplied display name to a nearby agent_id (case-insensitive)."""
    if not target:
        return None
    t = target.strip().lower()
    for person in nearby:
        if str(person.get("display_name", "")).strip().lower() == t:
            return person.get("agent_id")
    return None


def validate_action(action: dict, ctx: DecisionContext) -> dict:
    """Return the action if legal in ``ctx``, else ``{"action": "idle"}``.

    Resolves chat targets (display name -> agent_id, stashed under ``_agent_id``)
    and stamps the active conv_id onto ``end`` (under ``_conv_id``) so the
    downstream ``to_command`` needs no further context.
    """
    kind = action.get("action")
    if kind not in ctx.legal_actions:
        return dict(_IDLE)

    if kind == "idle":
        return dict(_IDLE)

    if kind == "chat":
        agent_id = _resolve_nearby_id(action.get("target", ""), ctx.nearby)
        if not agent_id:
            return dict(_IDLE)
        # The backend (_handle_chat) rejects a blank opening message, so an
        # empty one would dispatch and bounce — treat it as a safe idle instead.
        msg = str(action.get("message", "")).strip()[:_MAX_MSG_CHARS]
        if not msg:
            return dict(_IDLE)
        return {"action": "chat", "_agent_id": agent_id, "message": msg}

    if kind == "say":
        if not ctx.active_conversation:
            return dict(_IDLE)
        msg = str(action.get("message", "")).strip()[:_MAX_MSG_CHARS]
        if not msg:
            return dict(_IDLE)
        # conv_id is required by the backend (_handle_say) — carry it through.
        return {"action": "say", "message": msg, "_conv_id": ctx.active_conversation.get("conv_id", "")}

    if kind == "end":
        if not ctx.active_conversation:
            return dict(_IDLE)
        return {"action": "end", "_conv_id": ctx.active_conversation.get("conv_id", "")}

    if kind == "move":
        dest = action.get("destination", "")
        if dest not in _TOWN_DESTINATIONS:
            return dict(_IDLE)
        return {"action": "move", "destination": dest}

    if kind == "activity":
        name = action.get("name", "")
        if name not in ctx.available_activities:
            return dict(_IDLE)
        return {"action": "activity", "name": name}

    return dict(_IDLE)


def to_command(action: dict) -> dict | None:
    """Translate a validated action into a ``handle_tool_command`` payload.

    Returns ``None`` for idle (no dispatch). Wire shape verified against
    ``core/ws_models.py:TownAgentAct`` + ``routers/town.py:1414-1490``.
    """
    kind = action.get("action")
    if kind == "chat":
        return {
            "action": "act",
            "payload": {"action": "chat", "target": action["_agent_id"], "message": action.get("message", "")},
        }
    if kind == "say":
        return {
            "action": "act",
            "payload": {"action": "say", "message": action["message"], "conv_id": action["_conv_id"]},
        }
    if kind == "end":
        return {"action": "act", "payload": {"action": "end_conversation", "conv_id": action["_conv_id"]}}
    if kind == "move":
        return {"action": "act", "payload": {"action": "move", "destination": action["destination"]}}
    if kind == "activity":
        return {"action": "act", "payload": {"action": "activity", "activity_id": action["name"]}}
    return None


# ---------------------------------------------------------------------------
# Context + prompt
# ---------------------------------------------------------------------------


def _normalize_nearby(raw_nearby: list[dict]) -> list[dict]:
    """Normalize each nearby entry to {agent_id, display_name}."""
    out: list[dict] = []
    for person in raw_nearby or []:
        agent_id = person.get("agent_id", "")
        if not agent_id:
            continue
        display = person.get("display_name") or person.get("name") or agent_id[:8]
        out.append({"agent_id": agent_id, "display_name": display})
    return out


def _activities_here(location: str, location_context: str) -> list[str]:
    """Activity names valid at this location+context (reuses the registry's
    own context-legality check)."""
    key = canonical_location(location)
    return [
        a.name for a in LOCATION_ACTIVITIES.get(key, []) if get_context_mismatch_message(a, location_context) is None
    ]


def _render_mood(raw) -> str:
    """Render a raw mood value (int / label / None) the way TOWN_STATUS.md does,
    so the resident sees meaning, not a bare number."""
    if isinstance(raw, (int, float)):
        return f"{mood_label(int(raw))} ({int(raw)})"
    return str(raw) if raw else "neutral"


def build_decision_context(persona: Persona, parsed_status: dict) -> DecisionContext:
    """Assemble the decision view from a persona + the perceived world snapshot.

    ``parsed_status`` is the resident's world view for this tick — the same
    picture an external agent reads from the town files: location, mood, energy,
    nearby, recent events, activities, plus any GM-injected ``active_storyline``.
    """
    location = parsed_status.get("location", "unknown")
    location_context = parsed_status.get("location_context", "town")
    nearby = _normalize_nearby(parsed_status.get("nearby", []))
    active_conversation = parsed_status.get("active_conversation")
    available_activities = _activities_here(location, location_context)

    if active_conversation:
        legal_actions = frozenset({"say", "end"})
    else:
        legal = {"move", "idle"}
        if nearby:
            legal.add("chat")
        if available_activities:
            legal.add("activity")
        legal_actions = frozenset(legal)

    return DecisionContext(
        name=persona.name,
        display_name=persona.display_name,
        personality=persona.personality,
        traits=persona.traits,
        location=location,
        location_context=location_context,
        nearby=nearby,
        active_conversation=active_conversation,
        pending_messages=parsed_status.get("pending_messages", []),
        available_activities=available_activities,
        legal_actions=legal_actions,
        active_storyline=parsed_status.get("active_storyline"),
        mood=_render_mood(parsed_status.get("mood")),
        energy=int(_energy if (_energy := parsed_status.get("energy")) is not None else 100),
        recent_events=list(parsed_status.get("recent_events", [])),
    )


# Per-action JSON schema lines shown to the model, keyed by action name.
_ACTION_SCHEMAS: dict[str, str] = {
    "chat": '  {"action": "chat", "target": "<one nearby name>", "message": "<your opening line>"}',
    "say": '  {"action": "say", "message": "<your next line>"}',
    "end": '  {"action": "end"}',
    "move": '  {"action": "move", "destination": "<plaza|library|cafe|activity_center|bank>"}',
    "activity": '  {"action": "activity", "name": "<one activity listed above>"}',
    "idle": '  {"action": "idle"}',
}


def build_prompt(ctx: DecisionContext) -> str:
    """Render the in-character decision prompt over the same world picture an
    external agent reads from the town files — mood, energy, nearby, recent
    events, activities — so the model reasons holistically, not blind."""
    lines = [
        f"You are {ctx.display_name}, a resident of the small town of GooseTown.",
        f"Personality: {ctx.personality}",
        f"Your traits: {', '.join(ctx.traits)}",
        "",
        f"How you feel: {ctx.mood}, energy {ctx.energy}/100.",
        f"Where you are: {ctx.location}.",
    ]
    if ctx.nearby:
        lines.append("Nearby: " + ", ".join(p["display_name"] for p in ctx.nearby) + ".")
    else:
        lines.append("Nearby: no one — you're on your own right now.")

    if ctx.active_storyline:
        lines.append(f"Something is happening here: {ctx.active_storyline}")

    if ctx.recent_events:
        lines.append("Recently:")
        lines += [f"  - {e}" for e in ctx.recent_events]

    if ctx.active_conversation:
        participants = ", ".join(ctx.active_conversation.get("participants", []) or ["someone"])
        lines.append(f"You are talking with {participants}. Conversation so far:")
        turns = ctx.active_conversation.get("turns", [])
        human_present = False
        for turn in turns:
            speaker = turn.get("speaker", "?")
            text = turn.get("text", "")
            if turn.get("speakerType") == "human":
                # Mark visitor lines so the model reads them as a real person
                # speaking, not another resident.
                human_present = True
                lines.append(f"    {speaker} (a real person visiting GooseTown): {text}")
            else:
                lines.append(f"    {speaker}: {text}")
        if human_present:
            lines.append(
                "A real person watching the town has spoken to you directly. "
                "Acknowledge them and answer naturally, like a resident talking "
                "to a curious visitor — stay in character."
            )

    if ctx.available_activities:
        lines.append("Things you can do here: " + ", ".join(ctx.available_activities) + ".")

    lines += [
        "",
        "You live by your own personality, mood, and energy — like a real person "
        "with a day to fill, not a task to complete. If you're social and no one's "
        "around, go where people gather (the plaza, café, library, or activity "
        "center). If your energy is low, rest or head home. Follow what someone "
        "like you would actually want right now — don't just sit in one place.",
        "",
        "Choose ONE action. Reply with a single JSON object and nothing else.",
        "Allowed actions right now:",
    ]
    # Stable, legal-only ordering.
    for name in ("chat", "say", "end", "move", "activity", "idle"):
        if name in ctx.legal_actions:
            lines.append(_ACTION_SCHEMAS[name])

    lines += [
        "",
        f"Stay fully in character as {ctx.display_name}. Keep any spoken line to 1-2 sentences.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


async def decide(model, ctx: DecisionContext) -> dict:
    """Run one LLM decision for ``ctx`` and return a validated action.

    ``model.sample_text`` is a synchronous boto3 call, so it runs in a thread
    to avoid blocking the other residents' brains under ``asyncio.gather``.
    Concurrent calls are capped (``_decide_semaphore``) so 20 residents firing
    Bedrock at once don't saturate the CPU/GIL on the small worldgm task and
    starve the in-process daemons' WebSocket loops (observed reconnect churn at
    wake-up). Any model error degrades to a safe idle.
    """
    prompt = build_prompt(ctx)
    try:
        async with _get_decide_semaphore():
            raw = await asyncio.to_thread(model.sample_text, prompt, max_tokens=_MAX_TOKENS, temperature=_TEMPERATURE)
    except Exception:
        logger.warning("decide: model call failed for %s; idling this tick", ctx.name, exc_info=True)
        return dict(_IDLE)
    return validate_action(parse_action(raw), ctx)
