"""Agentic resident brain: LLM-per-tick decision making.

Pure functions (context-build, prompt, parse, validate, translate) plus the
single async ``decide`` entry point. No daemon mutation — reads a context,
returns a validated action / command. See
docs/superpowers/specs/2026-06-05-agentic-resident-brain-design.md.
"""

from __future__ import annotations

import asyncio
import hashlib
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

# Decision sampling knobs (the cheap decision model — picks the action).
_MAX_TOKENS = 200
_TEMPERATURE = 0.8

# Dialogue sampling knobs (the better dialogue model — writes the spoken line).
# Slightly hotter for voice/creativity; 160 tokens covers 1-3 sentences with room.
_DIALOGUE_MAX_TOKENS = 160
_DIALOGUE_TEMPERATURE = 0.9


def _dialogue_temperature(slug: str) -> float:
    """Stable per-resident temperature in [0.8, 1.0], so the cast doesn't collapse
    to one phrasing distribution. SHA-256 (not builtin hash, which is salted) →
    stable across processes; not security-sensitive, just deterministic bucketing."""
    h = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    return round(0.8 + (h % 5) * 0.05, 2)  # 0.80, 0.85, 0.90, 0.95, 1.00


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
    # Character-bible fields (copied from the Persona) — fuel the dialogue prompt.
    voice: str = ""
    obsessions: tuple[str, ...] = ()
    opinions: tuple[tuple[str, str], ...] = ()
    current_life: str = ""
    disagreeableness: int = 3
    remembered: str = ""
    beliefs: str = ""
    role: str = ""
    social_goal: str = ""
    town_lore: str = ""


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


# Relationship/goal/stance fields the backend (daemon_bridge) injects per nearby
# agent. Carried through verbatim so the resident reasons over the same picture a
# real user's agent reads from TOWN_STATUS.md — not a name-only subset.
_NEARBY_REL_FIELDS = (
    "currentGoal",
    "stanceFromMe",
    "stanceFromThem",
    "affinity",
    "interactionCount",
    "relationshipType",
)


def _normalize_nearby(raw_nearby: list[dict]) -> list[dict]:
    """Normalize each nearby entry to {agent_id, display_name, +relationship}.

    Preserves the goal / stance / affinity fields the backend injects (see
    ``daemon_bridge._send_to_daemon``) so the resident brain reaches parity with
    what an external agent already reads from its town files — and so the
    dialogue model can colour a line by the real relationship.
    """
    out: list[dict] = []
    for person in raw_nearby or []:
        agent_id = person.get("agent_id", "")
        if not agent_id:
            continue
        display = person.get("display_name") or person.get("name") or agent_id[:8]
        entry = {"agent_id": agent_id, "display_name": display}
        for fld in _NEARBY_REL_FIELDS:
            if person.get(fld) is not None:
                entry[fld] = person[fld]
        out.append(entry)
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
        voice=persona.voice,
        obsessions=persona.obsessions,
        opinions=persona.opinions,
        current_life=persona.current_life,
        disagreeableness=persona.disagreeableness,
        remembered=parsed_status.get("remembered", ""),
        beliefs=parsed_status.get("beliefs", ""),
        role=persona.role,
        social_goal=parsed_status.get("social_goal", ""),
        town_lore=parsed_status.get("town_lore", ""),
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


def _affinity_label(score: int) -> str:
    """Map an affinity score (-100..100) to a human word."""
    if score >= 50:
        return "close"
    if score >= 20:
        return "friendly"
    if score <= -50:
        return "hostile"
    if score <= -20:
        return "frosty"
    return "neutral"


def _relationship_phrase(near: dict) -> str:
    """One-line relationship colour for a nearby agent, from the affinity /
    stance / goal fields the backend injects. Empty string when nothing is set."""
    parts: list[str] = []
    aff = near.get("affinity")
    if aff is not None:
        n = int(near.get("interactionCount") or 0)
        label = _affinity_label(int(aff))
        parts.append(f"{label}, talked {n}×" if n else label)
    mine = (near.get("stanceFromMe") or "").strip()
    if mine:
        parts.append(f"you feel {mine}")
    goal = (near.get("currentGoal") or "").strip()
    if goal:
        parts.append(f"they're after: {goal}")
    return "; ".join(parts)


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
        lines.append("Nearby:")
        for p in ctx.nearby:
            rel = _relationship_phrase(p)
            lines.append(f"  - {p['display_name']}" + (f" ({rel})" if rel else ""))
    else:
        lines.append("Nearby: no one — you're on your own right now.")

    if ctx.active_storyline:
        lines.append(f"Something is happening here: {ctx.active_storyline}")

    if ctx.recent_events:
        lines.append("Recently:")
        lines += [f"  - {e}" for e in ctx.recent_events]

    if ctx.role or ctx.social_goal:
        bits = []
        if ctx.role:
            bits.append(f"You are {ctx.display_name}, the town's {ctx.role}.")
        if ctx.social_goal:
            bits.append(f"Right now you want to: {ctx.social_goal}.")
        bits.append(
            "Gravitate toward people you click with, steer clear of those you clash with, "
            "and act on what you're after — don't drift aimlessly."
        )
        lines.append(" ".join(bits))

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
        n_turns = len(turns)
        if n_turns >= 4:
            lines.append(
                f"This exchange has run {n_turns} turns. People make their point and move on — they don't "
                "agree back and forth forever. If you've said what you came to say (or it's going in circles), "
                "choose 'end'."
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


# ---------------------------------------------------------------------------
# Dialogue (split-brain): the decision model picks the action, the dialogue
# model writes the spoken line. Separating them lets the cheap model drive the
# 30s decision cadence while a better model performs the (rarer) spoken lines.
# ---------------------------------------------------------------------------


def _find_nearby(ctx: DecisionContext, agent_id: str) -> dict | None:
    for p in ctx.nearby:
        if p.get("agent_id") == agent_id:
            return p
    return None


def _clean_line(raw: str, speaker: str) -> str:
    """Reduce a dialogue reply to the bare spoken words.

    Strips a leading ``Name:`` prefix and wrapping quotes the model sometimes
    adds, then caps at the backend's message limit.
    """
    s = (raw or "").strip()
    prefix = f"{speaker}:"
    if s[: len(prefix)].lower() == prefix.lower():
        s = s[len(prefix) :].strip()
    if len(s) >= 2 and s[0] in "\"'“”" and s[-1] in "\"'“”":
        s = s[1:-1].strip()
    return s[:_MAX_MSG_CHARS]


def _disagreeableness_instruction(level: int) -> str:
    """Trait-conditioned guidance on how readily this resident agrees.

    Heterogeneous disagreeableness — not uniform agreement — is what makes
    multi-agent conversation believable (2026-06-24 design, §4.2). NO chain-of-
    thought 'do I really agree?' step: that measurably amplifies sycophancy.
    """
    if level >= 4:
        return (
            "You don't soften your view to be liked. If you see it differently, say so plainly "
            "and hold your ground — agreement should cost them a real reason."
        )
    if level <= 2:
        return "You're warm and easy, but still bring your own real opinion — don't just validate them."
    return "Speak your honest take. Agree when you mean it; push back when you don't."


def build_dialogue_prompt(ctx: DecisionContext, action: dict) -> str:
    """Voice-first prompt for the dialogue model.

    Unlike ``build_prompt`` (which asks for a JSON action), this asks for ONLY
    the spoken line — the full character bible, the relationship with whoever
    they're addressing, and the conversation so far — so the model performs the
    character instead of stuffing a line into an action.
    """
    lines = [f"You are {ctx.display_name}, a resident of GooseTown. Stay completely in character."]
    if ctx.personality:
        lines.append(f"Who you are: {ctx.personality}")
    if ctx.voice:
        lines.append(f"How you talk: {ctx.voice}")
    if ctx.obsessions:
        lines.append("You love steering toward: " + ", ".join(ctx.obsessions) + ".")
    if ctx.current_life:
        lines.append(f"On your mind lately: {ctx.current_life}")

    # Who am I addressing, and what's the history?
    addressee = None
    addressee_name = "them"
    history: list[str] = []
    if action.get("action") == "say" and ctx.active_conversation:
        others = [p for p in (ctx.active_conversation.get("participants") or []) if p != ctx.display_name]
        addressee_name = others[0] if others else "them"
        for p in ctx.nearby:
            if p.get("display_name") == addressee_name:
                addressee = p
                break
        for turn in ctx.active_conversation.get("turns", []):
            spk = turn.get("speaker", "?")
            tag = " (a real person visiting GooseTown)" if turn.get("speakerType") == "human" else ""
            history.append(f"  {spk}{tag}: {turn.get('text', '')}")
    else:  # chat — opening a new conversation
        addressee = _find_nearby(ctx, action.get("_agent_id", ""))
        addressee_name = (addressee or {}).get("display_name") or action.get("target") or "them"

    take = dict(ctx.opinions).get(addressee_name)
    if take:
        lines.append(f"Your honest read on {addressee_name}: {take}")
    if addressee:
        rel = _relationship_phrase(addressee)
        if rel:
            lines.append(f"Where you stand with {addressee_name}: {rel}.")

    if ctx.remembered:
        lines.append(f"What you actually remember about {addressee_name}: {ctx.remembered}")
    if ctx.beliefs:
        lines.append(f"Lately you've come to believe: {ctx.beliefs}")

    if ctx.active_storyline:
        lines.append(f"Happening around you: {ctx.active_storyline}")
    if ctx.town_lore:
        lines.append(f"Town lore everyone knows: {ctx.town_lore}")

    lines.append(f"Right now: you're at the {ctx.location}, feeling {ctx.mood}.")
    if ctx.recent_events:
        lines.append("What's actually happened lately:")
        lines += [f"  - {e}" for e in ctx.recent_events]

    lines.append("")
    if history:
        lines.append(f"You're mid-conversation with {addressee_name}. So far:")
        lines += history
        lines.append("")
        lines.append(
            f"Say your next line as {ctx.display_name}: react to what they just said — get specific, "
            f"add something new. {_disagreeableness_instruction(ctx.disagreeableness)}"
        )
    else:
        lines.append(
            f"You're walking up to {addressee_name} to start talking. Open with something only "
            f"{ctx.display_name} would say — specific to who you are and your read on them, not a generic "
            f"greeting. {_disagreeableness_instruction(ctx.disagreeableness)}"
        )
    lines.append("Don't restate your last point or reach for your usual catchphrases — move it forward or land it.")
    lines.append(
        "Talk about what's actually here — what they just said, a real recent event, what's going on in "
        "town. Do NOT invent events, places, projects, or people that aren't above. "
        "Reply with ONLY the words you speak — 1-3 sentences, no name prefix, no quotation marks, no stage directions."
    )
    return "\n".join(lines)


async def generate_dialogue(model, ctx: DecisionContext, action: dict) -> str:
    """Generate the spoken line for a chat/say action on the dialogue model.

    Returns "" on any failure so the caller falls back to the decision model's
    own line. Shares the decide semaphore so dialogue + decision calls together
    respect the Bedrock concurrency cap.
    """
    prompt = build_dialogue_prompt(ctx, action)
    try:
        async with _get_decide_semaphore():
            raw = await asyncio.to_thread(
                model.sample_text,
                prompt,
                max_tokens=_DIALOGUE_MAX_TOKENS,
                temperature=_dialogue_temperature(ctx.name),
            )
    except Exception:
        logger.warning("generate_dialogue: model call failed for %s; using fallback line", ctx.name, exc_info=True)
        return ""
    return _clean_line(raw, ctx.display_name)


async def decide_and_speak(decision_model, dialogue_model, ctx: DecisionContext) -> dict:
    """Split-brain decision: the cheap ``decision_model`` picks the action; for a
    chat/say the (better) ``dialogue_model`` writes the actual spoken line.

    Falls back to the decision model's own line when no dialogue model is given
    or the dialogue call fails — so a Nova-Pro outage degrades to Nova-Lite
    dialogue, never to silence.
    """
    action = await decide(decision_model, ctx)
    if dialogue_model is not None and action.get("action") in ("chat", "say"):
        line = await generate_dialogue(dialogue_model, ctx, action)
        if line:
            action["message"] = line
    return action
