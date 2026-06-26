"""Tests for connect-time exchange resync (missed-notify safety).

``resync_agent_jobs`` re-sends action-required job events the agent may have
missed (stale connection at award/deliver time, daemon restart). Driven
directly — no connect handshake — with the mailbox stubbed.
"""

from __future__ import annotations

import time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.services import exchange_service as ex

# ---------------------------------------------------------------------------
# Fixtures & helpers (mirroring test_exchange_lifecycle conventions)
# ---------------------------------------------------------------------------


class StubWsManager:
    def __init__(self):
        self.conns: dict[str, SimpleNamespace] = {}

    def connected_agents(self):
        return list(self.conns.values())

    def get_by_agent_id(self, agent_id):
        return self.conns.get(agent_id)


@pytest.fixture
def mailbox():
    mb = MagicMock()
    mb.notify.return_value = True
    with patch("core.services.exchange_service.get_daemon_mailbox", return_value=mb):
        yield mb


@pytest.fixture
def ws():
    stub = StubWsManager()
    with patch("core.services.exchange_service.get_town_agent_ws_manager", return_value=stub):
        yield stub


@pytest.fixture(autouse=True)
def _clean_timers():
    yield
    for task in list(ex._timers.values()):
        task.cancel()
    ex._timers.clear()


def _seed_job(db, job_id: str, **attrs) -> None:
    item = {
        "id": job_id,
        "title": "Research tools",
        "description": "Compare three pixel-art tools.",
        "maxPrice": 30,
        "createdAt": "2026-06-12T00:00:00+00:00",
        "bids": {},
    }
    item.update(attrs)
    db.Table("goosetown-jobs").put_item(Item=item)


def _events_for(mailbox, agent_id: str) -> list[dict]:
    return [c.args[1] for c in mailbox.notify.call_args_list if c.args[0] == agent_id]


# ---------------------------------------------------------------------------
# resync_agent_jobs
# ---------------------------------------------------------------------------


class TestResyncAgentJobs:
    def test_awarded_provider_renotified_with_recomputed_deadline(self, dynamodb_tables, mailbox):
        _seed_job(
            dynamodb_tables,
            "j1",
            status="AWARDED",
            providerId="prov-1",
            providerName="pat",
            consumerId="cons-1",
            consumerName="carol",
            agreedPrice=20,
            deliveryDeadlineAt=Decimal(str(time.time() + 100)),
            successCriteria=["covers three tools"],
        )

        sent = ex.resync_agent_jobs("prov-1")

        assert sent == 1
        (evt,) = _events_for(mailbox, "prov-1")
        assert evt["event"] == "job_awarded"
        assert evt["resync"] is True
        assert evt["job_id"] == "j1"
        assert evt["agreed_price"] == 20
        assert 0 <= evt["deadline_sec"] <= 100
        assert evt["description"] == "Compare three pixel-art tools."
        assert evt["success_criteria"] == ["covers three tools"]
        assert evt["provider_id"] == "prov-1"
        assert evt["consumer_id"] == "cons-1"

    def test_awarded_past_deadline_clamps_to_zero(self, dynamodb_tables, mailbox):
        _seed_job(
            dynamodb_tables,
            "j1",
            status="AWARDED",
            providerId="prov-1",
            consumerId="cons-1",
            agreedPrice=20,
            deliveryDeadlineAt=Decimal(str(time.time() - 50)),
        )

        ex.resync_agent_jobs("prov-1")

        (evt,) = _events_for(mailbox, "prov-1")
        assert evt["deadline_sec"] == 0  # the sweeper will fail it shortly; never negative

    def test_delivered_consumer_renotified_with_deliverable(self, dynamodb_tables, mailbox):
        _seed_job(
            dynamodb_tables,
            "j2",
            status="DELIVERED",
            providerId="prov-1",
            consumerId="cons-1",
            agreedPrice=20,
            deliverable="the finished work",
            reviewDeadlineAt=Decimal(str(time.time() + 100)),
        )

        sent = ex.resync_agent_jobs("cons-1")

        assert sent == 1
        (evt,) = _events_for(mailbox, "cons-1")
        assert evt["event"] == "job_delivered"
        assert evt["resync"] is True
        assert evt["deliverable"] == "the finished work"

    def test_delivered_provider_gets_nothing(self, dynamodb_tables, mailbox):
        """Provider waiting on review has no action — no resync event."""
        _seed_job(
            dynamodb_tables,
            "j2",
            status="DELIVERED",
            providerId="prov-1",
            consumerId="cons-1",
            deliverable="the finished work",
        )

        assert ex.resync_agent_jobs("prov-1") == 0
        assert _events_for(mailbox, "prov-1") == []

    def test_unrelated_agent_gets_nothing(self, dynamodb_tables, mailbox):
        _seed_job(dynamodb_tables, "j1", status="AWARDED", providerId="prov-1", consumerId="cons-1")
        _seed_job(dynamodb_tables, "j2", status="DELIVERED", providerId="prov-1", consumerId="cons-1")

        assert ex.resync_agent_jobs("stranger") == 0
        assert mailbox.notify.call_count == 0

    def test_query_exception_returns_zero_never_raises(self, dynamodb_tables, mailbox):
        with patch.object(ex, "_jobs_with_status", side_effect=RuntimeError("ddb down")):
            assert ex.resync_agent_jobs("prov-1") == 0

    def test_undelivered_notify_not_counted(self, dynamodb_tables, mailbox):
        mailbox.notify.return_value = False
        _seed_job(dynamodb_tables, "j1", status="AWARDED", providerId="prov-1", consumerId="cons-1")

        assert ex.resync_agent_jobs("prov-1") == 0

    # -----------------------------------------------------------------------
    # New: terminal bid-outcome resync (offline loser gap)
    # -----------------------------------------------------------------------

    def test_offline_loser_on_awarded_job_gets_bid_lost(self, dynamodb_tables, mailbox):
        """Bidder that was offline when the window closed and someone else won
        must receive a resync bid_lost so its stale [BID] entry is cleared."""
        _seed_job(
            dynamodb_tables,
            "j3",
            status="AWARDED",
            providerId="winner-1",
            consumerId="cons-1",
            bids={"loser-2": {"price": 5}, "winner-1": {"price": 10}},
        )

        sent = ex.resync_agent_jobs("loser-2")

        assert sent == 1
        (evt,) = _events_for(mailbox, "loser-2")
        assert evt["event"] == "bid_lost"
        assert evt["job_id"] == "j3"
        assert evt["resync"] is True

    def test_offline_loser_on_completed_job_gets_bid_lost(self, dynamodb_tables, mailbox):
        """Bidder on a job that has already settled (COMPLETED) also gets resync
        bid_lost — the bid window closed and the settlement happened while offline."""
        _seed_job(
            dynamodb_tables,
            "j4",
            status="COMPLETED",
            providerId="winner-1",
            consumerId="cons-1",
            bids={"loser-2": {"price": 5}, "winner-1": {"price": 10}},
        )

        sent = ex.resync_agent_jobs("loser-2")

        assert sent == 1
        (evt,) = _events_for(mailbox, "loser-2")
        assert evt["event"] == "bid_lost"
        assert evt["resync"] is True

    def test_bidder_on_cancelled_job_gets_job_cancelled(self, dynamodb_tables, mailbox):
        """Consumer withdrew the job while the bidder was offline — resync
        job_cancelled so the stale [BID] entry is cleared."""
        _seed_job(
            dynamodb_tables,
            "j5",
            status="CANCELLED",
            consumerId="cons-1",
            bids={"bidder-1": {"price": 8}},
        )

        sent = ex.resync_agent_jobs("bidder-1")

        assert sent == 1
        (evt,) = _events_for(mailbox, "bidder-1")
        assert evt["event"] == "job_cancelled"
        assert evt["job_id"] == "j5"
        assert evt["consumer_id"] == "cons-1"
        assert evt["resync"] is True

    def test_winning_provider_on_awarded_gets_job_awarded_not_bid_lost(self, dynamodb_tables, mailbox):
        """The winning provider's agent_id is also in bids[], but they must
        receive job_awarded (existing path) — NOT bid_lost."""
        _seed_job(
            dynamodb_tables,
            "j6",
            status="AWARDED",
            providerId="winner-1",
            consumerId="cons-1",
            agreedPrice=10,
            deliveryDeadlineAt=__import__("decimal").Decimal(str(__import__("time").time() + 100)),
            bids={"winner-1": {"price": 10}},
        )

        sent = ex.resync_agent_jobs("winner-1")

        assert sent == 1
        events = _events_for(mailbox, "winner-1")
        assert len(events) == 1
        assert events[0]["event"] == "job_awarded"

    def test_agent_not_in_bids_gets_nothing_for_terminal_jobs(self, dynamodb_tables, mailbox):
        """An agent that never bid on a job must not receive bid_lost."""
        _seed_job(
            dynamodb_tables,
            "j7",
            status="COMPLETED",
            providerId="winner-1",
            consumerId="cons-1",
            bids={"winner-1": {"price": 10}},
        )

        assert ex.resync_agent_jobs("stranger") == 0
        assert mailbox.notify.call_count == 0


class TestOpenOfferResync:
    """Wave 2 §3.5 / codex P1 — the dropped-offer recovery's SECOND seam.

    A provider that was offline when a job was posted holds a durable
    ``offeredTo`` bidding right (the matcher commits it before the best-effort
    ``job_offered`` notify — proven in test_exchange_matcher), but has no local
    TOWN_OFFERS entry because the live wake was lost. On reconnect,
    ``resync_agent_jobs`` must re-deliver ``job_offered`` for every OPEN job
    where the agent is in ``offeredTo`` (and isn't the consumer). Without this,
    a missed offer is never healed and the provider silently can't bid.
    """

    def test_offered_provider_gets_job_offered_on_resync(self, dynamodb_tables, mailbox):
        deadline = time.time() + 600
        _seed_job(
            dynamodb_tables,
            "jo1",
            status="OPEN",
            consumerId="cons-1",
            consumerName="carol",
            category="writing.poetry",
            maxPrice=25,
            offeredTo={"prov-1"},
            bidWindowEndsAt=Decimal(str(deadline)),
            successCriteria=["5-7-5 haiku", "mentions the fountain"],
        )

        sent = ex.resync_agent_jobs("prov-1")

        assert sent == 1
        (evt,) = _events_for(mailbox, "prov-1")
        assert evt["event"] == "job_offered"
        assert evt["resync"] is True
        assert evt["job_id"] == "jo1"
        assert evt["title"] == "Research tools"  # _seed_job default
        assert evt["category"] == "writing.poetry"
        assert evt["max_price"] == 25
        assert isinstance(evt["max_price"], int)  # int(), not Decimal — daemon-friendly
        assert evt["consumer_name"] == "carol"
        assert evt["description"] == "Compare three pixel-art tools."
        assert evt["bid_deadline"] is not None and evt["bid_deadline"] > time.time()
        assert evt["criteria"] == ["5-7-5 haiku", "mentions the fountain"]

    def test_consumer_excluded_from_own_open_offer(self, dynamodb_tables, mailbox):
        """The matcher excludes the consumer, but the resync guard must also skip
        a job where the agent IS the consumer even if somehow in offeredTo."""
        _seed_job(
            dynamodb_tables,
            "jo2",
            status="OPEN",
            consumerId="cons-1",
            offeredTo={"cons-1"},
            bidWindowEndsAt=Decimal(str(time.time() + 600)),
        )

        assert ex.resync_agent_jobs("cons-1") == 0
        assert _events_for(mailbox, "cons-1") == []

    def test_agent_not_in_offeredto_gets_no_open_offer(self, dynamodb_tables, mailbox):
        _seed_job(
            dynamodb_tables,
            "jo3",
            status="OPEN",
            consumerId="cons-1",
            offeredTo={"someone-else"},
            bidWindowEndsAt=Decimal(str(time.time() + 600)),
        )

        assert ex.resync_agent_jobs("prov-1") == 0
        assert _events_for(mailbox, "prov-1") == []

    def test_open_job_without_offeredto_is_pull_only(self, dynamodb_tables, mailbox):
        """A job with no subscribers (no offeredTo) lives on the public board
        only — resync must not invent an offer for anyone."""
        _seed_job(
            dynamodb_tables,
            "jo4",
            status="OPEN",
            consumerId="cons-1",
            bidWindowEndsAt=Decimal(str(time.time() + 600)),
        )

        assert ex.resync_agent_jobs("prov-1") == 0

    def test_open_offer_bid_deadline_none_and_no_criteria_when_unset(self, dynamodb_tables, mailbox):
        """Optional fields degrade cleanly: no bid window → bid_deadline None;
        no success criteria → no ``criteria`` key at all."""
        _seed_job(
            dynamodb_tables,
            "jo5",
            status="OPEN",
            consumerId="cons-1",
            offeredTo={"prov-1"},
        )

        ex.resync_agent_jobs("prov-1")

        (evt,) = _events_for(mailbox, "prov-1")
        assert evt["bid_deadline"] is None
        assert "criteria" not in evt

    def test_undelivered_open_offer_not_counted(self, dynamodb_tables, mailbox):
        """If the reconnect notify itself fails (still racing the new connection),
        the offer is not counted as delivered — the next reconnect heals it."""
        mailbox.notify.return_value = False
        _seed_job(
            dynamodb_tables,
            "jo6",
            status="OPEN",
            consumerId="cons-1",
            offeredTo={"prov-1"},
            bidWindowEndsAt=Decimal(str(time.time() + 600)),
        )

        assert ex.resync_agent_jobs("prov-1") == 0


class TestAwardNotifyFailureLogsWarning:
    async def test_winner_notify_false_logs_heal_on_reconnect(self, dynamodb_tables, mailbox, ws, caplog):
        # Seed a closable OPEN job with one connected bidder, but make every
        # notify report non-delivery (stale connection at award time).
        mailbox.notify.return_value = False
        dynamodb_tables.Table("goosetown-agents").put_item(
            Item={"id": "cons-1", "agentName": "carol", "isActive": "true", "currentLocation": "bank"}
        )
        dynamodb_tables.Table("goosetown-agents").put_item(
            Item={"id": "prov-1", "agentName": "pat", "isActive": "true", "currentLocation": "bank"}
        )
        _seed_job(
            dynamodb_tables,
            "j1",
            status="OPEN",
            consumerId="cons-1",
            consumerName="carol",
            consumerUserId="user-c",
            bidWindowEndsAt=Decimal(str(time.time() - 1)),
            bids={"prov-1": {"price": 10, "confidence": Decimal("0.5"), "pitch": "", "bidAt": "2026-06-12T00:00:01"}},
        )
        ws.conns["prov-1"] = SimpleNamespace(
            agent_id="prov-1", agent_name="pat", location_context="town", connection_id="c-p"
        )

        import logging

        with caplog.at_level(logging.WARNING):
            await ex._close_bid_window("j1")

        job = dynamodb_tables.Table("goosetown-jobs").get_item(Key={"id": "j1"})["Item"]
        assert job["status"] == "AWARDED"
        assert any("will heal on reconnect resync" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Terminal-outcome resync for provider + consumer principals
# ---------------------------------------------------------------------------


def _seed_agent(db, agent_id: str, *, balance: int = 50) -> None:
    """Seed a minimal agent row so get_wallet / ensure_signup_grant works."""
    db.Table("goosetown-agents").put_item(
        Item={
            "id": agent_id,
            "agentName": agent_id,
            "isActive": "true",
            "walletBalance": balance,
            "signupGrantAt": "2026-06-12T00:00:00+00:00",  # already granted
        }
    )


class TestTerminalOutcomeResync:
    """Offline provider/consumer receive settlement events on reconnect."""

    def test_offline_provider_on_completed_gets_job_completed_with_payout_and_balance(self, dynamodb_tables, mailbox):
        """Provider that was offline when the job settled receives job_completed
        with payout, fee, and their current wallet balance."""
        _seed_agent(dynamodb_tables, "prov-1", balance=75)
        _seed_job(
            dynamodb_tables,
            "jc1",
            status="COMPLETED",
            providerId="prov-1",
            consumerId="cons-1",
            agreedPrice=20,
            outcome="SUCCESS",
        )

        sent = ex.resync_agent_jobs("prov-1")

        assert sent >= 1
        evts = _events_for(mailbox, "prov-1")
        completed = [e for e in evts if e["event"] == "job_completed"]
        assert completed, "expected at least one job_completed resync event"
        evt = completed[0]
        assert evt["resync"] is True
        assert evt["job_id"] == "jc1"
        # payout = agreedPrice − platform_fee(agreedPrice)
        from core.services import exchange_scoring

        expected_fee = exchange_scoring.platform_fee(20)
        assert evt["payout"] == 20 - expected_fee
        assert evt["fee"] == expected_fee
        # balance attached (current truth from get_wallet)
        assert evt["balance"] == 75

    def test_offline_provider_on_failed_gets_job_failed_with_outcome(self, dynamodb_tables, mailbox):
        """Provider that was offline when the job failed receives job_failed
        with the stored outcome — no fabricated reason."""
        _seed_agent(dynamodb_tables, "prov-1")
        _seed_job(
            dynamodb_tables,
            "jf1",
            status="FAILED",
            providerId="prov-1",
            consumerId="cons-1",
            agreedPrice=15,
            outcome="FAILURE_PROVIDER",
        )

        sent = ex.resync_agent_jobs("prov-1")

        assert sent >= 1
        evts = _events_for(mailbox, "prov-1")
        failed = [e for e in evts if e["event"] == "job_failed"]
        assert failed
        evt = failed[0]
        assert evt["resync"] is True
        assert evt["job_id"] == "jf1"
        assert evt["outcome"] == "FAILURE_PROVIDER"
        # provider didn't receive money — no balance key required on their copy
        # (spec says balance attached only for money-relevant side)

    def test_offline_consumer_on_expired_gets_job_expired_with_balance(self, dynamodb_tables, mailbox):
        """Consumer that was offline when the job expired receives job_expired
        with their current wallet balance (escrow was refunded)."""
        _seed_agent(dynamodb_tables, "cons-1", balance=120)
        _seed_job(
            dynamodb_tables,
            "je1",
            status="EXPIRED",
            consumerId="cons-1",
            maxPrice=30,
        )

        sent = ex.resync_agent_jobs("cons-1")

        assert sent >= 1
        evts = _events_for(mailbox, "cons-1")
        expired = [e for e in evts if e["event"] == "job_expired"]
        assert expired
        evt = expired[0]
        assert evt["resync"] is True
        assert evt["job_id"] == "je1"
        assert evt["balance"] == 120

    def test_balance_attached_exactly_once_across_multiple_resync_events(self, dynamodb_tables, mailbox):
        """When multiple money-relevant terminal jobs resync for the same agent,
        the balance field appears on the FIRST event only — not duplicated."""
        _seed_agent(dynamodb_tables, "prov-1", balance=200)
        # Two completed jobs for the same provider
        _seed_job(
            dynamodb_tables,
            "jm1",
            status="COMPLETED",
            providerId="prov-1",
            consumerId="cons-1",
            agreedPrice=10,
            outcome="SUCCESS",
            createdAt="2026-06-12T00:00:00+00:00",
        )
        _seed_job(
            dynamodb_tables,
            "jm2",
            status="COMPLETED",
            providerId="prov-1",
            consumerId="cons-1",
            agreedPrice=20,
            outcome="SUCCESS",
            createdAt="2026-06-12T00:00:01+00:00",
        )

        ex.resync_agent_jobs("prov-1")

        evts = [e for e in _events_for(mailbox, "prov-1") if e["event"] == "job_completed"]
        assert len(evts) == 2
        # Exactly one event carries "balance"
        with_balance = [e for e in evts if "balance" in e]
        assert len(with_balance) == 1

    def test_recency_bound_respected_via_limit_patch(self, dynamodb_tables, mailbox):
        """_jobs_with_status is called with limit=50 for terminal statuses."""
        _seed_agent(dynamodb_tables, "prov-1")
        with patch.object(ex, "_jobs_with_status", return_value=[]) as mock_jws:
            ex.resync_agent_jobs("prov-1")

        # Verify terminal-status calls include limit=50
        terminal_calls = [
            c for c in mock_jws.call_args_list if c.args and c.args[0] in ("COMPLETED", "FAILED", "EXPIRED")
        ]
        assert terminal_calls, "expected _jobs_with_status called for terminal statuses"
        for c in terminal_calls:
            assert c.kwargs.get("limit") == 50 or (len(c.args) > 1 and c.args[1] == 50), (
                f"expected limit=50 for terminal status call, got {c}"
            )
