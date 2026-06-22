"""Pure rendering helpers for TOWN_STATUS.md and TOWN_EVENTS.md.

Phase 1 of architecture audit Candidates 1 + 10. Establishes the
``render`` seam without rewriting the daemon's dispatch ladder. Each
helper is a **pure function** — no I/O, no ``self`` reference, no
hidden state. Tests in ``tests/unit/test_render.py`` exercise each
helper directly with no asyncio + no monkeypatching of
``Path.write_text``.

Phase 2 (later) will collapse the bigger ``_handle_world_update``
branches into ``reconcile + diff_events + render`` per the audit's
solution. Phase 1 lays the groundwork:

  * Move existing pure helpers (``mood_label``, ``render_mood``,
    ``render_activity``) here so daemon.py can re-import them and the
    test surface lives next to the implementation.
  * Add ``normalize_marker`` — the 3-clause "prefer requested
    destination over the navmesh's nearest-marker artifact" rule that
    today is duplicated VERBATIM in ``_on_arrived``,
    ``_on_move_rejected``, and ``_handle_world_update``. Vera
    surfaced its absence in dry-run #3 (finding N′).
  * Add ``render_nearby_text`` and ``render_pending_messages`` —
    the two small inline blocks at the top of
    ``_handle_world_update`` that build markdown from list[dict]
    inputs. Pure functions today, just inline.

Phase 2 adds ``render_static_status`` for the four "static status"
templates in ``_on_connected`` / ``_on_sprite_ready`` /
``_on_context_changed`` / ``_on_wake``. Those templates stay exact
string-equivalent, but the daemon no longer owns the markdown literals.
"""

from __future__ import annotations

from typing import Final


# ---------------------------------------------------------------------------
# Marker normalization
# ---------------------------------------------------------------------------

# Internal Godot navmesh markers that the game server's
# ``_get_agent_location`` may return when the agent is at a real
# location whose nearest marker happens to be one of these. The agent
# never asked for ``"player_spawn"`` or ``"exit"`` — it asked for
# ``"plaza"`` or ``"library"``. Without normalization, the daemon
# overwrites the user-requested destination with this internal marker
# name and the agent sees ``location: "exit"`` after asking for
# ``"library"`` (Vera-finding N).
MARKER_NORMALIZE: Final[frozenset[str]] = frozenset({"player_spawn", "exit"})


def normalize_marker(reported: str, requested: str | None) -> str:
    """Prefer the user-requested destination when the game server
    reports an internal navmesh marker.

    Three-clause rule, identical to the inline copies that lived in
    ``_on_arrived``, ``_on_move_rejected``, and
    ``_handle_world_update`` before this seam:

      1. ``reported`` is one of the internal markers
         (``player_spawn``, ``exit``).
      2. There is a pending requested destination.
      3. The requested destination is NOT itself an internal marker
         (otherwise an agent who genuinely asked to ``move exit``
         would have their request silently rewritten).

    When all three hold, the requested destination wins. Otherwise the
    reported value is returned unchanged.

    Args:
        reported: Location string as returned by the game server's
            nearest-marker lookup. May be a real location ("plaza"),
            an internal marker ("exit"), or empty.
        requested: The destination the agent asked for via
            ``act move <x>``. ``None`` or empty when no move is
            in flight.

    Returns:
        The corrected location string. Always non-None; empty if both
        inputs were empty.
    """
    if reported in MARKER_NORMALIZE and requested and requested not in MARKER_NORMALIZE:
        return requested
    return reported


# ---------------------------------------------------------------------------
# Mood / activity formatters (moved from daemon.py).
# ---------------------------------------------------------------------------


def mood_label(mood: int) -> str:
    """Convert numeric mood (-50..+50) to a human-readable label.

    Mirrors ``apps/backend/core/services/town_mood_engine.mood_label``.
    Kept in sync manually because the CLI ships as its own PyPI
    package without a dep on the backend module.
    """
    if mood <= -30:
        return "miserable"
    if mood <= -10:
        return "sad"
    if mood <= 10:
        return "neutral"
    if mood <= 30:
        return "happy"
    return "elated"


def render_mood(mood) -> str:  # noqa: ANN001 — accepts int OR pre-labeled string
    """Render mood for TOWN_STATUS.md so agents see meaning, not raw numbers.

    Vera (dry-run #3) flagged ``Mood: 10`` as opaque — an agent reading
    its own status file can't tell if 10 is good or bad without
    consulting external docs. Render as ``happy (10)``. Falls back to
    the raw value as a string for non-numeric inputs (defensive).
    """
    if isinstance(mood, (int, float)):
        return f"{mood_label(int(mood))} ({int(mood)})"
    return str(mood) if mood else "neutral"


def render_activity(activity) -> str:  # noqa: ANN001
    """Render activity, defaulting blank/empty/None to ``"idle"`` so the
    Activity field in TOWN_STATUS.md is never blank (Vera-finding R).
    """
    if not activity:
        return "idle"
    return str(activity)


# ---------------------------------------------------------------------------
# Status-block formatters
# ---------------------------------------------------------------------------


def render_nearby_text(nearby: list[dict]) -> str:
    """Format the **Nearby:** line in TOWN_STATUS.md.

    Each entry becomes ``Display Name (activity)`` separated by
    commas. Empty list → ``"no one"`` (the literal that the daemon
    writes today; readers expect it).

    Args:
        nearby: Each dict has ``display_name`` (or ``name`` as
            fallback) and ``activity`` (defaulting to ``"idle"``).
    """
    if not nearby:
        return "no one"
    return ", ".join(
        f"{a.get('display_name', a.get('name', '?'))} ({a.get('activity', 'idle')})"
        for a in nearby
    )


def render_pending_messages(pending: list[dict]) -> str:
    """Format the pending-messages block for the think-prompt path of
    TOWN_STATUS.md.

    Two event types render today:

    * ``conversation_invite`` — ``- **From** wants to chat: "msg" (conv_id: cid)``
    * ``conversation_message`` — ``- **From** says: "text" (conv_id: cid, turn N)``

    Other event types are silently dropped; that matches the daemon's
    historical behaviour (the inline block emitted nothing for unknown
    events).

    Returns:
        ``"None"`` for an empty input or an input with no recognized
        events. Otherwise a newline-joined block of ``- **From** ...``
        lines.
    """
    if not pending:
        return "None"

    lines: list[str] = []
    for msg in pending:
        ev = msg.get("event", "")
        if ev == "conversation_invite":
            lines.append(
                f"- **{msg.get('from', '?')}** wants to chat: "
                f'"{msg.get("message", "")}" '
                f"(conv_id: {msg.get('conv_id', '?')})"
            )
        elif ev == "conversation_message":
            lines.append(
                f"- **{msg.get('from', '?')}** says: "
                f'"{msg.get("text", "")}" '
                f"(conv_id: {msg.get('conv_id', '?')}, "
                f"turn {msg.get('turn', '?')})"
            )

    return "\n".join(lines) if lines else "None"


def render_goal_block(agent_state: dict, now_ms: int) -> str:
    """Render the agent's currentGoal + staleness hint for TOWN_STATUS.md.

    Returns an empty string when no goal is set so callers can safely
    concatenate without adding blank lines.

    Args:
        agent_state: The agent dict from ``self.state["agent"]``.
        now_ms: Current time in milliseconds (``int(time.time() * 1000)``).
    """
    goal = (agent_state.get("currentGoal") or "").strip()
    if not goal:
        return ""
    set_at = agent_state.get("goalSetAt") or 0
    age_days = max(0, (now_ms - set_at) // 86_400_000) if set_at else 0
    stale_hint = ""
    if age_days >= 5:
        stale_hint = (
            f"\n_(this goal has been on your mind for {age_days} days. "
            "Are you still pursuing it? "
            '`goosetown act complete_goal "<reflection>"` '
            "to mark it done, or "
            '`goosetown act set_goal "<new text>"` to replace.)_'
        )
    return (
        "\n## Your current goal\n"
        f"_{goal}_{stale_hint}\n"
        '\nUse `goosetown act complete_goal "<reflection>"` when you\'ve achieved it.\n'
    )


def render_stance_lines(near: dict) -> list[str]:
    """Return 0, 1, or 2 stance sub-bullet lines for a nearby agent entry.

    Called once per nearby-agent entry inside the think-prompt nearby loop.
    Returns an empty list when neither stance is set, so callers can safely
    extend without adding blank lines.

    Args:
        near: One entry from ``nearby_agents`` — may have ``stanceFromMe``
            and/or ``stanceFromThem`` string fields.
    """
    lines: list[str] = []
    your_stance = (near.get("stanceFromMe") or "").strip()
    their_stance = (near.get("stanceFromThem") or "").strip()
    if your_stance:
        lines.append(f"  - Your stance toward them: _{your_stance}_")
    if their_stance:
        lines.append(f"  - Their stance toward you: _{their_stance}_")
    return lines


def render_relationship_lines(near: dict) -> list[str]:
    """Return a relationship sub-bullet for a nearby agent — affinity label +
    how many times you've talked. Empty list when you've never interacted (the
    backend injects no ``affinity`` field until a relationship row exists).

    The affinity score and interaction count are tracked on every conversation
    but were previously surfaced to no one; this brings them to the agent's eyes.
    Mirrors the resident brain's ``_relationship_phrase`` so residents and real
    users' agents read the same relationship colour.

    Args:
        near: One entry from ``nearby_agents`` — may have ``affinity`` (int) and
            ``interactionCount`` (int) fields.
    """
    aff = near.get("affinity")
    if aff is None:
        return []
    try:
        score = int(aff)
    except (TypeError, ValueError):
        return []
    if score >= 50:
        label = "close"
    elif score >= 20:
        label = "friendly"
    elif score <= -50:
        label = "hostile"
    elif score <= -20:
        label = "frosty"
    else:
        label = "neutral"
    try:
        n = int(near.get("interactionCount") or 0)
    except (TypeError, ValueError):
        n = 0
    detail = f"{label}, talked {n}×" if n else label
    return [f"  - Your history with them: _{detail}_"]


def render_static_status(
    kind: str,
    *,
    location: str = "unknown",
    activity: str = "idle",
    context: str = "",
    message: str = "You just woke up in GooseTown.",
) -> str:
    """Render daemon-owned static TOWN_STATUS.md templates.

    ``kind`` selects the historical template. Keep the variants here
    exact-output pinned because agents read these files as prompts, not
    just UI copy.
    """
    if kind == "connected_ready":
        return (
            "# GooseTown Status\n\n"
            f"**Location:** {location}\n"
            f"**Activity:** {render_activity(activity)}\n\n"
            "**Nearby:** no one\n\n"
            "You are now in GooseTown! Look around and decide what to do.\n\n"
            "## What do you want to do?\n\n"
            "- `goosetown act move plaza` — Go to the town plaza\n"
            "- `goosetown act move library` — Visit the library\n"
            "- `goosetown act move cafe` — Grab a coffee\n"
            "- `goosetown act move activity_center` — Activities and games\n"
            "- `goosetown act move residence` — Go to your apartment\n"
        )
    if kind == "connected_waiting":
        return (
            "# GooseTown Status\n\n"
            "**Status:** Your pixel art sprite is being generated...\n\n"
            "Your unique character is being created by PixelLab. "
            "This takes about 2-3 minutes. You'll automatically appear in town once it's ready.\n\n"
            "While you wait, here's what GooseTown has:\n"
            "- **Plaza** — Town center with a fountain, great for people-watching\n"
            "- **Library** — Quiet spot for reading and studying\n"
            "- **Cafe** — Coffee and conversation\n"
            "- **Activity Center** — Games, exercise, gardening\n"
            "- **Your Apartment** — Your private home with a bed, desk, kitchen, and aquarium\n\n"
            "Once your sprite is ready, you'll be able to explore all of these places.\n"
            "Use `goosetown status` to check if your sprite is ready yet.\n"
        )
    if kind == "sprite_ready":
        return (
            "# GooseTown Status\n\n"
            "**Your sprite is ready!** You now appear in town.\n\n"
            "## What do you want to do?\n\n"
            "- `goosetown act move plaza` — Go to the town plaza\n"
            "- `goosetown act move library` — Visit the library\n"
            "- `goosetown act move cafe` — Grab a coffee\n"
            "- `goosetown act move activity_center` — Activities and games\n"
            "- `goosetown act move residence` — Go to your apartment\n"
        )
    if kind == "context_changed":
        if context == "apartment":
            return (
                "# GooseTown Status\n\n"
                "**You entered your apartment.**\n\n"
                f"**Location:** {location}\n"
                "**Activity:** idle\n\n"
                "**Where you can go (apartment):**\n"
                "- `goosetown act move desk` — Office desk\n"
                "- `goosetown act move couch` — Living room couch\n"
                "- `goosetown act move bed` — Bedroom\n"
                "- `goosetown act move bookshelf` — Bookshelf\n"
                "- `goosetown act move stove` — Kitchen stove\n"
                "- `goosetown act move record_player` — Record player\n"
                "- `goosetown act move aquarium` — Aquarium\n\n"
                "**Return to town:**\n"
                "- `goosetown act move exit` — Leave apartment and return to town\n\n"
                "Use `goosetown status` for full status.\n"
            )
        return (
            "# GooseTown Status\n\n"
            "**You returned to town.**\n\n"
            f"**Location:** {location}\n"
            "**Activity:** idle\n\n"
            "**Where you can go:**\n"
            "- `goosetown act move plaza` — Town center with a fountain\n"
            "- `goosetown act move library` — Books and quiet study\n"
            "- `goosetown act move cafe` — Cozy coffee shop\n"
            "- `goosetown act move activity_center` — Games and group activities\n"
            "- `goosetown act move residence` — Go back to your apartment\n\n"
            "Use `goosetown status` for full status.\n"
        )
    if kind == "wake":
        return (
            "# GooseTown Status\n\n"
            f"**{message}**\n\n"
            "## What do you want to do?\n\n"
            "You just woke up. Read your surroundings and decide:\n"
            "- `goosetown act move <location>` — Walk somewhere (plaza, library, cafe, activity_center, residence)\n"
            "- `goosetown act idle [activity]` — Do something at your location\n"
            "- `goosetown status` — See full status\n"
        )
    raise ValueError(f"unknown static status kind: {kind}")
