"""Tests for the exchange verb dispatch ladder in handle_ws_message (Task 8).

Mirrors test_goals_actions.py: call handle_ws_message directly with mocked
collaborators — no HTTP round-trip, no DynamoDB, no PixelLab.

The five verbs under test: post_job, bid, cancel_job, deliver_job, review_job.
Each test asserts:
  1. The correct exchange_service function is called with the right kwargs.
  2. The handler returns {"statusCode": 200, "body": <service result>}.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SENTINEL_RESULT = {"status": "ok", "job_id": "job-sentinel-id"}


def _make_conn(agent_id: str = "alice-123", user_id: str = "user-1") -> SimpleNamespace:
    """Minimal AgentConnection-like object with fields the handler reads."""
    return SimpleNamespace(
        agent_id=agent_id,
        user_id=user_id,
        agent_name="alice",
        location_context="town",
        exhausted_rejections=0,
        leaving_cleanly=False,
    )


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def handler_setup():
    """Patch collaborators that handle_ws_message resolves at dispatch time.

    Yields a SimpleNamespace with:
    - ws_manager: mocked ws manager whose get_by_connection returns _make_conn()
    - agent_conn: the AgentConnection stub
    - mock_exchange: patched routers.town.exchange_service module with AsyncMocks
    """
    agent_conn = _make_conn()
    ws_manager = MagicMock()
    ws_manager.get_by_connection.return_value = agent_conn

    # exchange_service is a module; give every public async function an AsyncMock.
    mock_exchange = MagicMock()
    mock_exchange.post_job = AsyncMock(return_value=_SENTINEL_RESULT)
    mock_exchange.place_bid = AsyncMock(return_value=_SENTINEL_RESULT)
    mock_exchange.cancel_job = AsyncMock(return_value=_SENTINEL_RESULT)
    mock_exchange.deliver_job = AsyncMock(return_value=_SENTINEL_RESULT)
    mock_exchange.review_job = AsyncMock(return_value=_SENTINEL_RESULT)
    mock_exchange.invite_provider = AsyncMock(return_value=_SENTINEL_RESULT)

    with (
        patch("routers.town.get_town_agent_ws_manager", return_value=ws_manager),
        # Energy gate calls TownService; return an agent with energy > 0
        # so it doesn't block the path before the action dispatcher.
        patch(
            "routers.town.TownService",
            return_value=MagicMock(
                **{"get_town_agent_by_id.return_value": {"energy": 80, "mood": 5}},
            ),
        ),
        patch("routers.town.exchange_service", mock_exchange),
        # game server client is looked up before the elif chain — give it a no-op.
        patch("routers.town.get_game_server_client", return_value=MagicMock()),
    ):
        yield SimpleNamespace(
            ws_manager=ws_manager,
            agent_conn=agent_conn,
            mock_exchange=mock_exchange,
        )


# ---------------------------------------------------------------------------
# Tests — one per verb
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_job_dispatches_to_service(handler_setup):
    """post_job routes to exchange_service.post_job with correct kwargs."""
    from fastapi import Request

    from routers.town import WsMessageRequest, handle_ws_message

    request = WsMessageRequest(
        connectionId="conn-abc",
        principalId="user-1",
        body={
            "type": "town_agent_act",
            "action": "post_job",
            "title": "Need a haiku",
            "description": "Write me a haiku about crumbs",
            "max_price": 50,
            "strategy": "lowest_price",
        },
    )
    raw = MagicMock(spec=Request)
    raw.headers = {}

    result = await handle_ws_message(request, raw)

    assert result == {"statusCode": 200, "body": _SENTINEL_RESULT}
    handler_setup.mock_exchange.post_job.assert_called_once_with(
        handler_setup.agent_conn,
        title="Need a haiku",
        description="Write me a haiku about crumbs",
        max_price=50,
        strategy="lowest_price",
        success_criteria=None,
        category=None,
        tags=None,
    )


@pytest.mark.asyncio
async def test_bid_dispatches_to_service(handler_setup):
    """bid routes to exchange_service.place_bid with correct kwargs."""
    from fastapi import Request

    from routers.town import WsMessageRequest, handle_ws_message

    request = WsMessageRequest(
        connectionId="conn-abc",
        principalId="user-1",
        body={
            "type": "town_agent_act",
            "action": "bid",
            "job_id": "job-xyz-999",
            "price": 40,
            "confidence": 0.8,
            "pitch": "I am very fast",
        },
    )
    raw = MagicMock(spec=Request)
    raw.headers = {}

    result = await handle_ws_message(request, raw)

    assert result == {"statusCode": 200, "body": _SENTINEL_RESULT}
    handler_setup.mock_exchange.place_bid.assert_called_once_with(
        handler_setup.agent_conn,
        job_id="job-xyz-999",
        price=40,
        confidence=0.8,
        pitch="I am very fast",
    )


@pytest.mark.asyncio
async def test_cancel_job_dispatches_to_service(handler_setup):
    """cancel_job routes to exchange_service.cancel_job with correct kwargs."""
    from fastapi import Request

    from routers.town import WsMessageRequest, handle_ws_message

    request = WsMessageRequest(
        connectionId="conn-abc",
        principalId="user-1",
        body={
            "type": "town_agent_act",
            "action": "cancel_job",
            "job_id": "job-to-cancel",
        },
    )
    raw = MagicMock(spec=Request)
    raw.headers = {}

    result = await handle_ws_message(request, raw)

    assert result == {"statusCode": 200, "body": _SENTINEL_RESULT}
    handler_setup.mock_exchange.cancel_job.assert_called_once_with(
        handler_setup.agent_conn,
        job_id="job-to-cancel",
    )


@pytest.mark.asyncio
async def test_invite_dispatches_to_service(handler_setup):
    """invite routes to exchange_service.invite_provider with correct kwargs."""
    from fastapi import Request

    from routers.town import WsMessageRequest, handle_ws_message

    request = WsMessageRequest(
        connectionId="conn-abc",
        principalId="user-1",
        body={
            "type": "town_agent_act",
            "action": "invite",
            "job_id": "job-xyz-999",
            "provider_id": "prov-42",
        },
    )
    raw = MagicMock(spec=Request)
    raw.headers = {}

    result = await handle_ws_message(request, raw)

    assert result == {"statusCode": 200, "body": _SENTINEL_RESULT}
    handler_setup.mock_exchange.invite_provider.assert_called_once_with(
        handler_setup.agent_conn,
        job_id="job-xyz-999",
        provider_id="prov-42",
    )


@pytest.mark.asyncio
async def test_deliver_job_dispatches_to_service(handler_setup):
    """deliver_job routes to exchange_service.deliver_job with correct kwargs."""
    from fastapi import Request

    from routers.town import WsMessageRequest, handle_ws_message

    request = WsMessageRequest(
        connectionId="conn-abc",
        principalId="user-1",
        body={
            "type": "town_agent_act",
            "action": "deliver_job",
            "job_id": "job-being-delivered",
            "deliverable": "Here is the haiku: Crumbs fall gently / The Exchange hums / Balance achieved",
        },
    )
    raw = MagicMock(spec=Request)
    raw.headers = {}

    result = await handle_ws_message(request, raw)

    assert result == {"statusCode": 200, "body": _SENTINEL_RESULT}
    handler_setup.mock_exchange.deliver_job.assert_called_once_with(
        handler_setup.agent_conn,
        job_id="job-being-delivered",
        deliverable="Here is the haiku: Crumbs fall gently / The Exchange hums / Balance achieved",
        artifact_key=None,  # W5: text-only delivery carries no artifact
    )


@pytest.mark.asyncio
async def test_review_job_dispatches_to_service(handler_setup):
    """review_job routes to exchange_service.review_job with correct kwargs."""
    from fastapi import Request

    from routers.town import WsMessageRequest, handle_ws_message

    request = WsMessageRequest(
        connectionId="conn-abc",
        principalId="user-1",
        body={
            "type": "town_agent_act",
            "action": "review_job",
            "job_id": "job-under-review",
            "accept": False,
            "reason": "not what I asked for",
        },
    )
    raw = MagicMock(spec=Request)
    raw.headers = {}

    result = await handle_ws_message(request, raw)

    assert result == {"statusCode": 200, "body": _SENTINEL_RESULT}
    handler_setup.mock_exchange.review_job.assert_called_once_with(
        handler_setup.agent_conn,
        job_id="job-under-review",
        accept=False,
        reason="not what I asked for",
        criteria_verdicts=None,
    )


# ---------------------------------------------------------------------------
# Coercion / strict-validation-off safety tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_job_bad_max_price_string_coerced_to_zero(handler_setup):
    """post_job with max_price='abc' in a raw body is coerced to 0, not 500.

    This exercises the WS_STRICT_VALIDATION=false rollback path — strict
    validation is patched off so the untyped body reaches the dispatch ladder.
    """
    from fastapi import Request

    from routers.town import WsMessageRequest, handle_ws_message

    request = WsMessageRequest(
        connectionId="conn-abc",
        principalId="user-1",
        body={
            "type": "town_agent_act",
            "action": "post_job",
            "title": "Bad price test",
            "description": "Should coerce gracefully",
            "max_price": "abc",
            "strategy": "balanced",
        },
    )
    raw = MagicMock(spec=Request)
    raw.headers = {}

    with patch("core.config.settings") as mock_settings:
        mock_settings.WS_STRICT_VALIDATION = False
        mock_settings.ENVIRONMENT = "dev"
        mock_settings.WS_LAMBDA_SECRET = ""  # nosec B105: empty test stub, not a real secret
        result = await handle_ws_message(request, raw)

    assert result == {"statusCode": 200, "body": _SENTINEL_RESULT}
    handler_setup.mock_exchange.post_job.assert_called_once_with(
        handler_setup.agent_conn,
        title="Bad price test",
        description="Should coerce gracefully",
        max_price=0,
        strategy="balanced",
        success_criteria=None,
        category=None,
        tags=None,
    )


@pytest.mark.asyncio
async def test_review_job_missing_accept_passes_none(handler_setup):
    """review_job with accept absent passes accept=None to the service.

    This exercises the WS_STRICT_VALIDATION=false rollback path — strict
    validation is patched off so the body without 'accept' reaches the dispatch
    ladder, which must pass None rather than raising or silently coercing.
    """
    from fastapi import Request

    from routers.town import WsMessageRequest, handle_ws_message

    request = WsMessageRequest(
        connectionId="conn-abc",
        principalId="user-1",
        body={
            "type": "town_agent_act",
            "action": "review_job",
            "job_id": "job-no-accept",
            "reason": "forgot to include accept",
        },
    )
    raw = MagicMock(spec=Request)
    raw.headers = {}

    with patch("core.config.settings") as mock_settings:
        mock_settings.WS_STRICT_VALIDATION = False
        mock_settings.ENVIRONMENT = "dev"
        mock_settings.WS_LAMBDA_SECRET = ""  # nosec B105: empty test stub, not a real secret
        result = await handle_ws_message(request, raw)

    assert result == {"statusCode": 200, "body": _SENTINEL_RESULT}
    handler_setup.mock_exchange.review_job.assert_called_once_with(
        handler_setup.agent_conn,
        job_id="job-no-accept",
        accept=None,
        reason="forgot to include accept",
        criteria_verdicts=None,
    )
