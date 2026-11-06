"""Tests for the provider onboarding WS verbs (Wave 2, Task 8).

Two layers:

1. **Dispatch** — ``handle_ws_message`` routes ``provider_register`` /
   ``provider_off`` ``town_agent_act`` bodies to the
   ``exchange_service.register_provider`` / ``unregister_provider`` thin
   wrappers with the right kwargs, returning ``{statusCode: 200, body: ...}``
   (mirrors ``test_exchange_actions.py``).

2. **Wrappers** — ``register_provider`` / ``unregister_provider`` call
   ``ProviderDirectory().register`` / ``.unregister`` and notify the caller via
   the ``exchange_ok`` mailbox echo, against moto-backed DynamoDB (mirrors
   ``test_exchange_matcher.py``).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services import exchange_service as ex

# ---------------------------------------------------------------------------
# Dispatch tests
# ---------------------------------------------------------------------------

_SENTINEL_RESULT = {"status": "ok", "is_provider": True}


def _make_conn(agent_id: str = "alice-123", user_id: str = "user-1") -> SimpleNamespace:
    return SimpleNamespace(
        agent_id=agent_id,
        runtime_session_claims=None,
        user_id=user_id,
        agent_name="alice",
        location_context="town",
        exhausted_rejections=0,
        leaving_cleanly=False,
    )


@pytest.fixture
def handler_setup():
    agent_conn = _make_conn()
    ws_manager = MagicMock()
    ws_manager.get_by_connection.return_value = agent_conn

    mock_exchange = MagicMock()
    mock_exchange.register_provider = AsyncMock(return_value=_SENTINEL_RESULT)
    mock_exchange.unregister_provider = AsyncMock(return_value={"status": "ok", "is_provider": False})

    with (
        patch("routers.town.get_town_agent_ws_manager", return_value=ws_manager),
        patch(
            "routers.town.TownService",
            return_value=MagicMock(
                **{"get_town_agent_by_id.return_value": {"energy": 80, "mood": 5}},
            ),
        ),
        patch("routers.town.exchange_service", mock_exchange),
        patch("routers.town.get_game_server_client", return_value=MagicMock()),
    ):
        yield SimpleNamespace(
            ws_manager=ws_manager,
            agent_conn=agent_conn,
            mock_exchange=mock_exchange,
        )


@pytest.mark.asyncio
async def test_provider_register_dispatches_to_service(handler_setup):
    from fastapi import Request

    from routers.town import WsMessageRequest, handle_ws_message

    request = WsMessageRequest(
        connectionId="conn-abc",
        principalId="user-1",
        body={
            "type": "town_agent_act",
            "action": "provider_register",
            "skills": ["haiku", "research"],
            "categories": ["writing", "research"],
            "bio": "I write things.",
            "min_price": 5,
        },
    )
    raw = MagicMock(spec=Request)
    raw.headers = {}

    result = await handle_ws_message(request, raw)

    assert result == {"statusCode": 200, "body": _SENTINEL_RESULT}
    handler_setup.mock_exchange.register_provider.assert_called_once_with(
        handler_setup.agent_conn,
        skills=["haiku", "research"],
        categories=["writing", "research"],
        bio="I write things.",
        min_price=5,
    )


@pytest.mark.asyncio
async def test_provider_register_defaults_min_price_zero(handler_setup):
    """min_price absent → coerced to 0 (matches the CLI's optional flag)."""
    from fastapi import Request

    from routers.town import WsMessageRequest, handle_ws_message

    request = WsMessageRequest(
        connectionId="conn-abc",
        principalId="user-1",
        body={
            "type": "town_agent_act",
            "action": "provider_register",
            "skills": ["haiku"],
            "categories": ["writing"],
            "bio": "",
        },
    )
    raw = MagicMock(spec=Request)
    raw.headers = {}

    result = await handle_ws_message(request, raw)

    assert result == {"statusCode": 200, "body": _SENTINEL_RESULT}
    handler_setup.mock_exchange.register_provider.assert_called_once_with(
        handler_setup.agent_conn,
        skills=["haiku"],
        categories=["writing"],
        bio="",
        min_price=0,
    )


@pytest.mark.asyncio
async def test_provider_off_dispatches_to_service(handler_setup):
    from fastapi import Request

    from routers.town import WsMessageRequest, handle_ws_message

    request = WsMessageRequest(
        connectionId="conn-abc",
        principalId="user-1",
        body={
            "type": "town_agent_act",
            "action": "provider_off",
        },
    )
    raw = MagicMock(spec=Request)
    raw.headers = {}

    result = await handle_ws_message(request, raw)

    assert result == {"statusCode": 200, "body": {"status": "ok", "is_provider": False}}
    handler_setup.mock_exchange.unregister_provider.assert_called_once_with(
        handler_setup.agent_conn,
    )


# ---------------------------------------------------------------------------
# ws_models validation
# ---------------------------------------------------------------------------


class TestWsModelsValidation:
    def test_provider_register_validates(self):
        from core.ws_models import TownAgentAct

        m = TownAgentAct.model_validate(
            {
                "type": "town_agent_act",
                "action": "provider_register",
                "skills": ["a", "b"],
                "categories": ["writing"],
                "bio": "hi",
                "min_price": 3,
            }
        )
        assert m.action == "provider_register"
        assert m.skills == ["a", "b"]
        assert m.categories == ["writing"]
        assert m.bio == "hi"
        assert m.min_price == 3

    def test_provider_register_requires_categories(self):
        import pydantic

        from core.ws_models import TownAgentAct

        with pytest.raises(pydantic.ValidationError):
            TownAgentAct.model_validate(
                {
                    "type": "town_agent_act",
                    "action": "provider_register",
                    "skills": ["a"],
                    "categories": [],
                    "bio": "hi",
                }
            )

    def test_provider_off_needs_no_fields(self):
        from core.ws_models import TownAgentAct

        m = TownAgentAct.model_validate({"type": "town_agent_act", "action": "provider_off"})
        assert m.action == "provider_off"

    def test_skills_only_valid_with_provider_register(self):
        import pydantic

        from core.ws_models import TownAgentAct

        with pytest.raises(pydantic.ValidationError):
            TownAgentAct.model_validate({"type": "town_agent_act", "action": "idle", "skills": ["a"]})


# ---------------------------------------------------------------------------
# Wrapper tests (moto-backed)
# ---------------------------------------------------------------------------


def _seed_agent(db, agent_id: str, name: str) -> None:
    db.Table("goosetown-agents").put_item(
        Item={
            "id": agent_id,
            "agentName": name,
            "displayName": name,
            "isActive": "true",
            "currentLocation": "bank",
            "energy": 100,
            "mood": 0,
            "traits": "",
        }
    )


def _conn(agent_id: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(agent_id=agent_id, agent_name=name, location_context="town", connection_id="c1")


def _agent(db, agent_id: str) -> dict:
    return db.Table("goosetown-agents").get_item(Key={"id": agent_id})["Item"]


@pytest.fixture
def mailbox():
    mb = MagicMock()
    with patch("core.services.exchange_service.get_daemon_mailbox", return_value=mb):
        yield mb


class TestRegisterProviderWrapper:
    @pytest.mark.asyncio
    async def test_register_writes_profile_and_subscriptions(self, dynamodb_tables, mailbox):
        _seed_agent(dynamodb_tables, "p1", "pat")
        cc = _conn("p1", "pat")

        result = await ex.register_provider(
            cc,
            skills=["haiku", "research"],
            categories=["writing", "research"],
            bio="I write things.",
            min_price=5,
        )

        assert result["status"] == "ok"
        assert result["is_provider"] is True
        assert result["categories"] == ["writing", "research"]
        # Profile attrs landed on the agents item.
        row = _agent(dynamodb_tables, "p1")
        assert row["isProvider"] is True
        assert row["providerSkills"] == ["haiku", "research"]
        assert row["providerCategories"] == ["writing", "research"]
        assert int(row["minPrice"]) == 5
        # Subscription projection rows were written (discoverable in directory).
        from core.services.provider_directory import ProviderDirectory

        ids = ProviderDirectory().subscribers_for("writing")
        assert "p1" in ids

    @pytest.mark.asyncio
    async def test_register_echoes_exchange_ok(self, dynamodb_tables, mailbox):
        _seed_agent(dynamodb_tables, "p1", "pat")
        cc = _conn("p1", "pat")

        await ex.register_provider(cc, skills=[], categories=["writing"], bio="")

        echo = next(c.args[1] for c in mailbox.notify.call_args_list if c.args[1].get("verb") == "provider_register")
        assert echo["event"] == "exchange_ok"
        assert echo["verb"] == "provider_register"

    @pytest.mark.asyncio
    async def test_all_invalid_categories_rejected(self, dynamodb_tables, mailbox):
        # The directory raises ValueError; the wrapper surfaces a clean rejection
        # instead of a silent broken provider (codex P2).
        _seed_agent(dynamodb_tables, "p1", "pat")
        cc = _conn("p1", "pat")

        result = await ex.register_provider(cc, skills=["x"], categories=["has space"], bio="")

        assert result["status"] == "rejected"
        assert result["reason"] == "no_valid_categories"
        # Not marked a provider, no subscription rows.
        assert "isProvider" not in _agent(dynamodb_tables, "p1")
        from core.services.provider_directory import ProviderDirectory

        assert ProviderDirectory().subscribers_for("writing") == []

    @pytest.mark.asyncio
    async def test_update_replaces_categories(self, dynamodb_tables, mailbox):
        _seed_agent(dynamodb_tables, "p1", "pat")
        cc = _conn("p1", "pat")
        from core.services.provider_directory import ProviderDirectory

        await ex.register_provider(cc, skills=[], categories=["writing"], bio="")
        assert ProviderDirectory().subscribers_for("writing") == ["p1"]

        # Re-register (update) with a different category set replaces, no stale rows.
        await ex.register_provider(cc, skills=[], categories=["research"], bio="")
        assert ProviderDirectory().subscribers_for("writing") == []
        assert ProviderDirectory().subscribers_for("research") == ["p1"]


class TestUnregisterProviderWrapper:
    @pytest.mark.asyncio
    async def test_unregister_clears_profile_and_rows(self, dynamodb_tables, mailbox):
        _seed_agent(dynamodb_tables, "p1", "pat")
        cc = _conn("p1", "pat")
        from core.services.provider_directory import ProviderDirectory

        await ex.register_provider(cc, skills=[], categories=["writing"], bio="")
        assert ProviderDirectory().subscribers_for("writing") == ["p1"]

        result = await ex.unregister_provider(cc)
        assert result["status"] == "ok"
        assert result["is_provider"] is False
        assert _agent(dynamodb_tables, "p1")["isProvider"] is False
        assert ProviderDirectory().subscribers_for("writing") == []

    @pytest.mark.asyncio
    async def test_unregister_echoes_exchange_ok(self, dynamodb_tables, mailbox):
        _seed_agent(dynamodb_tables, "p1", "pat")
        cc = _conn("p1", "pat")

        await ex.unregister_provider(cc)

        echo = next(c.args[1] for c in mailbox.notify.call_args_list if c.args[1].get("verb") == "provider_off")
        assert echo["event"] == "exchange_ok"
