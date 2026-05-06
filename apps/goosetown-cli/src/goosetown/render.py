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

The bigger renderer extraction (the four "static status" templates
in ``_on_connected`` / ``_on_sprite_ready`` / ``_on_context_changed``
/ ``_on_wake``) is deliberately **not** in this PR. Each of those is
a 10-30 line literal-string template; collapsing them into one
parameterised helper is a behaviour-equivalence-test-heavy change
that's better in its own focused PR.
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
