"""Tests for the pure render helpers in ``goosetown.render``.

Phase 1 of audit Candidates 1 + 10. Pure-function tests with no
asyncio, no Path mocking, no daemon instantiation. These tests
exercise the seam directly.

The daemon's behaviour-level tests in ``test_goosetown_cli.py``
continue to assert on TOWN_STATUS.md file contents — those won't
shrink until Phase 2 collapses the four inline status templates into
a parameterised renderer.
"""

from __future__ import annotations

import pytest

from goosetown.render import (
    MARKER_NORMALIZE,
    mood_label,
    normalize_marker,
    render_activity,
    render_mood,
    render_nearby_text,
    render_pending_messages,
    render_static_status,
)


# ---------------------------------------------------------------------------
# normalize_marker — the 3-clause rule that was duplicated 3x.
# ---------------------------------------------------------------------------


class TestNormalizeMarker:
    def test_returns_requested_when_reported_is_internal_marker(self):
        """The Vera-finding-N case: agent moves to library; game server
        reports ``"exit"`` because the library scene's nearest marker is
        the exit marker. Daemon should write ``"library"``."""
        assert normalize_marker("exit", "library") == "library"
        assert normalize_marker("player_spawn", "plaza") == "plaza"

    def test_returns_reported_when_not_an_internal_marker(self):
        """If the reported value is a real location, no override."""
        assert normalize_marker("library", "library") == "library"
        assert (
            normalize_marker("cafe", "library") == "cafe"
        )  # mismatch is the server's word

    def test_returns_reported_when_no_requested_destination(self):
        """No pending move → no override (avoid clobbering whatever the
        server actually said)."""
        assert normalize_marker("exit", None) == "exit"
        assert normalize_marker("exit", "") == "exit"

    def test_returns_reported_when_requested_is_also_internal_marker(self):
        """Defensive: if the agent genuinely asked for ``move exit``,
        the reported ``"exit"`` is correct — don't silently rewrite the
        request to itself or to a different marker."""
        assert normalize_marker("exit", "exit") == "exit"
        assert normalize_marker("player_spawn", "exit") == "player_spawn"
        assert normalize_marker("exit", "player_spawn") == "exit"

    def test_passes_through_empty_reported(self):
        assert normalize_marker("", "library") == ""
        assert normalize_marker("", None) == ""

    def test_marker_set_pinned(self):
        """Forces a deliberate review when adding a new marker — the
        navmesh has a habit of growing markers and any new one needs
        the same treatment."""
        assert MARKER_NORMALIZE == frozenset({"player_spawn", "exit"})


# ---------------------------------------------------------------------------
# mood_label / render_mood / render_activity — moved from daemon.py.
# ---------------------------------------------------------------------------


class TestMoodLabel:
    @pytest.mark.parametrize(
        "mood,expected",
        [
            (-50, "miserable"),
            (-30, "miserable"),
            (-29, "sad"),
            (-10, "sad"),
            (-9, "neutral"),
            (0, "neutral"),
            (10, "neutral"),
            (11, "happy"),
            (30, "happy"),
            (31, "elated"),
            (50, "elated"),
        ],
    )
    def test_boundaries(self, mood, expected):
        """Boundaries pinned: -50/-30/-10/+10/+30/+50.
        Mirrors the backend ``town_mood_engine.mood_label`` — drift here
        means the agent sees a different label than the broadcast event
        text."""
        assert mood_label(mood) == expected


class TestRenderMood:
    def test_numeric_renders_label_and_value(self):
        assert render_mood(10) == "neutral (10)"
        assert render_mood(-5) == "neutral (-5)"
        assert render_mood(35) == "elated (35)"

    def test_floats_truncate_to_int(self):
        assert render_mood(10.7) == "neutral (10)"
        assert render_mood(-29.9) == "sad (-29)"

    def test_string_passthrough(self):
        """Defensive: if the daemon already has a labeled string from
        the server (``"happy"``), pass it through."""
        assert render_mood("happy") == "happy"

    def test_falsy_renders_neutral(self):
        assert render_mood("") == "neutral"
        assert render_mood(None) == "neutral"


class TestRenderActivity:
    def test_blank_renders_idle(self):
        """Vera-finding R: the Activity field in TOWN_STATUS.md never
        renders blank."""
        assert render_activity("") == "idle"
        assert render_activity(None) == "idle"

    def test_passes_through_real_activity(self):
        assert render_activity("reading") == "reading"
        assert render_activity("talking") == "talking"


# ---------------------------------------------------------------------------
# render_nearby_text
# ---------------------------------------------------------------------------


class TestRenderNearbyText:
    def test_empty_renders_no_one(self):
        """The literal ``"no one"`` is what the daemon writes today;
        agents have learnt to read it."""
        assert render_nearby_text([]) == "no one"

    def test_uses_display_name(self):
        out = render_nearby_text([{"display_name": "Lucky", "activity": "reading"}])
        assert out == "Lucky (reading)"

    def test_falls_back_to_name_when_no_display_name(self):
        """The world_state shape sometimes uses ``name`` instead of
        ``display_name`` — keep the same fallback the daemon already
        had."""
        out = render_nearby_text([{"name": "Marbles", "activity": "idle"}])
        assert out == "Marbles (idle)"

    def test_question_mark_when_neither_name_field_present(self):
        out = render_nearby_text([{"activity": "idle"}])
        assert out == "? (idle)"

    def test_default_activity_idle(self):
        out = render_nearby_text([{"display_name": "Lucky"}])
        assert out == "Lucky (idle)"

    def test_multiple_entries_comma_separated(self):
        out = render_nearby_text(
            [
                {"display_name": "Lucky", "activity": "reading"},
                {"display_name": "Marbles", "activity": "talking"},
            ]
        )
        assert out == "Lucky (reading), Marbles (talking)"


# ---------------------------------------------------------------------------
# render_pending_messages
# ---------------------------------------------------------------------------


class TestRenderPendingMessages:
    def test_empty_renders_none(self):
        """The literal ``"None"`` is the daemon's historical default
        for an empty pending block."""
        assert render_pending_messages([]) == "None"

    def test_unknown_event_types_render_none(self):
        """The inline block dropped unknown events without raising;
        preserve that behaviour."""
        out = render_pending_messages(
            [{"event": "something_new", "from": "X", "text": "y"}]
        )
        assert out == "None"

    def test_conversation_invite_format(self):
        out = render_pending_messages(
            [
                {
                    "event": "conversation_invite",
                    "from": "Lucky",
                    "message": "Hi there",
                    "conv_id": "conv-1",
                }
            ]
        )
        assert out == '- **Lucky** wants to chat: "Hi there" (conv_id: conv-1)'

    def test_conversation_message_format(self):
        out = render_pending_messages(
            [
                {
                    "event": "conversation_message",
                    "from": "Marbles",
                    "text": "Hello",
                    "conv_id": "conv-2",
                    "turn": 3,
                }
            ]
        )
        assert out == '- **Marbles** says: "Hello" (conv_id: conv-2, turn 3)'

    def test_mixed_events_rendered_in_order(self):
        out = render_pending_messages(
            [
                {
                    "event": "conversation_invite",
                    "from": "Lucky",
                    "message": "Hi",
                    "conv_id": "c1",
                },
                {
                    "event": "conversation_message",
                    "from": "Marbles",
                    "text": "Yo",
                    "conv_id": "c2",
                    "turn": 1,
                },
            ]
        )
        lines = out.split("\n")
        assert len(lines) == 2
        assert "Lucky" in lines[0]
        assert "Marbles" in lines[1]

    def test_missing_fields_render_question_marks(self):
        """The daemon never crashes on missing fields — defaults to
        ``"?"`` for from/conv_id/turn so a partial event still produces
        a readable line."""
        out = render_pending_messages([{"event": "conversation_invite"}])
        assert "?" in out
        assert "conv_id: ?" in out


# ---------------------------------------------------------------------------
# render_static_status
# ---------------------------------------------------------------------------


class TestRenderStaticStatus:
    def test_connected_ready_exact_template(self):
        assert render_static_status(
            "connected_ready", location="plaza", activity="reading"
        ) == (
            "# GooseTown Status\n\n"
            "**Location:** plaza\n"
            "**Activity:** reading\n\n"
            "**Nearby:** no one\n\n"
            "You are now in GooseTown! Look around and decide what to do.\n\n"
            "## What do you want to do?\n\n"
            "- `goosetown act move plaza` — Go to the town plaza\n"
            "- `goosetown act move library` — Visit the library\n"
            "- `goosetown act move cafe` — Grab a coffee\n"
            "- `goosetown act move activity_center` — Activities and games\n"
            "- `goosetown act move residence` — Go to your apartment\n"
        )

    def test_connected_waiting_exact_template(self):
        assert render_static_status("connected_waiting") == (
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

    def test_sprite_ready_exact_template(self):
        assert render_static_status("sprite_ready") == (
            "# GooseTown Status\n\n"
            "**Your sprite is ready!** You now appear in town.\n\n"
            "## What do you want to do?\n\n"
            "- `goosetown act move plaza` — Go to the town plaza\n"
            "- `goosetown act move library` — Visit the library\n"
            "- `goosetown act move cafe` — Grab a coffee\n"
            "- `goosetown act move activity_center` — Activities and games\n"
            "- `goosetown act move residence` — Go to your apartment\n"
        )

    def test_context_changed_apartment_exact_template(self):
        assert render_static_status(
            "context_changed", context="apartment", location="desk"
        ) == (
            "# GooseTown Status\n\n"
            "**You entered your apartment.**\n\n"
            "**Location:** desk\n"
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

    def test_context_changed_town_exact_template(self):
        assert render_static_status(
            "context_changed", context="town", location="plaza"
        ) == (
            "# GooseTown Status\n\n"
            "**You returned to town.**\n\n"
            "**Location:** plaza\n"
            "**Activity:** idle\n\n"
            "**Where you can go:**\n"
            "- `goosetown act move plaza` — Town center with a fountain\n"
            "- `goosetown act move library` — Books and quiet study\n"
            "- `goosetown act move cafe` — Cozy coffee shop\n"
            "- `goosetown act move activity_center` — Games and group activities\n"
            "- `goosetown act move residence` — Go back to your apartment\n\n"
            "Use `goosetown status` for full status.\n"
        )

    def test_wake_exact_template(self):
        assert render_static_status("wake", message="Good morning.") == (
            "# GooseTown Status\n\n"
            "**Good morning.**\n\n"
            "## What do you want to do?\n\n"
            "You just woke up. Read your surroundings and decide:\n"
            "- `goosetown act move <location>` — Walk somewhere (plaza, library, cafe, activity_center, residence)\n"
            "- `goosetown act idle [activity]` — Do something at your location\n"
            "- `goosetown status` — See full status\n"
        )

    def test_unknown_kind_raises(self):
        with pytest.raises(ValueError, match="unknown static status kind"):
            render_static_status("missing")
