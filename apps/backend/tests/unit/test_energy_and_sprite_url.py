"""Unit tests for recently added energy and sprite-URL logic in routers/town.py
and core/services/town_service.py.

Covers:
    - ``versioned_sprite_url`` helper (pure function).
    - ``_apply_event_with_sleep_guard`` helper — event applies normally when
      energy stays positive; applies sleep event, marks agent sleeping, and
      closes the WS when energy would drop to zero.
    - Energy soft-gate in ``town_agent_act`` message handling — rejects non-idle
      actions at energy=0, bumps rejection counter, force-closes the WS after
      10 rejections, and also applies sleep-reset at the force-close.

The goal is coverage for the logic we've been touching this week. We mock the
DynamoDB service, DaemonMailbox, and WS manager at their source modules so the
routers/town.py local imports resolve to the mocks.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_agent(
    agent_id: str = "agent-1",
    energy: int = 100,
    mood: int = 0,
    traits: str = "extrovert",
    sprite_url: str = "https://cdn.example/sprites/agent-1/walk.png",
    sprite_updated_at: int | None = 1776400000,
) -> dict:
    return {
        "id": agent_id,
        "energy": energy,
        "mood": mood,
        "traits": traits,
        "spriteUrl": sprite_url,
        "spriteUpdatedAt": sprite_updated_at,
    }


def _make_conn(agent_id: str = "agent-1") -> SimpleNamespace:
    return SimpleNamespace(
        connection_id="conn-abc",
        user_id="user-1",
        agent_name="lucky",
        agent_id=agent_id,
        instance_id="inst-1",
        location_context="town",
        exhausted_rejections=0,
    )


# ---------------------------------------------------------------------------
# versioned_sprite_url — pure function
# ---------------------------------------------------------------------------


class TestVersionedSpriteUrl:
    def test_appends_version_from_sprite_updated_at(self):
        from core.services.town_service import versioned_sprite_url

        out = versioned_sprite_url(
            "https://cdn.example/sprites/a/walk.png",
            {"spriteUpdatedAt": 1776400000},
        )
        assert out == "https://cdn.example/sprites/a/walk.png?v=1776400000"

    def test_empty_url_returns_empty_string(self):
        from core.services.town_service import versioned_sprite_url

        assert versioned_sprite_url("", {"spriteUpdatedAt": 123}) == ""
        assert versioned_sprite_url(None, {"spriteUpdatedAt": 123}) == ""

    def test_no_agent_returns_unchanged_url(self):
        from core.services.town_service import versioned_sprite_url

        url = "https://cdn.example/sprites/a/walk.png"
        assert versioned_sprite_url(url, None) == url

    def test_missing_sprite_updated_at_returns_unchanged_url(self):
        from core.services.town_service import versioned_sprite_url

        url = "https://cdn.example/sprites/a/walk.png"
        # Agent has other fields but no spriteUpdatedAt yet (pre-backfill state).
        assert versioned_sprite_url(url, {"id": "a", "spriteUrl": url}) == url

    def test_handles_url_with_existing_query_param(self):
        from core.services.town_service import versioned_sprite_url

        out = versioned_sprite_url(
            "https://cdn.example/sprites/a/walk.png?foo=bar",
            {"spriteUpdatedAt": 42},
        )
        assert out == "https://cdn.example/sprites/a/walk.png?foo=bar&v=42"


# ---------------------------------------------------------------------------
# _version_animation_urls — cache-bust the per-animation URLs at game-server
# connect, so regenerated sheets (e.g. re-stripped talking.png) show in town.
# ---------------------------------------------------------------------------


class TestVersionAnimationUrls:
    def test_versions_every_entry_url(self):
        from core.services.agent_connect_service import _version_animation_urls

        anims = {
            "walk": {"url": "https://cdn/x/walk.png", "frameCount": 6, "fps": 8},
            "talking": {"url": "https://cdn/x/talking.png", "frameCount": 9, "fps": 8},
        }
        out = _version_animation_urls(anims, {"spriteUpdatedAt": 99})
        assert out["walk"]["url"] == "https://cdn/x/walk.png?v=99"
        assert out["talking"]["url"] == "https://cdn/x/talking.png?v=99"
        # Other metadata preserved.
        assert out["talking"]["frameCount"] == 9 and out["talking"]["fps"] == 8

    def test_empty_dict_passthrough(self):
        from core.services.agent_connect_service import _version_animation_urls

        assert _version_animation_urls({}, {"spriteUpdatedAt": 99}) == {}

    def test_entries_without_url_untouched(self):
        from core.services.agent_connect_service import _version_animation_urls

        anims = {"weird": {"frameCount": 1}, "alsoweird": "not-a-dict"}
        out = _version_animation_urls(anims, {"spriteUpdatedAt": 99})
        assert out == anims

    def test_no_version_token_leaves_urls_bare(self):
        from core.services.agent_connect_service import _version_animation_urls

        anims = {"walk": {"url": "https://cdn/x/walk.png"}}
        out = _version_animation_urls(anims, {"id": "x"})  # no spriteUpdatedAt
        assert out["walk"]["url"] == "https://cdn/x/walk.png"


# ---------------------------------------------------------------------------
# update_agent_state — auto-bump spriteUpdatedAt when sprite_url is written
# ---------------------------------------------------------------------------


class TestSpriteUpdatedAtAutoBump:
    def test_writing_sprite_url_also_sets_sprite_updated_at(self, dynamodb_tables, monkeypatch):
        """Setting sprite_url via update_agent_state should auto-populate
        spriteUpdatedAt so downstream versioned_sprite_url has a token to use.
        """
        from core.services.town_service import TownService

        # Seed a bare agent record
        agents_table = dynamodb_tables.Table("goosetown-agents")
        agents_table.put_item(
            Item={
                "id": "agent-1",
                "userId": "user-1",
                "agentName": "lucky",
                "instanceId": "inst-1",
                "isActive": "true",
                "joinedAt": "2026-04-17T00:00:00Z",
            }
        )

        # Freeze time for deterministic comparison
        monkeypatch.setattr("time.time", lambda: 1_700_000_000.0)

        service = TownService()
        service.update_agent_state("agent-1", sprite_url="https://cdn.example/sprites/agent-1/walk.png")

        refreshed = service.get_town_agent_by_id("agent-1")
        assert refreshed["spriteUrl"] == "https://cdn.example/sprites/agent-1/walk.png"
        assert refreshed["spriteUpdatedAt"] == 1_700_000_000

    def test_explicit_sprite_updated_at_is_respected(self, dynamodb_tables, monkeypatch):
        from core.services.town_service import TownService

        agents_table = dynamodb_tables.Table("goosetown-agents")
        agents_table.put_item(
            Item={
                "id": "agent-2",
                "userId": "user-1",
                "agentName": "ross",
                "instanceId": "inst-1",
                "isActive": "true",
                "joinedAt": "2026-04-17T00:00:00Z",
            }
        )

        monkeypatch.setattr("time.time", lambda: 9_999_999_999.0)

        service = TownService()
        # Caller passes an explicit value; auto-bump must NOT overwrite it.
        service.update_agent_state(
            "agent-2",
            sprite_url="https://cdn.example/sprites/agent-2/walk.png",
            sprite_updated_at=1_234_567_890,
        )

        refreshed = service.get_town_agent_by_id("agent-2")
        assert refreshed["spriteUpdatedAt"] == 1_234_567_890


# ---------------------------------------------------------------------------
# _apply_event_with_sleep_guard
# ---------------------------------------------------------------------------


class TestApplyEventWithSleepGuard:
    @pytest.fixture
    def mocks(self):
        """Patch the helper's external dependencies at their source modules."""
        # `_apply_event_with_sleep_guard` now lives in core.services.daemon_bridge
        # (sub-project #3 fourth slice, 2026-04-22); patch where the name is
        # looked up, not the routers.town re-export.
        patches = {
            "mailbox_factory": patch("core.services.daemon_bridge.get_daemon_mailbox"),
            "ws_mgr": patch("core.services.daemon_bridge.get_town_agent_ws_manager"),
        }
        started = {name: p.start() for name, p in patches.items()}
        yield started
        for p in patches.values():
            p.stop()

    def test_normal_event_persists_and_does_not_sleep(self, mocks):
        """Event that keeps energy > 0 persists new values, does not trigger
        sleep, and does not close the WS."""
        from routers.town import _apply_event_with_sleep_guard

        service = MagicMock()
        agent = _make_agent(energy=80, mood=0, traits="extrovert")
        mocks["ws_mgr"].return_value.get_by_agent_id.return_value = None

        new_e, new_m, slept = _apply_event_with_sleep_guard(service, "agent-1", agent, "conversation_completed")

        assert slept is False
        # conversation_completed for extrovert: energy -2, mood +8 per mood_engine
        assert 60 <= new_e < 80
        service.update_agent_mood_energy.assert_called_once_with("agent-1", new_m, new_e)
        service.update_agent_activity.assert_not_called()

    def test_event_that_exhausts_triggers_sleep_and_closes_ws(self, mocks):
        """When an event would drop energy to 0, the helper must apply the
        sleep event (refill), mark the agent sleeping, and close the WS."""
        from routers.town import _apply_event_with_sleep_guard

        service = MagicMock()
        agent = _make_agent(energy=1, mood=0, traits="extrovert")

        # Agent is connected — helper must send forced_sleep + close
        conn = _make_conn()
        mocks["ws_mgr"].return_value.get_by_agent_id.return_value = conn
        mailbox = MagicMock()
        mocks["mailbox_factory"].return_value = mailbox

        new_e, new_m, slept = _apply_event_with_sleep_guard(service, "agent-1", agent, "conversation_completed")

        assert slept is True
        assert new_e == 100  # sleep refills
        # DB: energy written + activity marked sleeping
        service.update_agent_mood_energy.assert_called_once()
        call_args = service.update_agent_mood_energy.call_args
        assert call_args[0][0] == "agent-1"
        assert call_args[0][2] == 100  # energy arg is 100
        service.update_agent_activity.assert_called_once_with("agent-1", "sleeping", None)
        # WS: forced_sleep sent, then disconnect
        mailbox.notify_connection.assert_called_once()
        sent_payload = mailbox.notify_connection.call_args[0][1]
        assert sent_payload["event"] == "forced_sleep"
        assert sent_payload["reason"] == "exhausted"
        mailbox.disconnect.assert_called_once_with(conn.connection_id)

    def test_exhausted_with_no_ws_connection_still_applies_sleep(self, mocks):
        """If the agent has no live WS when exhaustion triggers (edge case
        when they've already disconnected), the DB updates still land."""
        from routers.town import _apply_event_with_sleep_guard

        service = MagicMock()
        agent = _make_agent(energy=1, mood=0, traits="extrovert")
        mocks["ws_mgr"].return_value.get_by_agent_id.return_value = None

        new_e, _new_m, slept = _apply_event_with_sleep_guard(service, "agent-1", agent, "conversation_completed")

        assert slept is True
        assert new_e == 100
        service.update_agent_mood_energy.assert_called_once()
        service.update_agent_activity.assert_called_once_with("agent-1", "sleeping", None)


# ---------------------------------------------------------------------------
# Energy soft-gate in town_agent_act handler
# ---------------------------------------------------------------------------


class TestEnergySoftGate:
    """The soft-gate rejects non-idle actions from an agent at energy=0 and
    force-closes the WS after 10 rejections.
    """

    @pytest.fixture
    def setup(self):
        ws_manager = MagicMock()
        agent_conn = _make_conn()
        ws_manager.get_by_connection.return_value = agent_conn

        service = MagicMock()
        service.get_town_agent_by_id.return_value = _make_agent(energy=0)

        mailbox = MagicMock()

        with (
            patch("routers.town.get_town_agent_ws_manager", return_value=ws_manager),
            patch("routers.town.TownService", return_value=service),
            patch("core.services.daemon_mailbox.get_daemon_mailbox", return_value=mailbox),
        ):
            yield SimpleNamespace(
                ws_manager=ws_manager,
                agent_conn=agent_conn,
                service=service,
                mailbox=mailbox,
            )

    @pytest.mark.asyncio
    async def test_exhausted_agent_act_is_rejected(self, setup):
        """Any non-idle action from an energy=0 agent returns 200/rejected and
        sends an 'exhausted' event over the WS."""
        from fastapi import Request

        from routers.town import WsMessageRequest, handle_ws_message

        request = WsMessageRequest(
            connectionId="conn-abc",
            principalId="user-1",
            body={"type": "town_agent_act", "action": "say", "message": "hi"},
        )
        # raw_request only matters for secret auth, which is skipped in dev
        # when WS_LAMBDA_SECRET is unset.
        raw = MagicMock(spec=Request)
        raw.headers = {}

        result = await handle_ws_message(request, raw)

        assert result == {"statusCode": 200, "rejected": "exhausted"}
        assert setup.agent_conn.exhausted_rejections == 1

        # notify_connection called once with an exhausted event
        assert setup.mailbox.notify_connection.call_count == 1
        payload = setup.mailbox.notify_connection.call_args[0][1]
        assert payload["event"] == "exhausted"
        assert payload["action"] == "say"
        assert payload["rejections"] == 1

        # Under 10 rejections: no force-close yet
        setup.mailbox.disconnect.assert_not_called()

    @pytest.mark.asyncio
    async def test_tenth_rejection_force_closes_and_applies_sleep(self, setup):
        """On the 10th consecutive rejection the WS is force-closed and the
        agent's energy is reset via sleep so the reconnect doesn't hit the
        same loop."""
        from fastapi import Request

        from routers.town import WsMessageRequest, handle_ws_message

        # Simulate 9 prior rejections already on record
        setup.agent_conn.exhausted_rejections = 9

        request = WsMessageRequest(
            connectionId="conn-abc",
            principalId="user-1",
            body={"type": "town_agent_act", "action": "chat", "target": "x"},
        )
        raw = MagicMock(spec=Request)
        raw.headers = {}

        result = await handle_ws_message(request, raw)

        assert result == {"statusCode": 200, "rejected": "exhausted"}
        assert setup.agent_conn.exhausted_rejections == 10

        # Force-close was invoked once
        setup.mailbox.disconnect.assert_called_once_with("conn-abc")
        # Sleep event was applied so the next reconnect is clean
        setup.service.update_agent_mood_energy.assert_called_once()
        # Last arg of that call is new_energy — should be 100
        new_energy = setup.service.update_agent_mood_energy.call_args[0][2]
        assert new_energy == 100

    @pytest.mark.asyncio
    async def test_idle_action_is_not_gated(self, setup):
        """'idle' is intentionally exempt — agents must be able to rest even
        when exhausted without triggering a rejection."""
        from fastapi import Request

        from routers.town import WsMessageRequest, handle_ws_message

        # Prevent further pipeline execution past the gate by giving an idle action.
        request = WsMessageRequest(
            connectionId="conn-abc",
            principalId="user-1",
            body={"type": "town_agent_act", "action": "idle"},
        )
        raw = MagicMock(spec=Request)
        raw.headers = {}

        # idle actions flow through to the bottom of the handler and eventually
        # return statusCode:200. We just need to confirm the gate didn't fire.
        # Patch get_game_server_client so the idle branch has something to call.
        with patch("routers.town.get_game_server_client", return_value=MagicMock()):
            result = await handle_ws_message(request, raw)

        # Not rejected
        assert "rejected" not in result
        assert setup.agent_conn.exhausted_rejections == 0
        setup.mailbox.notify_connection.assert_not_called()
        setup.mailbox.disconnect.assert_not_called()


# ---------------------------------------------------------------------------
# Ensure configparser-style asyncio mark is recognized in this module
# ---------------------------------------------------------------------------

_ = asyncio  # used by marks indirectly
