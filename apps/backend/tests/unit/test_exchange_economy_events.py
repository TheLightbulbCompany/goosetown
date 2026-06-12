"""Tests for economy world-feed events emitted by exchange_service.

Verifies that:
- _emit_economy_event is called with the right tag/summary at each lifecycle point.
- A raising emitter does not break post_job or _settle (resilience).
- get_ledger_tail returns rows newest-first from the GSI.

All tests mock AWS collaborators so they never touch real infrastructure.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Minimal fakes
# ---------------------------------------------------------------------------


def _make_conn(
    agent_id: str = "consumer-1",
    agent_name: str = "Alice",
    user_id: str = "user-1",
    location_context: str = "town",
) -> SimpleNamespace:
    return SimpleNamespace(
        agent_id=agent_id,
        agent_name=agent_name,
        user_id=user_id,
        location_context=location_context,
    )


def _make_job(
    job_id: str = "job-1",
    consumer_id: str = "consumer-1",
    consumer_name: str = "Alice",
    title: str = "Paint the barn",
    max_price: int = 100,
    agreed_price: int = 80,
    provider_id: str = "provider-1",
    provider_name: str = "Bob",
    status: str = "DELIVERED",
) -> dict:
    return {
        "id": job_id,
        "consumerId": consumer_id,
        "consumerName": consumer_name,
        "title": title,
        "maxPrice": max_price,
        "agreedPrice": agreed_price,
        "providerId": provider_id,
        "providerName": provider_name,
        "status": status,
        "bids": {},
    }


# ---------------------------------------------------------------------------
# Context-manager helper: suppress all external calls for pure unit tests
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _patched(extra_patches=None):
    """Enter a set of patches that suppress all AWS calls in exchange_service."""
    common = [
        patch("core.services.exchange_service.TownService"),
        patch("core.services.exchange_service.dynamo"),
        patch("core.services.exchange_service.get_daemon_mailbox", return_value=MagicMock()),
        patch("core.services.exchange_service.get_town_agent_ws_manager", return_value=MagicMock()),
        patch("core.services.exchange_service.town_mood_engine"),
    ]
    stack = contextlib.ExitStack()
    mocks = []
    try:
        for p in common + (extra_patches or []):
            mocks.append(stack.enter_context(p))
        yield mocks
    finally:
        stack.close()


# ---------------------------------------------------------------------------
# _emit_economy_event helper: lazy import, suppress exceptions
# ---------------------------------------------------------------------------


class TestEmitEconomyEvent:
    def test_calls_event_publisher_with_correct_args(self):
        """_emit_economy_event delegates to event_publisher.emit_world_event."""
        from core.services.exchange_service import _emit_economy_event

        with patch("worldgm.event_publisher.emit_world_event") as mock_emit_fn:
            _emit_economy_event(
                "economy.test_tag",
                summary="Test summary",
                location="bank",
                job_id="j1",
            )

        mock_emit_fn.assert_called_once()
        call_kwargs = mock_emit_fn.call_args.kwargs
        assert call_kwargs["tag"] == "economy.test_tag"
        assert call_kwargs["summary"] == "Test summary"
        assert call_kwargs["location"] == "bank"
        assert call_kwargs["detail"]["job_id"] == "j1"

    def test_raising_emitter_does_not_propagate(self):
        """A failing emit must be swallowed (EventBridge absent in tests/local)."""
        from core.services.exchange_service import _emit_economy_event

        with patch(
            "worldgm.event_publisher.emit_world_event",
            side_effect=RuntimeError("EventBridge unavailable"),
        ):
            # Must not raise.
            _emit_economy_event("economy.whatever", summary="x")


# ---------------------------------------------------------------------------
# post_job emits economy.job_posted
# ---------------------------------------------------------------------------


class TestPostJobEmitsEvent:
    @pytest.mark.asyncio
    async def test_post_job_emits_economy_job_posted(self):
        """post_job must call _emit_economy_event with tag=economy.job_posted."""
        from core.services import exchange_service

        mock_agent = {"currentLocation": "bank", "userId": "user-1"}
        agent_conn_local = _make_conn(location_context="town")

        emit_patch = patch("core.services.exchange_service._emit_economy_event")
        ts_patch = patch.object(
            exchange_service.TownService,
            "get_town_agent_by_id",
            return_value=mock_agent,
        )
        extra = [
            patch("core.services.exchange_service.debit_wallet", return_value=90),
            patch("core.services.exchange_service._check_rate", return_value=True),
            patch("core.services.exchange_service._schedule"),
            patch("core.services.exchange_service._bump_counter"),
            patch("core.services.exchange_service.get_wallet", return_value={"balance": 90}),
            emit_patch,
            ts_patch,
        ]
        with _patched(extra) as mocks:
            mock_emit = mocks[-2]  # emit_patch result
            # Stub DDB put_item so the job write succeeds.
            exchange_service.dynamo.jobs_table.return_value.put_item = MagicMock()
            exchange_service.TownService.return_value.get_town_agent_by_id.return_value = mock_agent

            await exchange_service.post_job(
                agent_conn_local,
                title="Paint the barn",
                description="Need it done by Tuesday",
                max_price=100,
                strategy="balanced",
            )

        emit_tags = [c.args[0] if c.args else c.kwargs.get("tag") for c in mock_emit.call_args_list]
        assert "economy.job_posted" in emit_tags

    @pytest.mark.asyncio
    async def test_raising_event_publisher_does_not_break_post_job(self):
        """A raising event_publisher must be swallowed by _emit_economy_event's
        try/except so post_job still returns a result dict."""
        from core.services import exchange_service

        mock_agent = {"currentLocation": "bank", "userId": "user-1"}
        agent_conn_local = _make_conn(location_context="town")

        extra = [
            patch("core.services.exchange_service.debit_wallet", return_value=90),
            patch("core.services.exchange_service._check_rate", return_value=True),
            patch("core.services.exchange_service._schedule"),
            patch("core.services.exchange_service._bump_counter"),
            patch("core.services.exchange_service.get_wallet", return_value={"balance": 90}),
            patch(
                "worldgm.event_publisher.emit_world_event",
                side_effect=RuntimeError("bus down"),
            ),
            patch.object(
                exchange_service.TownService,
                "get_town_agent_by_id",
                return_value=mock_agent,
            ),
        ]
        with _patched(extra):
            exchange_service.dynamo.jobs_table.return_value.put_item = MagicMock()
            exchange_service.TownService.return_value.get_town_agent_by_id.return_value = mock_agent

            result = await exchange_service.post_job(
                agent_conn_local,
                title="Test",
                description="desc",
                max_price=50,
                strategy="balanced",
            )

        # post_job should still return a result dict, not raise.
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# _settle emits economy.job_completed / economy.job_failed
# ---------------------------------------------------------------------------


class TestSettleEmitsEvents:
    @pytest.mark.asyncio
    async def test_settle_success_emits_job_completed(self):
        """_settle(SUCCESS) must emit economy.job_completed."""
        from core.services import exchange_service

        job = _make_job(status="DELIVERED")

        extra = [
            patch("core.services.exchange_service._flip_status", return_value={"outcome": "SUCCESS"}),
            patch("core.services.exchange_service._cancel_timer"),
            patch("core.services.exchange_service.credit_wallet", return_value=75),
            patch("core.services.exchange_service._ledger_append"),
            patch("core.services.exchange_service._update_trust"),
            patch("core.services.exchange_service._apply_mood"),
            patch("core.services.exchange_service.exchange_scoring"),
            patch("core.services.exchange_service._emit_economy_event"),
        ]
        with _patched(extra) as mocks:
            mock_emit = mocks[-1]
            exchange_service.exchange_scoring.platform_fee.return_value = 5
            exchange_service.get_daemon_mailbox.return_value = MagicMock()

            await exchange_service._settle(job, "SUCCESS")

        emit_tags = [c.args[0] if c.args else c.kwargs.get("tag") for c in mock_emit.call_args_list]
        assert "economy.job_completed" in emit_tags

    @pytest.mark.asyncio
    async def test_settle_failure_emits_job_failed(self):
        """_settle(FAILURE_PROVIDER) must emit economy.job_failed."""
        from core.services import exchange_service

        job = _make_job(status="DELIVERED")

        extra = [
            patch("core.services.exchange_service._flip_status", return_value={"outcome": "FAILURE_PROVIDER"}),
            patch("core.services.exchange_service._cancel_timer"),
            patch("core.services.exchange_service.credit_wallet", return_value=80),
            patch("core.services.exchange_service._ledger_append"),
            patch("core.services.exchange_service._update_trust"),
            patch("core.services.exchange_service._apply_mood"),
            patch("core.services.exchange_service._bump_counter"),
            patch("core.services.exchange_service._emit_economy_event"),
        ]
        with _patched(extra) as mocks:
            mock_emit = mocks[-1]
            exchange_service.get_daemon_mailbox.return_value = MagicMock()

            await exchange_service._settle(job, "FAILURE_PROVIDER", rejected_by_consumer=True)

        emit_tags = [c.args[0] if c.args else c.kwargs.get("tag") for c in mock_emit.call_args_list]
        assert "economy.job_failed" in emit_tags

    @pytest.mark.asyncio
    async def test_raising_event_publisher_does_not_break_settle(self):
        """A raising event_publisher must be swallowed by _emit_economy_event's
        try/except so _settle still returns True."""
        from core.services import exchange_service

        job = _make_job(status="DELIVERED")

        extra = [
            patch("core.services.exchange_service._flip_status", return_value={"outcome": "SUCCESS"}),
            patch("core.services.exchange_service._cancel_timer"),
            patch("core.services.exchange_service.credit_wallet", return_value=75),
            patch("core.services.exchange_service._ledger_append"),
            patch("core.services.exchange_service._update_trust"),
            patch("core.services.exchange_service._apply_mood"),
            patch("core.services.exchange_service.exchange_scoring"),
            patch(
                "worldgm.event_publisher.emit_world_event",
                side_effect=RuntimeError("bus down"),
            ),
        ]
        with _patched(extra):
            exchange_service.exchange_scoring.platform_fee.return_value = 5
            exchange_service.get_daemon_mailbox.return_value = MagicMock()

            result = await exchange_service._settle(job, "SUCCESS")

        # _settle must still return True (the flip succeeded).
        assert result is True


# ---------------------------------------------------------------------------
# get_ledger_tail returns rows newest-first
# ---------------------------------------------------------------------------


class TestGetLedgerTail:
    def test_returns_rows_newest_first(self, dynamodb_tables):
        """get_ledger_tail must return at most limit rows, newest createdAt first."""
        from core.services.exchange_service import get_ledger_tail

        agent_id = "tail-agent-1"
        ledger_table = dynamodb_tables.Table("goosetown-ledger")
        ledger_table.put_item(
            Item={
                "id": "old-row",
                "agentId": agent_id,
                "entryType": "GRANT",
                "amount": 100,
                "balanceAfter": 100,
                "refId": "signup",
                "createdAt": "2026-06-01T10:00:00+00:00",
            }
        )
        ledger_table.put_item(
            Item={
                "id": "new-row",
                "agentId": agent_id,
                "entryType": "ESCROW_DEBIT",
                "amount": -20,
                "balanceAfter": 80,
                "refId": "job-abc",
                "createdAt": "2026-06-01T12:00:00+00:00",
            }
        )

        rows = get_ledger_tail(agent_id, limit=15)
        assert len(rows) == 2
        # Newest first.
        assert rows[0]["at"] > rows[1]["at"]
        assert rows[0]["type"] == "ESCROW_DEBIT"
        assert rows[1]["type"] == "GRANT"

    def test_respects_limit(self, dynamodb_tables):
        """get_ledger_tail must honour the limit parameter."""
        from core.services.exchange_service import get_ledger_tail

        agent_id = "tail-agent-2"
        ledger_table = dynamodb_tables.Table("goosetown-ledger")
        for i in range(5):
            ledger_table.put_item(
                Item={
                    "id": f"row-{i}",
                    "agentId": agent_id,
                    "entryType": "GRANT",
                    "amount": 10,
                    "balanceAfter": 10 * (i + 1),
                    "refId": f"ref-{i}",
                    "createdAt": f"2026-06-01T{10 + i:02d}:00:00+00:00",
                }
            )

        rows = get_ledger_tail(agent_id, limit=3)
        assert len(rows) <= 3

    def test_empty_agent_returns_empty_list(self, dynamodb_tables):
        """Agent with no ledger rows → empty list (no error)."""
        from core.services.exchange_service import get_ledger_tail

        rows = get_ledger_tail("agent-with-no-rows", limit=15)
        assert rows == []
