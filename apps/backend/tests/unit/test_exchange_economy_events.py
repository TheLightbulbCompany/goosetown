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


# ---------------------------------------------------------------------------
# Task 7: reputation.updated event + reindex_trust resilience
# ---------------------------------------------------------------------------


class TestReputationUpdatedEvent:
    def test_update_trust_emits_reputation_updated(self):
        """_update_trust must emit 'reputation.updated' after a successful trust write."""
        from core.services import exchange_service

        emit_calls = []

        def fake_emit(tag, summary, **kw):
            emit_calls.append({"tag": tag, "summary": summary, **kw})

        with (
            patch("core.services.exchange_service.TownService") as mock_ts,
            patch("core.services.exchange_service.dynamo") as mock_dynamo,
            patch("core.services.exchange_service._count_recent_same_counterparty_payouts", return_value=0),
            patch("core.services.exchange_service._bump_counter"),
            patch("core.services.exchange_service.ProviderDirectory"),
            patch("core.services.exchange_service._emit_economy_event", side_effect=fake_emit),
        ):
            # Simulate an agent row with initial trust; no CAS conflict.
            mock_ts.return_value.get_town_agent_by_id.return_value = {
                "id": "agent-1",
                "agentName": "Alice",
                "displayName": "Alice",
                "trustScore": "0.5",
                "jobsCompleted": 2,
            }
            agents_table = mock_dynamo.agents_table.return_value
            agents_table.update_item = MagicMock()  # no exception → success on first try

            exchange_service._update_trust("agent-1", "SUCCESS", completed_delta=1, failed_delta=0)

        rep_events = [e for e in emit_calls if e["tag"] == "reputation.updated"]
        assert rep_events, f"Expected 'reputation.updated' event; got tags: {[e['tag'] for e in emit_calls]}"
        ev = rep_events[0]
        assert "Alice" in ev["summary"]
        assert "agent_id" in ev
        assert "new_trust" in ev
        assert "tier" in ev

    def test_update_trust_reputation_event_not_emitted_on_retry_exhaustion(self):
        """If all CAS retries fail, reputation.updated must NOT be emitted."""
        from core.services import exchange_service

        emit_calls = []

        def fake_emit(tag, summary, **kw):
            emit_calls.append(tag)

        # Build a real-ish exception class that the except clause will catch.
        class FakeConditionalCheckFailed(Exception):
            pass

        with (
            patch("core.services.exchange_service.TownService") as mock_ts,
            patch("core.services.exchange_service.dynamo") as mock_dynamo,
            patch("core.services.exchange_service._count_recent_same_counterparty_payouts", return_value=0),
            patch("core.services.exchange_service._bump_counter"),
            patch("core.services.exchange_service.ProviderDirectory"),
            patch("core.services.exchange_service._emit_economy_event", side_effect=fake_emit),
        ):
            mock_ts.return_value.get_town_agent_by_id.return_value = {
                "id": "agent-2",
                "agentName": "Bob",
                "trustScore": "0.5",
                "jobsCompleted": 0,
            }
            agents_table = mock_dynamo.agents_table.return_value
            # Wire the exception class so the except clause recognises it.
            agents_table.meta.client.exceptions.ConditionalCheckFailedException = FakeConditionalCheckFailed
            # Always raise → exhaust all retries, trigger the else branch + return.
            agents_table.update_item.side_effect = FakeConditionalCheckFailed("cas failed")

            exchange_service._update_trust("agent-2", "SUCCESS", completed_delta=1, failed_delta=0)

        assert "reputation.updated" not in emit_calls

    def test_reindex_failure_does_not_break_update_trust(self):
        """A raising reindex_trust must not propagate — _update_trust still completes."""
        from core.services import exchange_service

        with (
            patch("core.services.exchange_service.TownService") as mock_ts,
            patch("core.services.exchange_service.dynamo") as mock_dynamo,
            patch("core.services.exchange_service._count_recent_same_counterparty_payouts", return_value=0),
            patch("core.services.exchange_service._bump_counter"),
            patch("core.services.exchange_service.ProviderDirectory") as mock_pd_cls,
            patch("core.services.exchange_service._emit_economy_event"),
        ):
            mock_ts.return_value.get_town_agent_by_id.return_value = {
                "id": "agent-3",
                "agentName": "Carol",
                "trustScore": "0.5",
                "jobsCompleted": 1,
            }
            agents_table = mock_dynamo.agents_table.return_value
            agents_table.update_item = MagicMock()  # success

            # Make reindex_trust blow up.
            mock_pd_cls.return_value.reindex_trust.side_effect = RuntimeError("DDB unavailable")

            # Must not raise — settlement must survive a denorm failure.
            exchange_service._update_trust("agent-3", "SUCCESS", completed_delta=1, failed_delta=0)


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
