"""Unit tests for core.services.sprite_update_apply.apply_sprite_update.

Five scenarios exercised:
  1. Agent IS in game server + daemon connected → update_agent_sprites awaited,
     connect_agent NOT called; restore_phase_from_db called with row's phase.
  2. Agent NOT in game server but daemon connected → connect_agent awaited with
     versioned sprite URL/animations.
  3. Agent missing in DynamoDB → no gs calls, no daemon send, no raise.
  4. kind="animations_updated" + daemon connected → _send_to_daemon called with
     sprite_ready event; kind="static_ready" → NOT called.
  5. gs push raising → swallowed (logged), sprite_ready still sent for
     animations_updated.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Shared builder helpers
# ---------------------------------------------------------------------------


def _make_agent_row(
    agent_id: str = "agent-uuid-1",
    onboarding_phase: str = "alive",
    sprite_url: str = "https://cdn.example/sprites/a1/walk.png",
    sprite_updated_at: int = 1_000_000,
    sprite_animations: dict | None = None,
    display_name: str = "Tester",
    current_location: str = "plaza",
) -> dict:
    return {
        "id": agent_id,
        "onboardingPhase": onboarding_phase,
        "spriteUrl": sprite_url,
        "spriteUpdatedAt": sprite_updated_at,
        "spriteAnimations": sprite_animations or {},
        "displayName": display_name,
        "currentLocation": current_location,
    }


def _make_conn(
    agent_id: str = "agent-uuid-1",
    user_id: str = "user-1",
    location_context: str = "town",
) -> MagicMock:
    conn = MagicMock()
    conn.agent_id = agent_id
    conn.user_id = user_id
    conn.location_context = location_context
    return conn


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------


class TestApplySpriteUpdate:
    """apply_sprite_update — backend-local effects of a sprite change."""

    # ------------------------------------------------------------------
    # Case 1: agent in game server + daemon connected
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_in_gs_calls_update_not_connect(self):
        """update_agent_sprites is awaited; connect_agent is NOT called."""
        agent_id = "agent-uuid-1"
        row = _make_agent_row(agent_id=agent_id, onboarding_phase="alive")
        conn = _make_conn(agent_id=agent_id)

        mock_service = MagicMock()
        mock_service.get_town_agent_by_id.return_value = row

        mock_gs = MagicMock()
        mock_gs._agent_connections = {agent_id: MagicMock()}  # agent IS connected
        mock_gs.update_agent_sprites = AsyncMock()
        mock_gs.connect_agent = AsyncMock()

        mock_ws_mgr = MagicMock()
        mock_ws_mgr.get_by_agent_id.return_value = conn

        with (
            patch("core.services.sprite_update_apply.TownService", return_value=mock_service),
            patch("core.services.sprite_update_apply.get_game_server_client", return_value=mock_gs),
            patch("core.services.sprite_update_apply.get_town_agent_ws_manager", return_value=mock_ws_mgr),
            patch("core.services.sprite_update_apply.restore_phase_from_db") as mock_restore,
            patch("core.services.sprite_update_apply._send_to_daemon"),
        ):
            from core.services.sprite_update_apply import apply_sprite_update

            await apply_sprite_update(agent_id, "static_ready")

        mock_gs.update_agent_sprites.assert_awaited_once()
        mock_gs.connect_agent.assert_not_awaited()
        mock_restore.assert_called_once_with(agent_id, "alive")

    # ------------------------------------------------------------------
    # Case 2: agent NOT in game server but daemon connected
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_not_in_gs_calls_connect_with_versioned_urls(self):
        """connect_agent is awaited with the versioned sprite URL/animations."""
        agent_id = "agent-uuid-2"
        raw_url = "https://cdn.example/sprites/a2/walk.png"
        raw_animations = {"walk": {"url": "https://cdn.example/a2/walk-anim.png", "frameCount": 6, "fps": 8}}
        row = _make_agent_row(
            agent_id=agent_id,
            sprite_url=raw_url,
            sprite_updated_at=9999,
            sprite_animations=raw_animations,
            current_location="library",
        )
        conn = _make_conn(agent_id=agent_id, user_id="user-2", location_context="town")

        mock_service = MagicMock()
        mock_service.get_town_agent_by_id.return_value = row

        mock_gs = MagicMock()
        mock_gs._agent_connections = {}  # agent NOT in gs
        mock_gs.update_agent_sprites = AsyncMock()
        mock_gs.connect_agent = AsyncMock()

        mock_ws_mgr = MagicMock()
        mock_ws_mgr.get_by_agent_id.return_value = conn

        with (
            patch("core.services.sprite_update_apply.TownService", return_value=mock_service),
            patch("core.services.sprite_update_apply.get_game_server_client", return_value=mock_gs),
            patch("core.services.sprite_update_apply.get_town_agent_ws_manager", return_value=mock_ws_mgr),
            patch("core.services.sprite_update_apply.restore_phase_from_db"),
            patch("core.services.sprite_update_apply._send_to_daemon"),
        ):
            from core.services.sprite_update_apply import apply_sprite_update

            await apply_sprite_update(agent_id, "static_ready")

        mock_gs.connect_agent.assert_awaited_once()
        call_kwargs = mock_gs.connect_agent.call_args.kwargs

        # URLs should be versioned (?v=9999)
        assert "?v=9999" in call_kwargs["sprite_url"], (
            f"Expected versioned sprite_url, got: {call_kwargs['sprite_url']}"
        )
        walk_anim = call_kwargs["sprite_animations"].get("walk", {})
        assert "?v=9999" in walk_anim.get("url", ""), f"Expected versioned animation url, got: {walk_anim}"

        # connect_agent receives correct location / context / user_id
        assert call_kwargs["location"] == "library"
        assert call_kwargs["context"] == "town"
        assert call_kwargs["user_id"] == "user-2"

        # update_agent_sprites must NOT have been called
        mock_gs.update_agent_sprites.assert_not_awaited()

    # ------------------------------------------------------------------
    # Case 3: agent missing in DynamoDB
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_missing_agent_no_calls_no_raise(self):
        """Missing DDB row → early return, no gs or daemon calls."""
        mock_service = MagicMock()
        mock_service.get_town_agent_by_id.return_value = None

        mock_gs = MagicMock()
        mock_gs.update_agent_sprites = AsyncMock()
        mock_gs.connect_agent = AsyncMock()

        mock_ws_mgr = MagicMock()

        with (
            patch("core.services.sprite_update_apply.TownService", return_value=mock_service),
            patch("core.services.sprite_update_apply.get_game_server_client", return_value=mock_gs),
            patch("core.services.sprite_update_apply.get_town_agent_ws_manager", return_value=mock_ws_mgr),
            patch("core.services.sprite_update_apply.restore_phase_from_db") as mock_restore,
            patch("core.services.sprite_update_apply._send_to_daemon") as mock_send,
        ):
            from core.services.sprite_update_apply import apply_sprite_update

            # Must not raise
            await apply_sprite_update("ghost-agent-id", "animations_updated")

        mock_gs.update_agent_sprites.assert_not_awaited()
        mock_gs.connect_agent.assert_not_awaited()
        mock_restore.assert_not_called()
        mock_send.assert_not_called()

    # ------------------------------------------------------------------
    # Case 4: _send_to_daemon only called for animations_updated
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_send_to_daemon_only_for_animations_updated(self):
        """kind=animations_updated + daemon connected → sprite_ready sent."""
        agent_id = "agent-uuid-3"
        row = _make_agent_row(agent_id=agent_id, sprite_url="https://cdn.example/a3/walk.png")
        conn = _make_conn(agent_id=agent_id)

        mock_service = MagicMock()
        mock_service.get_town_agent_by_id.return_value = row

        mock_gs = MagicMock()
        mock_gs._agent_connections = {agent_id: MagicMock()}
        mock_gs.update_agent_sprites = AsyncMock()
        mock_gs.connect_agent = AsyncMock()

        mock_ws_mgr = MagicMock()
        mock_ws_mgr.get_by_agent_id.return_value = conn

        captured_calls: list[dict] = []

        def fake_send(aid, payload):
            captured_calls.append({"agent_id": aid, "payload": payload})
            return True

        with (
            patch("core.services.sprite_update_apply.TownService", return_value=mock_service),
            patch("core.services.sprite_update_apply.get_game_server_client", return_value=mock_gs),
            patch("core.services.sprite_update_apply.get_town_agent_ws_manager", return_value=mock_ws_mgr),
            patch("core.services.sprite_update_apply.restore_phase_from_db"),
            patch("core.services.sprite_update_apply._send_to_daemon", side_effect=fake_send),
        ):
            from core.services.sprite_update_apply import apply_sprite_update

            # animations_updated → daemon send expected
            await apply_sprite_update(agent_id, "animations_updated")

        assert len(captured_calls) == 1
        assert captured_calls[0]["payload"]["event"] == "sprite_ready"
        assert captured_calls[0]["agent_id"] == agent_id
        captured_calls.clear()

        with (
            patch("core.services.sprite_update_apply.TownService", return_value=mock_service),
            patch("core.services.sprite_update_apply.get_game_server_client", return_value=mock_gs),
            patch("core.services.sprite_update_apply.get_town_agent_ws_manager", return_value=mock_ws_mgr),
            patch("core.services.sprite_update_apply.restore_phase_from_db"),
            patch("core.services.sprite_update_apply._send_to_daemon", side_effect=fake_send),
        ):
            from core.services.sprite_update_apply import apply_sprite_update

            # static_ready → daemon send NOT expected
            await apply_sprite_update(agent_id, "static_ready")

        assert len(captured_calls) == 0, f"Expected no _send_to_daemon call for static_ready, got: {captured_calls}"

    # ------------------------------------------------------------------
    # Case 5: gs push raising → swallowed; daemon send still fires for animations_updated
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_gs_push_exception_swallowed_daemon_send_still_fires(self):
        """gs push raising is logged and swallowed; sprite_ready still sent."""
        agent_id = "agent-uuid-4"
        row = _make_agent_row(agent_id=agent_id)
        conn = _make_conn(agent_id=agent_id)

        mock_service = MagicMock()
        mock_service.get_town_agent_by_id.return_value = row

        mock_gs = MagicMock()
        mock_gs._agent_connections = {agent_id: MagicMock()}
        mock_gs.update_agent_sprites = AsyncMock(side_effect=RuntimeError("GS offline"))
        mock_gs.connect_agent = AsyncMock()

        mock_ws_mgr = MagicMock()
        mock_ws_mgr.get_by_agent_id.return_value = conn

        send_calls: list = []

        def fake_send(aid, payload):
            send_calls.append(payload)
            return True

        with (
            patch("core.services.sprite_update_apply.TownService", return_value=mock_service),
            patch("core.services.sprite_update_apply.get_game_server_client", return_value=mock_gs),
            patch("core.services.sprite_update_apply.get_town_agent_ws_manager", return_value=mock_ws_mgr),
            patch("core.services.sprite_update_apply.restore_phase_from_db"),
            patch("core.services.sprite_update_apply._send_to_daemon", side_effect=fake_send),
        ):
            from core.services.sprite_update_apply import apply_sprite_update

            # Must not raise despite gs failure
            await apply_sprite_update(agent_id, "animations_updated")

        # Exception swallowed → no raise
        # Daemon send still happened
        assert len(send_calls) == 1
        assert send_calls[0]["event"] == "sprite_ready"
