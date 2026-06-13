"""Tests for the Exchange job lifecycle (Task 6) — post/bid/award/deliver/review/settle.

Timers are never slept on: transitions are driven by awaiting the internal
functions directly (``_close_bid_window`` etc.). The daemon mailbox and the
WS connection registry are stubbed.
"""

from __future__ import annotations

import time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from boto3.dynamodb.conditions import Attr

from core.services import exchange_scoring
from core.services import exchange_service as ex

# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


class StubWsManager:
    """Minimal stand-in for TownAgentWsManager."""

    def __init__(self):
        self.conns: dict[str, SimpleNamespace] = {}  # agent_id -> conn

    def connected_agents(self):
        return list(self.conns.values())

    def get_by_agent_id(self, agent_id):
        return self.conns.get(agent_id)


@pytest.fixture
def mailbox():
    mb = MagicMock()
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


def _seed_agent(db, agent_id: str, name: str, location: str = "bank", **extra) -> None:
    item = {
        "id": agent_id,
        "agentName": name,
        "displayName": name,
        "isActive": "true",
        "currentLocation": location,
        "energy": 100,
        "mood": 0,
        "traits": "",
    }
    item.update(extra)
    db.Table("goosetown-agents").put_item(Item=item)


def _conn(agent_id: str, name: str, conn_id: str = "c1", ctx: str = "town") -> SimpleNamespace:
    return SimpleNamespace(agent_id=agent_id, agent_name=name, location_context=ctx, connection_id=conn_id)


def _get_job(db, job_id: str) -> dict:
    return db.Table("goosetown-jobs").get_item(Key={"id": job_id})["Item"]


def _get_agent(db, agent_id: str) -> dict:
    return db.Table("goosetown-agents").get_item(Key={"id": agent_id})["Item"]


def _ledger_rows(db, agent_id: str) -> list[dict]:
    resp = db.Table("goosetown-ledger").scan(FilterExpression=Attr("agentId").eq(agent_id))
    return resp.get("Items", [])


def _notified_events(mailbox) -> list[tuple[str, str]]:
    """(agent_id, event-name) for every mailbox.notify call."""
    return [(c.args[0], c.args[1]["event"]) for c in mailbox.notify.call_args_list]


async def _post(db, ws, consumer_id="consumer", consumer_name="carol", max_price=30, strategy="balanced"):
    _seed_agent(db, consumer_id, consumer_name)
    cc = _conn(consumer_id, consumer_name, f"c-{consumer_id}")
    ws.conns[consumer_id] = cc
    result = await ex.post_job(cc, "Research tools", "Compare three pixel-art tools.", max_price, strategy)
    assert result["status"] == "ok", result
    return cc, result["job_id"]


# ---------------------------------------------------------------------------
# post_job
# ---------------------------------------------------------------------------


class TestPostJob:
    async def test_happy_path_job_open_and_escrowed(self, dynamodb_tables, mailbox, ws):
        cc, job_id = await _post(dynamodb_tables, ws, max_price=30)

        job = _get_job(dynamodb_tables, job_id)
        assert job["status"] == "OPEN"
        assert int(job["maxPrice"]) == 30
        assert job["consumerId"] == "consumer"
        assert job["bids"] == {}
        agent = _get_agent(dynamodb_tables, "consumer")
        assert int(agent["walletBalance"]) == 70
        assert int(agent["jobsPosted"]) == 1

    async def test_happy_path_no_all_connections_broadcast(self, dynamodb_tables, mailbox, ws):
        # Wave 2 (spec §3.3): the old all-connections `job_posted` broadcast is
        # replaced by the targeted category-matched offer loop. A connected
        # bystander that isn't a subscriber gets nothing pushed.
        _seed_agent(dynamodb_tables, "other", "olly")
        ws.conns["other"] = _conn("other", "olly", "c-other")
        cc, job_id = await _post(dynamodb_tables, ws)

        # No notify_connection broadcast at all (the fan-out moved to mailbox.notify
        # targeted at offeredTo — covered in test_exchange_matcher.py).
        assert mailbox.notify_connection.call_count == 0
        offer_targets = [c.args[0] for c in mailbox.notify.call_args_list if c.args[1].get("event") == "job_offered"]
        assert "other" not in offer_targets

    async def test_happy_path_notifies_poster_exchange_ok(self, dynamodb_tables, mailbox, ws):
        cc, job_id = await _post(dynamodb_tables, ws)
        events = _notified_events(mailbox)
        assert ("consumer", "exchange_ok") in events

    async def test_not_at_bank_rejected_no_debit(self, dynamodb_tables, mailbox, ws):
        _seed_agent(dynamodb_tables, "consumer", "carol", location="plaza")
        cc = _conn("consumer", "carol")
        result = await ex.post_job(cc, "T", "D", 30, "balanced")

        assert result == {
            "status": "rejected",
            "reason": "not_at_exchange",
            "message": result["message"],
        }
        agent = _get_agent(dynamodb_tables, "consumer")
        assert "walletBalance" not in agent or int(agent["walletBalance"]) == 100
        assert _ledger_rows(dynamodb_tables, "consumer") == [] or all(
            r["entryType"] == "GRANT" for r in _ledger_rows(dynamodb_tables, "consumer")
        )
        assert ("consumer", "exchange_rejected") in _notified_events(mailbox)

    async def test_apartment_context_rejected(self, dynamodb_tables, mailbox, ws):
        _seed_agent(dynamodb_tables, "consumer", "carol", location="bank")
        cc = _conn("consumer", "carol", ctx="apartment")
        result = await ex.post_job(cc, "T", "D", 30, "balanced")
        assert result["reason"] == "not_at_exchange"

    async def test_post_immediately_after_arrival_write_accepted(self, dynamodb_tables, mailbox, ws):
        """Finding 2: a post_job right after the move-arrival currentLocation write
        must see the committed location. The location gate uses a strongly-consistent
        read, so the just-written ``bank`` is visible immediately (no stale reject)."""
        _seed_agent(dynamodb_tables, "consumer", "carol", location="plaza")
        cc = _conn("consumer", "carol")
        ws.conns["consumer"] = cc
        # Simulate the arrival handler committing currentLocation=bank just before
        # the post_job comes in (daemon_bridge writes before it notifies the daemon).
        dynamodb_tables.Table("goosetown-agents").update_item(
            Key={"id": "consumer"},
            UpdateExpression="SET currentLocation = :loc",
            ExpressionAttributeValues={":loc": "bank"},
        )
        result = await ex.post_job(cc, "T", "D", 30, "balanced")
        assert result["status"] == "ok", result

    async def test_bad_price_rejected(self, dynamodb_tables, mailbox, ws):
        _seed_agent(dynamodb_tables, "consumer", "carol")
        cc = _conn("consumer", "carol")
        result = await ex.post_job(cc, "T", "D", 0, "balanced")
        assert result["status"] == "rejected"
        assert result["reason"] == "bad_price"

    async def test_insufficient_funds_rejected_no_job(self, dynamodb_tables, mailbox, ws):
        _seed_agent(dynamodb_tables, "consumer", "carol")
        cc = _conn("consumer", "carol")
        result = await ex.post_job(cc, "T", "D", 500, "balanced")

        assert result["status"] == "rejected"
        assert result["reason"] == "insufficient_funds"
        resp = dynamodb_tables.Table("goosetown-jobs").scan()
        assert resp.get("Items", []) == []

    async def test_unknown_strategy_falls_back_to_balanced(self, dynamodb_tables, mailbox, ws):
        _seed_agent(dynamodb_tables, "consumer", "carol")
        cc = _conn("consumer", "carol")
        ws.conns["consumer"] = cc
        result = await ex.post_job(cc, "T", "D", 30, "yolo")
        assert result["status"] == "ok"
        assert _get_job(dynamodb_tables, result["job_id"])["strategy"] == "balanced"


# ---------------------------------------------------------------------------
# place_bid
# ---------------------------------------------------------------------------


class TestPlaceBid:
    async def test_two_bidders_both_stored(self, dynamodb_tables, mailbox, ws):
        cc, job_id = await _post(dynamodb_tables, ws)
        for aid, name in (("b1", "bea"), ("b2", "bob")):
            _seed_agent(dynamodb_tables, aid, name)
            ws.conns[aid] = _conn(aid, name, f"c-{aid}")

        r1 = await ex.place_bid(ws.conns["b1"], job_id, 20, 0.8, "pick me")
        r2 = await ex.place_bid(ws.conns["b2"], job_id, 25, 0.9, "no, me")
        assert r1["status"] == "ok"
        assert r2["status"] == "ok"

        bids = _get_job(dynamodb_tables, job_id)["bids"]
        assert set(bids.keys()) == {"b1", "b2"}
        assert int(bids["b1"]["price"]) == 20
        assert bids["b1"]["agentName"] == "bea"

    async def test_rebid_overwrites(self, dynamodb_tables, mailbox, ws):
        cc, job_id = await _post(dynamodb_tables, ws)
        _seed_agent(dynamodb_tables, "b1", "bea")
        ws.conns["b1"] = _conn("b1", "bea", "c-b1")

        await ex.place_bid(ws.conns["b1"], job_id, 20, 0.8, "first")
        await ex.place_bid(ws.conns["b1"], job_id, 15, 0.9, "cheaper")

        bids = _get_job(dynamodb_tables, job_id)["bids"]
        assert list(bids.keys()) == ["b1"]
        assert int(bids["b1"]["price"]) == 15

    async def test_own_job_rejected(self, dynamodb_tables, mailbox, ws):
        cc, job_id = await _post(dynamodb_tables, ws)
        result = await ex.place_bid(cc, job_id, 10, 0.5, "")
        assert result["status"] == "rejected"
        assert result["reason"] == "own_job"

    async def test_not_at_bank_rejected(self, dynamodb_tables, mailbox, ws):
        cc, job_id = await _post(dynamodb_tables, ws)
        _seed_agent(dynamodb_tables, "b1", "bea", location="cafe")
        result = await ex.place_bid(_conn("b1", "bea"), job_id, 10, 0.5, "")
        assert result["reason"] == "not_at_exchange"

    async def test_over_max_price_rejected(self, dynamodb_tables, mailbox, ws):
        cc, job_id = await _post(dynamodb_tables, ws, max_price=30)
        _seed_agent(dynamodb_tables, "b1", "bea")
        result = await ex.place_bid(_conn("b1", "bea"), job_id, 31, 0.5, "")
        assert result["reason"] == "bad_price"

    async def test_unknown_job_rejected(self, dynamodb_tables, mailbox, ws):
        _seed_agent(dynamodb_tables, "b1", "bea")
        result = await ex.place_bid(_conn("b1", "bea"), "nope", 10, 0.5, "")
        assert result["reason"] == "not_found"

    async def test_bid_after_close_rejected_not_open(self, dynamodb_tables, mailbox, ws):
        cc, job_id = await _post(dynamodb_tables, ws)
        await ex._close_bid_window(job_id)  # no bids -> EXPIRED
        _seed_agent(dynamodb_tables, "b1", "bea")
        result = await ex.place_bid(_conn("b1", "bea"), job_id, 10, 0.5, "")
        assert result["reason"] == "not_open"

    async def test_nan_confidence_rejected_bad_confidence(self, dynamodb_tables, mailbox, ws):
        cc, job_id = await _post(dynamodb_tables, ws)
        _seed_agent(dynamodb_tables, "b1", "bea")
        ws.conns["b1"] = _conn("b1", "bea", "c-b1")
        result = await ex.place_bid(ws.conns["b1"], job_id, 20, float("nan"), "pick me")
        assert result["status"] == "rejected"
        assert result["reason"] == "bad_confidence"

    async def test_confidence_above_one_clamped_to_one(self, dynamodb_tables, mailbox, ws):
        cc, job_id = await _post(dynamodb_tables, ws)
        _seed_agent(dynamodb_tables, "b1", "bea")
        ws.conns["b1"] = _conn("b1", "bea", "c-b1")
        result = await ex.place_bid(ws.conns["b1"], job_id, 20, 7.0, "pick me")
        assert result["status"] == "ok"
        bids = _get_job(dynamodb_tables, job_id)["bids"]
        assert float(bids["b1"]["confidence"]) == 1.0

    async def test_consumer_notified_bid_received_without_amount(self, dynamodb_tables, mailbox, ws):
        cc, job_id = await _post(dynamodb_tables, ws)
        _seed_agent(dynamodb_tables, "b1", "bea")
        ws.conns["b1"] = _conn("b1", "bea", "c-b1")
        mailbox.reset_mock()

        await ex.place_bid(ws.conns["b1"], job_id, 20, 0.8, "pick me")

        received = [c.args[1] for c in mailbox.notify.call_args_list if c.args[0] == "consumer"]
        assert len(received) == 1
        evt = received[0]
        assert evt["event"] == "bid_received"
        assert evt["bid_count"] == 1
        assert "price" not in evt  # sealed-bid: consumer never sees amounts
        evt_no_id = {k: v for k, v in evt.items() if k != "job_id"}
        assert "20" not in str(evt_no_id)  # bid price 20 must not leak (job_id excluded to avoid UUID false positives)


# ---------------------------------------------------------------------------
# cancel_job
# ---------------------------------------------------------------------------


class TestCancelJob:
    async def test_consumer_cancel_refunds(self, dynamodb_tables, mailbox, ws):
        cc, job_id = await _post(dynamodb_tables, ws, max_price=30)
        result = await ex.cancel_job(cc, job_id)

        assert result["status"] == "ok"
        assert _get_job(dynamodb_tables, job_id)["status"] == "CANCELLED"
        assert int(_get_agent(dynamodb_tables, "consumer")["walletBalance"]) == 100

    async def test_non_consumer_rejected(self, dynamodb_tables, mailbox, ws):
        cc, job_id = await _post(dynamodb_tables, ws)
        _seed_agent(dynamodb_tables, "b1", "bea")
        result = await ex.cancel_job(_conn("b1", "bea"), job_id)
        assert result["reason"] == "not_consumer"

    async def test_cancel_after_close_rejected(self, dynamodb_tables, mailbox, ws):
        cc, job_id = await _post(dynamodb_tables, ws)
        await ex._close_bid_window(job_id)
        result = await ex.cancel_job(cc, job_id)
        assert result["reason"] == "not_open"

    async def test_cancel_with_bids_notifies_bidders_not_consumer(self, dynamodb_tables, mailbox, ws):
        """Cancelling an OPEN job that has bids must send job_cancelled to every
        bidder; the consumer must NOT receive a job_cancelled notification."""
        cc, job_id = await _post(dynamodb_tables, ws, max_price=30)
        for aid, name in (("b1", "bea"), ("b2", "bob")):
            _seed_agent(dynamodb_tables, aid, name)
            ws.conns[aid] = _conn(aid, name, f"c-{aid}")
        await ex.place_bid(ws.conns["b1"], job_id, 20, 0.8, "pick me")
        await ex.place_bid(ws.conns["b2"], job_id, 25, 0.9, "no, me")
        mailbox.reset_mock()

        result = await ex.cancel_job(cc, job_id)

        assert result["status"] == "ok"
        events = _notified_events(mailbox)
        assert ("b1", "job_cancelled") in events
        assert ("b2", "job_cancelled") in events
        # Consumer gets exchange_ok from _notify_outcome, never job_cancelled.
        assert ("consumer", "job_cancelled") not in events

    async def test_cancel_no_bids_sends_no_job_cancelled(self, dynamodb_tables, mailbox, ws):
        """Cancelling a job with no bids sends no job_cancelled notifications."""
        cc, job_id = await _post(dynamodb_tables, ws, max_price=30)
        mailbox.reset_mock()

        result = await ex.cancel_job(cc, job_id)

        assert result["status"] == "ok"
        events = _notified_events(mailbox)
        cancelled_targets = [aid for aid, ev in events if ev == "job_cancelled"]
        assert cancelled_targets == []


# ---------------------------------------------------------------------------
# _close_bid_window
# ---------------------------------------------------------------------------


async def _post_with_two_bids(db, ws, mailbox, max_price=30):
    cc, job_id = await _post(db, ws, max_price=max_price)
    _seed_agent(db, "b1", "bea", trustScore=Decimal("0.9"))
    _seed_agent(db, "b2", "bob")
    ws.conns["b1"] = _conn("b1", "bea", "c-b1")
    ws.conns["b2"] = _conn("b2", "bob", "c-b2")
    await ex.place_bid(ws.conns["b1"], job_id, 20, 0.9, "experienced")
    await ex.place_bid(ws.conns["b2"], job_id, 20, 0.3, "newbie")
    mailbox.reset_mock()
    return cc, job_id


class TestCloseBidWindow:
    async def test_awarded_to_scoring_winner_with_partial_refund(self, dynamodb_tables, mailbox, ws):
        cc, job_id = await _post_with_two_bids(dynamodb_tables, ws, mailbox)
        await ex._close_bid_window(job_id)

        job = _get_job(dynamodb_tables, job_id)
        assert job["status"] == "AWARDED"
        assert job["providerId"] == "b1"  # higher trust + confidence at same price
        assert int(job["agreedPrice"]) == 20
        assert "deliveryDeadlineAt" in job
        # consumer escrowed 30, refunded 10 -> 100 - 20 = 80
        assert int(_get_agent(dynamodb_tables, "consumer")["walletBalance"]) == 80

    async def test_award_events_winner_consumer_losers(self, dynamodb_tables, mailbox, ws):
        cc, job_id = await _post_with_two_bids(dynamodb_tables, ws, mailbox)
        await ex._close_bid_window(job_id)

        events = _notified_events(mailbox)
        assert ("b1", "job_awarded") in events
        assert ("consumer", "job_awarded") in events
        assert ("b2", "bid_lost") in events
        assert ("b1", "bid_lost") not in events

    async def test_winner_gets_full_description_and_mood(self, dynamodb_tables, mailbox, ws):
        cc, job_id = await _post_with_two_bids(dynamodb_tables, ws, mailbox)
        await ex._close_bid_window(job_id)

        winner_events = [c.args[1] for c in mailbox.notify.call_args_list if c.args[0] == "b1"]
        awarded = [e for e in winner_events if e["event"] == "job_awarded"][0]
        assert awarded["description"] == "Compare three pixel-art tools."
        assert awarded["agreed_price"] == 20
        # mood "job_won" applied (+3)
        assert int(_get_agent(dynamodb_tables, "b1")["mood"]) == 3

    async def test_disconnected_bidder_disqualified(self, dynamodb_tables, mailbox, ws):
        cc, job_id = await _post_with_two_bids(dynamodb_tables, ws, mailbox)
        del ws.conns["b1"]  # bea (the would-be winner) disconnects
        await ex._close_bid_window(job_id)

        assert _get_job(dynamodb_tables, job_id)["providerId"] == "b2"

    async def test_no_bids_expires_with_full_refund(self, dynamodb_tables, mailbox, ws):
        cc, job_id = await _post(dynamodb_tables, ws, max_price=30)
        mailbox.reset_mock()
        await ex._close_bid_window(job_id)

        job = _get_job(dynamodb_tables, job_id)
        assert job["status"] == "EXPIRED"
        assert int(_get_agent(dynamodb_tables, "consumer")["walletBalance"]) == 100
        assert ("consumer", "job_expired") in _notified_events(mailbox)
        # mood job_expired_consumer (-2)
        assert int(_get_agent(dynamodb_tables, "consumer")["mood"]) == -2

    async def test_idempotent_on_non_open_job(self, dynamodb_tables, mailbox, ws):
        cc, job_id = await _post(dynamodb_tables, ws)
        await ex._close_bid_window(job_id)
        balance_before = int(_get_agent(dynamodb_tables, "consumer")["walletBalance"])
        await ex._close_bid_window(job_id)  # second fire — no double refund
        assert int(_get_agent(dynamodb_tables, "consumer")["walletBalance"]) == balance_before


# ---------------------------------------------------------------------------
# deliver_job
# ---------------------------------------------------------------------------


async def _run_to_awarded(db, ws, mailbox, max_price=30, bid_price=20):
    cc, job_id = await _post(db, ws, max_price=max_price)
    _seed_agent(db, "provider", "pete")
    pc = _conn("provider", "pete", "c-provider")
    ws.conns["provider"] = pc
    await ex.place_bid(pc, job_id, bid_price, 0.8, "on it")
    await ex._close_bid_window(job_id)
    assert _get_job(db, job_id)["status"] == "AWARDED"
    mailbox.reset_mock()
    return cc, pc, job_id


class TestDeliverJob:
    async def test_wrong_agent_rejected(self, dynamodb_tables, mailbox, ws):
        cc, pc, job_id = await _run_to_awarded(dynamodb_tables, ws, mailbox)
        result = await ex.deliver_job(cc, job_id, "not my job")
        assert result["reason"] == "not_provider"

    async def test_deliver_sets_delivered_and_notifies_consumer(self, dynamodb_tables, mailbox, ws):
        cc, pc, job_id = await _run_to_awarded(dynamodb_tables, ws, mailbox)
        result = await ex.deliver_job(pc, job_id, "Here is the research.")

        assert result["status"] == "ok"
        job = _get_job(dynamodb_tables, job_id)
        assert job["status"] == "DELIVERED"
        assert job["deliverable"] == "Here is the research."
        assert "reviewDeadlineAt" in job

        consumer_events = [c.args[1] for c in mailbox.notify.call_args_list if c.args[0] == "consumer"]
        delivered = [e for e in consumer_events if e["event"] == "job_delivered"][0]
        assert delivered["deliverable"] == "Here is the research."
        # job_delivered mood event: -10 energy
        assert int(_get_agent(dynamodb_tables, "provider")["energy"]) == 90

    async def test_deliverable_truncated(self, dynamodb_tables, mailbox, ws):
        cc, pc, job_id = await _run_to_awarded(dynamodb_tables, ws, mailbox)
        await ex.deliver_job(pc, job_id, "x" * 20000)
        assert len(_get_job(dynamodb_tables, job_id)["deliverable"]) == 16384

    async def test_double_deliver_rejected(self, dynamodb_tables, mailbox, ws):
        cc, pc, job_id = await _run_to_awarded(dynamodb_tables, ws, mailbox)
        await ex.deliver_job(pc, job_id, "v1")
        result = await ex.deliver_job(pc, job_id, "v2")
        assert result["reason"] == "not_awarded"


# ---------------------------------------------------------------------------
# review_job / settlement
# ---------------------------------------------------------------------------


async def _run_to_delivered(db, ws, mailbox, max_price=30, bid_price=20):
    cc, pc, job_id = await _run_to_awarded(db, ws, mailbox, max_price=max_price, bid_price=bid_price)
    await ex.deliver_job(pc, job_id, "the work")
    mailbox.reset_mock()
    return cc, pc, job_id


class TestReviewAndSettle:
    async def test_accept_completes_and_pays_provider(self, dynamodb_tables, mailbox, ws):
        cc, pc, job_id = await _run_to_delivered(dynamodb_tables, ws, mailbox)
        result = await ex.review_job(cc, job_id, True, "")

        assert result["status"] == "ok"
        assert result["outcome"] == "SUCCESS"
        job = _get_job(dynamodb_tables, job_id)
        assert job["status"] == "COMPLETED"
        assert job["outcome"] == "SUCCESS"

        fee = exchange_scoring.platform_fee(20)  # 2
        provider = _get_agent(dynamodb_tables, "provider")
        assert int(provider["walletBalance"]) == 100 + 20 - fee
        assert int(provider["jobsCompleted"]) == 1
        assert float(provider["trustScore"]) == pytest.approx(0.6)  # 0.8*0.5 + 0.2*1.0

        town_rows = _ledger_rows(dynamodb_tables, "TOWN")
        assert len(town_rows) == 1
        assert town_rows[0]["entryType"] == "FEE"
        assert int(town_rows[0]["amount"]) == fee

        events = _notified_events(mailbox)
        assert ("provider", "job_completed") in events
        assert ("consumer", "job_completed") in events

        # Fix 3: provider's copy must carry updated balance; consumer's must not.
        provider_events = [c.args[1] for c in mailbox.notify.call_args_list if c.args[0] == "provider"]
        provider_completed = [e for e in provider_events if e["event"] == "job_completed"][0]
        assert "balance" in provider_completed
        assert provider_completed["balance"] == 100 + 20 - fee
        consumer_events = [c.args[1] for c in mailbox.notify.call_args_list if c.args[0] == "consumer"]
        consumer_completed = [e for e in consumer_events if e["event"] == "job_completed"][0]
        assert "balance" not in consumer_completed

    async def test_reject_fails_refunds_and_trust_down(self, dynamodb_tables, mailbox, ws):
        cc, pc, job_id = await _run_to_delivered(dynamodb_tables, ws, mailbox)
        result = await ex.review_job(cc, job_id, False, "sloppy work")

        assert result["status"] == "ok"
        assert result["outcome"] == "FAILURE_PROVIDER"
        job = _get_job(dynamodb_tables, job_id)
        assert job["status"] == "FAILED"

        # consumer paid 30 escrow, got 10 back at award, 20 back on reject -> 100
        assert int(_get_agent(dynamodb_tables, "consumer")["walletBalance"]) == 100
        provider = _get_agent(dynamodb_tables, "provider")
        assert int(provider["jobsFailed"]) == 1
        assert float(provider["trustScore"]) == pytest.approx(0.4)  # 0.8*0.5

        events = _notified_events(mailbox)
        assert ("provider", "job_failed") in events
        assert ("consumer", "job_failed") in events

        # Fix 3: consumer's copy must carry updated balance; provider's must not.
        consumer_events = [c.args[1] for c in mailbox.notify.call_args_list if c.args[0] == "consumer"]
        consumer_failed = [e for e in consumer_events if e["event"] == "job_failed"][0]
        assert "balance" in consumer_failed
        assert consumer_failed["balance"] == 100  # full refund restored to 100
        provider_events = [c.args[1] for c in mailbox.notify.call_args_list if c.args[0] == "provider"]
        provider_failed = [e for e in provider_events if e["event"] == "job_failed"][0]
        assert "balance" not in provider_failed

    async def test_non_consumer_review_rejected(self, dynamodb_tables, mailbox, ws):
        cc, pc, job_id = await _run_to_delivered(dynamodb_tables, ws, mailbox)
        result = await ex.review_job(pc, job_id, True, "")
        assert result["reason"] == "not_consumer"

    async def test_review_before_delivery_rejected(self, dynamodb_tables, mailbox, ws):
        cc, pc, job_id = await _run_to_awarded(dynamodb_tables, ws, mailbox)
        result = await ex.review_job(cc, job_id, True, "")
        assert result["reason"] == "not_delivered"

    async def test_review_job_accept_none_rejected_bad_accept_job_stays_delivered(self, dynamodb_tables, mailbox, ws):
        """accept=None (missing from unvalidated body) → rejected bad_accept; job stays DELIVERED."""
        cc, pc, job_id = await _run_to_delivered(dynamodb_tables, ws, mailbox)
        result = await ex.review_job(cc, job_id, None, "")
        assert result["status"] == "rejected"
        assert result["reason"] == "bad_accept"
        # Job must remain DELIVERED — no settlement occurred.
        assert _get_job(dynamodb_tables, job_id)["status"] == "DELIVERED"

    async def test_review_race_loss_returns_already_settled(self, dynamodb_tables, mailbox, ws):
        """If the job settles between review_job's status read and its flip
        (auto-accept timer winning the race), the reject must surface an
        honest already_settled rejection — no refund, no trust change."""
        cc, pc, job_id = await _run_to_delivered(dynamodb_tables, ws, mailbox)
        stale = ex._get_job(job_id)  # status DELIVERED, as review_job would see it
        await ex._review_timeout(job_id)  # auto-accept wins: job → COMPLETED
        provider_before = _get_agent(dynamodb_tables, "provider")
        consumer_before = _get_agent(dynamodb_tables, "consumer")
        mailbox.reset_mock()

        with patch.object(ex, "_get_job", return_value=stale):
            result = await ex.review_job(cc, job_id, False, "too slow")

        assert result["status"] == "rejected"
        assert result["reason"] == "already_settled"
        # Auto-accept outcome stands untouched.
        job = _get_job(dynamodb_tables, job_id)
        assert job["status"] == "COMPLETED"
        assert job["outcome"] == "SUCCESS_AUTO"
        # No refund, no double money movement, no trust change.
        provider_after = _get_agent(dynamodb_tables, "provider")
        consumer_after = _get_agent(dynamodb_tables, "consumer")
        assert provider_after["walletBalance"] == provider_before["walletBalance"]
        assert consumer_after["walletBalance"] == consumer_before["walletBalance"]
        assert provider_after["trustScore"] == provider_before["trustScore"]
        assert consumer_after.get("rejectionsIssued") == consumer_before.get("rejectionsIssued")
        # Only the rejection echo went out — no job_failed events.
        events = _notified_events(mailbox)
        assert events == [("consumer", "exchange_rejected")]

    async def test_review_timeout_auto_accepts(self, dynamodb_tables, mailbox, ws):
        cc, pc, job_id = await _run_to_delivered(dynamodb_tables, ws, mailbox)
        await ex._review_timeout(job_id)

        job = _get_job(dynamodb_tables, job_id)
        assert job["status"] == "COMPLETED"
        assert job["outcome"] == "SUCCESS_AUTO"
        provider = _get_agent(dynamodb_tables, "provider")
        assert float(provider["trustScore"]) == pytest.approx(0.58)  # 0.8*0.5 + 0.2*0.9

    async def test_delivery_timeout_fails_with_refund_and_trust_hit(self, dynamodb_tables, mailbox, ws):
        cc, pc, job_id = await _run_to_awarded(dynamodb_tables, ws, mailbox)
        await ex._delivery_timeout(job_id)

        job = _get_job(dynamodb_tables, job_id)
        assert job["status"] == "FAILED"
        assert job["outcome"] == "FAILURE_PROVIDER"
        assert int(_get_agent(dynamodb_tables, "consumer")["walletBalance"]) == 100
        provider = _get_agent(dynamodb_tables, "provider")
        assert int(provider["jobsFailed"]) == 1
        assert float(provider["trustScore"]) == pytest.approx(0.4)

        events = _notified_events(mailbox)
        assert ("provider", "job_failed") in events
        assert ("consumer", "job_failed") in events


# ---------------------------------------------------------------------------
# Sweeper
# ---------------------------------------------------------------------------


class TestSweeper:
    async def test_sweep_settles_overdue_open_and_awarded(self, dynamodb_tables, mailbox, ws):
        _seed_agent(dynamodb_tables, "consumer", "carol", walletBalance=100, signupGrantAt="x")
        _seed_agent(dynamodb_tables, "provider", "pete", walletBalance=100, signupGrantAt="x")
        past = Decimal(str(time.time() - 60))
        jobs = dynamodb_tables.Table("goosetown-jobs")
        jobs.put_item(
            Item={
                "id": "job-open",
                "consumerId": "consumer",
                "consumerName": "carol",
                "title": "stale open",
                "description": "d",
                "maxPrice": 10,
                "strategy": "balanced",
                "status": "OPEN",
                "bids": {},
                "createdAt": "2026-06-12T00:00:00+00:00",
                "bidWindowEndsAt": past,
            }
        )
        jobs.put_item(
            Item={
                "id": "job-awarded",
                "consumerId": "consumer",
                "consumerName": "carol",
                "title": "stale awarded",
                "description": "d",
                "maxPrice": 10,
                "strategy": "balanced",
                "status": "AWARDED",
                "bids": {},
                "providerId": "provider",
                "providerName": "pete",
                "agreedPrice": 10,
                "createdAt": "2026-06-12T00:00:01+00:00",
                "deliveryDeadlineAt": past,
            }
        )

        await ex.sweep_overdue_jobs()

        assert _get_job(dynamodb_tables, "job-open")["status"] == "EXPIRED"
        assert _get_job(dynamodb_tables, "job-awarded")["status"] == "FAILED"

    async def test_sweep_ignores_jobs_within_deadline(self, dynamodb_tables, mailbox, ws):
        _seed_agent(dynamodb_tables, "consumer", "carol", walletBalance=100, signupGrantAt="x")
        future = Decimal(str(time.time() + 600))
        dynamodb_tables.Table("goosetown-jobs").put_item(
            Item={
                "id": "job-fresh",
                "consumerId": "consumer",
                "consumerName": "carol",
                "title": "fresh",
                "description": "d",
                "maxPrice": 10,
                "strategy": "balanced",
                "status": "OPEN",
                "bids": {},
                "createdAt": "2026-06-12T00:00:00+00:00",
                "bidWindowEndsAt": future,
            }
        )
        await ex.sweep_overdue_jobs()
        assert _get_job(dynamodb_tables, "job-fresh")["status"] == "OPEN"

    async def test_sweep_empty_tables_noop(self, dynamodb_tables, mailbox, ws):
        await ex.sweep_overdue_jobs()  # must not raise


# ---------------------------------------------------------------------------
# Ledger invariant + board
# ---------------------------------------------------------------------------


class TestLedgerInvariantAndBoard:
    async def test_happy_path_ledger_nets(self, dynamodb_tables, mailbox, ws):
        cc, pc, job_id = await _run_to_delivered(dynamodb_tables, ws, mailbox, max_price=30, bid_price=20)
        await ex.review_job(cc, job_id, True, "")

        fee = exchange_scoring.platform_fee(20)

        def net(agent_id: str) -> int:
            return sum(int(r["amount"]) for r in _ledger_rows(dynamodb_tables, agent_id) if r["entryType"] != "GRANT")

        assert net("consumer") == -20
        assert net("provider") == 20 - fee
        assert net("TOWN") == fee

    async def test_list_board_jobs_shape(self, dynamodb_tables, mailbox, ws):
        cc, job_id = await _post(dynamodb_tables, ws)
        board = ex.list_board_jobs()

        assert len(board) == 1
        entry = board[0]
        assert entry["job_id"] == job_id
        assert entry["status"] == "OPEN"
        assert entry["consumer_name"] == "carol"
        assert entry["bid_count"] == 0
        assert len(entry["description"]) <= 200
        assert "deliverable" not in entry
        assert "bids" not in entry

    async def test_list_board_includes_settled(self, dynamodb_tables, mailbox, ws):
        cc, pc, job_id = await _run_to_delivered(dynamodb_tables, ws, mailbox)
        await ex.review_job(cc, job_id, True, "")
        board = ex.list_board_jobs()
        assert [e["status"] for e in board] == ["COMPLETED"]


# ---------------------------------------------------------------------------
# Escrow compensation on job-write failure (Fix 2)
# ---------------------------------------------------------------------------


class TestEscrowCompensationOnJobWriteFailure:
    async def test_job_write_failure_refunds_escrow_and_returns_rejected(self, dynamodb_tables, mailbox, ws):
        _seed_agent(dynamodb_tables, "consumer", "carol")
        cc = _conn("consumer", "carol", "c-consumer")
        ws.conns["consumer"] = cc

        # Patch jobs_table().put_item to raise on the first call (the job write).
        class _FailingTable:
            def put_item(self, **kwargs):
                raise RuntimeError("DynamoDB write failed (simulated)")

        def _patched_jobs_table():
            return _FailingTable()

        with patch.object(ex.dynamo, "jobs_table", _patched_jobs_table):
            result = await ex.post_job(cc, "Test job", "Description.", 20, "balanced")

        # Result must be a rejection with reason internal_error.
        assert result["status"] == "rejected"
        assert result["reason"] == "internal_error"

        # Consumer wallet must be back to 100 (debit reversed by ESCROW_REFUND).
        agent = _get_agent(dynamodb_tables, "consumer")
        assert int(agent["walletBalance"]) == 100

        # Ledger: ESCROW_DEBIT followed by ESCROW_REFUND — net zero (excluding GRANT).
        rows = _ledger_rows(dynamodb_tables, "consumer")
        non_grant = [r for r in rows if r["entryType"] != "GRANT"]
        types = [r["entryType"] for r in non_grant]
        assert "ESCROW_DEBIT" in types
        assert "ESCROW_REFUND" in types


# ---------------------------------------------------------------------------
# rejectionsIssued counter (Fix 4)
# ---------------------------------------------------------------------------


class TestRejectionsIssuedCounter:
    async def test_consumer_reject_increments_rejections_issued(self, dynamodb_tables, mailbox, ws):
        cc, pc, job_id = await _run_to_delivered(dynamodb_tables, ws, mailbox)
        await ex.review_job(cc, job_id, False, "sloppy work")

        consumer = _get_agent(dynamodb_tables, "consumer")
        assert int(consumer.get("rejectionsIssued", 0)) == 1

    async def test_delivery_timeout_does_not_increment_rejections_issued(self, dynamodb_tables, mailbox, ws):
        cc, pc, job_id = await _run_to_awarded(dynamodb_tables, ws, mailbox)
        await ex._delivery_timeout(job_id)

        consumer = _get_agent(dynamodb_tables, "consumer")
        assert "rejectionsIssued" not in consumer


# ---------------------------------------------------------------------------
# Trust EMA optimistic concurrency (codex P2 on PR #386)
# ---------------------------------------------------------------------------


class TestTrustUpdateOptimisticConcurrency:
    def test_conflict_retries_and_lands_compounded_ema(self, dynamodb_tables, monkeypatch):
        """A conditional-check conflict on the first write must trigger a re-read,
        so the final trust reflects BOTH outcomes (no lost update)."""
        agents = dynamodb_tables.Table("goosetown-agents")
        agents.put_item(Item={"id": "prov-1", "agentName": "prov", "trustScore": Decimal("0.5")})

        real_table = ex.dynamo.agents_table()
        calls = {"n": 0}

        class RacingTable:
            """First update_item simulates a concurrent writer winning the race."""

            meta = real_table.meta

            def __getattr__(self, name):
                return getattr(real_table, name)

            def update_item(self, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    # Another task's settle landed in between: trust moved 0.5 -> 0.6.
                    real_table.update_item(
                        Key={"id": "prov-1"},
                        UpdateExpression="SET trustScore = :t",
                        ExpressionAttributeValues={":t": Decimal("0.6")},
                    )
                return real_table.update_item(**kwargs)

        monkeypatch.setattr(ex.dynamo, "agents_table", lambda: RacingTable())
        ex._update_trust("prov-1", "SUCCESS", completed_delta=0, failed_delta=0)

        item = agents.get_item(Key={"id": "prov-1"})["Item"]
        # Second attempt re-read 0.6 and compounded: 0.8*0.6 + 0.2*1.0 = 0.68.
        assert float(item["trustScore"]) == pytest.approx(0.68)
        assert calls["n"] >= 2  # first write conflicted, retry landed


# ---------------------------------------------------------------------------
# Race: bids that land between _get_job read and status flip (codex P2)
# ---------------------------------------------------------------------------


class TestLateRacingBidNotifications:
    """Verify that a bid committed atomically between the pre-read snapshot and
    the OPEN→terminal status flip receives the correct terminal notification.

    Deterministic approach: monkeypatch ``_get_job`` to return a snapshot that
    does NOT include the late bidder, while the real DynamoDB table already
    contains the late bid (written directly).  The code under test will flip the
    status and iterate the post-flip ALL_NEW bids map — which DOES include the
    late bid — so the terminal event must reach the late bidder.
    """

    async def test_late_bid_before_award_flip_gets_bid_lost(self, dynamodb_tables, mailbox, ws):
        """AWARDED path: snapshot has bidder A only; real table has A + late B.
        After _close_bid_window, B must receive bid_lost and A's award flow is unaffected."""
        cc, job_id = await _post(dynamodb_tables, ws, max_price=30)
        # Seed two agents — A will be in the snapshot, B is the late racer.
        _seed_agent(dynamodb_tables, "bid-a", "alice", trustScore=Decimal("0.9"))
        _seed_agent(dynamodb_tables, "bid-b", "brenda")
        ws.conns["bid-a"] = _conn("bid-a", "alice", "c-a")
        ws.conns["bid-b"] = _conn("bid-b", "brenda", "c-b")

        # Write A's bid through the normal path so the job item is consistent.
        await ex.place_bid(ws.conns["bid-a"], job_id, 20, 0.9, "experienced")

        # Write B's bid directly to DynamoDB (simulating the race: B's conditional
        # write succeeded while the close path was between _get_job and _flip_status).
        dynamodb_tables.Table("goosetown-jobs").update_item(
            Key={"id": job_id},
            UpdateExpression="SET bids.#aid = :bid",
            ExpressionAttributeNames={"#aid": "bid-b"},
            ExpressionAttributeValues={
                ":bid": {
                    "price": 25,
                    "confidence": Decimal("0.5"),
                    "pitch": "late racer",
                    "bidAt": "2026-06-12T00:00:00+00:00",
                    "agentName": "brenda",
                }
            },
        )

        # Snapshot visible to _close_bid_window excludes bid-b (simulates the race).
        real_get_job = ex._get_job(job_id)
        snapshot_without_b = {**real_get_job, "bids": {k: v for k, v in real_get_job["bids"].items() if k != "bid-b"}}

        mailbox.reset_mock()
        with patch.object(ex, "_get_job", return_value=snapshot_without_b):
            await ex._close_bid_window(job_id)

        events = _notified_events(mailbox)
        # bid-a wins the award (only bidder in the snapshot live_bids).
        assert ("bid-a", "job_awarded") in events
        assert ("consumer", "job_awarded") in events
        # bid-b (the late racer) must receive bid_lost from the post-flip iteration.
        assert ("bid-b", "bid_lost") in events
        # bid-a must NOT receive bid_lost.
        assert ("bid-a", "bid_lost") not in events

    async def test_late_bid_before_expired_flip_gets_bid_lost(self, dynamodb_tables, mailbox, ws):
        """EXPIRED path: snapshot shows no bids; real table has one late bid.
        After _close_bid_window, late bidder must receive bid_lost."""
        cc, job_id = await _post(dynamodb_tables, ws, max_price=30)
        _seed_agent(dynamodb_tables, "bid-late", "lara")
        ws.conns["bid-late"] = _conn("bid-late", "lara", "c-late")

        # Write the late bid directly to DynamoDB (race: bid committed before flip).
        dynamodb_tables.Table("goosetown-jobs").update_item(
            Key={"id": job_id},
            UpdateExpression="SET bids.#aid = :bid",
            ExpressionAttributeNames={"#aid": "bid-late"},
            ExpressionAttributeValues={
                ":bid": {
                    "price": 10,
                    "confidence": Decimal("0.7"),
                    "pitch": "too late?",
                    "bidAt": "2026-06-12T00:00:00+00:00",
                    "agentName": "lara",
                }
            },
        )

        # Snapshot has no bids (window appeared empty to the close path).
        real_get_job = ex._get_job(job_id)
        snapshot_no_bids = {**real_get_job, "bids": {}}

        mailbox.reset_mock()
        with patch.object(ex, "_get_job", return_value=snapshot_no_bids):
            await ex._close_bid_window(job_id)

        job = _get_job(dynamodb_tables, job_id)
        assert job["status"] == "EXPIRED"
        events = _notified_events(mailbox)
        # Consumer gets job_expired.
        assert ("consumer", "job_expired") in events
        # Late bidder must get bid_lost via post-flip ALL_NEW iteration.
        assert ("bid-late", "bid_lost") in events

    async def test_late_bid_before_cancel_flip_gets_job_cancelled(self, dynamodb_tables, mailbox, ws):
        """cancel_job race: snapshot excludes late bidder; real table includes them.
        Late bidder must receive job_cancelled after the flip."""
        cc, job_id = await _post(dynamodb_tables, ws, max_price=30)
        _seed_agent(dynamodb_tables, "bid-c", "caitlin")
        ws.conns["bid-c"] = _conn("bid-c", "caitlin", "c-c")

        # Write the racing bid directly (simulates commit between _get_job and flip).
        dynamodb_tables.Table("goosetown-jobs").update_item(
            Key={"id": job_id},
            UpdateExpression="SET bids.#aid = :bid",
            ExpressionAttributeNames={"#aid": "bid-c"},
            ExpressionAttributeValues={
                ":bid": {
                    "price": 15,
                    "confidence": Decimal("0.6"),
                    "pitch": "racing in",
                    "bidAt": "2026-06-12T00:00:00+00:00",
                    "agentName": "caitlin",
                }
            },
        )

        # Snapshot returned to cancel_job has no bids (race).
        real_job = ex._get_job(job_id)
        snapshot_no_bids = {**real_job, "bids": {}}

        mailbox.reset_mock()
        with patch.object(ex, "_get_job", return_value=snapshot_no_bids):
            result = await ex.cancel_job(cc, job_id)

        assert result["status"] == "ok"
        job = _get_job(dynamodb_tables, job_id)
        assert job["status"] == "CANCELLED"
        events = _notified_events(mailbox)
        # The late racer must receive job_cancelled via post-flip iteration.
        assert ("bid-c", "job_cancelled") in events
        # Consumer must NOT receive job_cancelled (only exchange_ok from _notify_outcome).
        assert ("consumer", "job_cancelled") not in events


# ---------------------------------------------------------------------------
# Revision-guarded close: late bids are SCORED, not just notified (codex P2)
# ---------------------------------------------------------------------------


class TestBidRevisionGuard:
    """``place_bid`` bumps ``bidsRev`` atomically with the bid; ``_close_bid_window``
    guards its status flip on the revision it scored and retries on mismatch, so a
    bid that commits between the close path's read and its flip participates in
    the award instead of being silently dropped from scoring."""

    @staticmethod
    def _stale_first_read(monkeypatch, stale: dict) -> dict:
        """Patch ex._get_job so the FIRST call returns ``stale``; later calls hit
        the real table. Returns the call counter dict."""
        real_get_job = ex._get_job
        calls = {"n": 0}

        def fake(job_id):
            calls["n"] += 1
            if calls["n"] == 1:
                return stale
            return real_get_job(job_id)

        monkeypatch.setattr(ex, "_get_job", fake)
        return calls

    async def test_retry_scores_late_bid_and_awards_it(self, dynamodb_tables, mailbox, ws, monkeypatch):
        """Stale snapshot has bidder A only; the real table holds A + late bidder B
        (strictly better: lower price, same confidence). The guarded flip must
        fail, the close must retry on fresh data, and B must win the award."""
        cc, job_id = await _post(dynamodb_tables, ws, max_price=30)
        for aid, name in (("bid-a", "alice"), ("bid-b", "brenda")):
            _seed_agent(dynamodb_tables, aid, name)
            ws.conns[aid] = _conn(aid, name, f"c-{aid}")

        await ex.place_bid(ws.conns["bid-a"], job_id, 20, 0.9, "first in")
        stale = ex._get_job(job_id)  # bidsRev=1, bids={A}
        # Late racer commits via the REAL bid path (bumps bidsRev to 2).
        await ex.place_bid(ws.conns["bid-b"], job_id, 10, 0.9, "late but cheaper")

        calls = self._stale_first_read(monkeypatch, stale)
        mailbox.reset_mock()
        await ex._close_bid_window(job_id)

        job = _get_job(dynamodb_tables, job_id)
        assert job["status"] == "AWARDED"
        assert job["providerId"] == "bid-b"  # late bid was scored and won
        assert int(job["agreedPrice"]) == 10
        assert calls["n"] >= 3  # stale read, post-failure re-read, retry read
        events = _notified_events(mailbox)
        assert ("bid-b", "job_awarded") in events
        assert ("bid-a", "bid_lost") in events
        assert ("bid-b", "bid_lost") not in events

    async def test_rebid_race_awards_at_reread_price(self, dynamodb_tables, mailbox, ws, monkeypatch):
        """A bids 50 then rebids 10 (bidsRev bumps twice). A stale snapshot from
        the first bid must NOT settle at 50 — the guard fails, the close re-reads,
        and the award uses the rebid price."""
        cc, job_id = await _post(dynamodb_tables, ws, max_price=60)
        _seed_agent(dynamodb_tables, "bid-a", "alice")
        ws.conns["bid-a"] = _conn("bid-a", "alice", "c-a")

        await ex.place_bid(ws.conns["bid-a"], job_id, 50, 0.8, "opening offer")
        stale = ex._get_job(job_id)  # bidsRev=1, price 50
        await ex.place_bid(ws.conns["bid-a"], job_id, 10, 0.8, "actually cheaper")  # bidsRev=2

        calls = self._stale_first_read(monkeypatch, stale)
        mailbox.reset_mock()
        await ex._close_bid_window(job_id)

        job = _get_job(dynamodb_tables, job_id)
        assert job["status"] == "AWARDED"
        assert job["providerId"] == "bid-a"
        assert int(job["agreedPrice"]) == 10  # re-read won; stale 50 never settled
        assert calls["n"] >= 3
        # Escrow 60, agreed 10 -> 50 refunded: consumer back to 90.
        assert int(_get_agent(dynamodb_tables, "consumer")["walletBalance"]) == 90


# ---------------------------------------------------------------------------
# Persisted-deadline enforcement in verb paths (codex P2)
# ---------------------------------------------------------------------------


class TestPersistedDeadlineEnforcement:
    """Verb paths must enforce persisted deadline attrs, not just rely on the
    in-memory timer — so they're correct after a backend restart."""

    # ------------------------------------------------------------------
    # place_bid: past bidWindowEndsAt → window_closed rejection + settle
    # ------------------------------------------------------------------

    async def test_place_bid_past_deadline_rejected_window_closed(self, dynamodb_tables, mailbox, ws):
        """place_bid after bidWindowEndsAt has passed returns window_closed and
        drives _close_bid_window inline (job settles to AWARDED or EXPIRED)."""
        cc, job_id = await _post(dynamodb_tables, ws, max_price=30)
        # Seed a connected bidder so the window closes to AWARDED (not EXPIRED).
        _seed_agent(dynamodb_tables, "b1", "bea")
        ws.conns["b1"] = _conn("b1", "bea", "c-b1")
        await ex.place_bid(ws.conns["b1"], job_id, 20, 0.8, "first bid")
        # Backdate the persisted deadline to the past (no timer fires).
        dynamodb_tables.Table("goosetown-jobs").update_item(
            Key={"id": job_id},
            UpdateExpression="SET bidWindowEndsAt = :past",
            ExpressionAttributeValues={":past": Decimal(str(time.time() - 60))},
        )
        mailbox.reset_mock()

        # A second bidder tries to bid — should be rejected because window is past.
        _seed_agent(dynamodb_tables, "b2", "bob")
        ws.conns["b2"] = _conn("b2", "bob", "c-b2")
        result = await ex.place_bid(ws.conns["b2"], job_id, 15, 0.9, "late bid")

        # (a) Rejection reason is window_closed.
        assert result["status"] == "rejected"
        assert result["reason"] == "window_closed"
        # (b) Overdue transition fired: job is now settled (AWARDED because b1 was live).
        job = _get_job(dynamodb_tables, job_id)
        assert job["status"] in ("AWARDED", "EXPIRED")

    async def test_place_bid_past_deadline_no_bids_job_expires(self, dynamodb_tables, mailbox, ws):
        """If there are no live bids when place_bid triggers the overdue close,
        the job must expire and the consumer must be refunded."""
        cc, job_id = await _post(dynamodb_tables, ws, max_price=30)
        # Backdate the deadline; no bids have been placed.
        dynamodb_tables.Table("goosetown-jobs").update_item(
            Key={"id": job_id},
            UpdateExpression="SET bidWindowEndsAt = :past",
            ExpressionAttributeValues={":past": Decimal(str(time.time() - 60))},
        )
        mailbox.reset_mock()

        _seed_agent(dynamodb_tables, "b1", "bea")
        ws.conns["b1"] = _conn("b1", "bea", "c-b1")
        result = await ex.place_bid(ws.conns["b1"], job_id, 20, 0.8, "too late")

        assert result["status"] == "rejected"
        assert result["reason"] == "window_closed"
        # Job must be EXPIRED and consumer refunded.
        assert _get_job(dynamodb_tables, job_id)["status"] == "EXPIRED"
        assert int(_get_agent(dynamodb_tables, "consumer")["walletBalance"]) == 100

    async def test_place_bid_future_deadline_accepted(self, dynamodb_tables, mailbox, ws):
        """A bid placed while the persisted deadline is still in the future must
        succeed normally — deadline guard must not fire for future deadlines."""
        cc, job_id = await _post(dynamodb_tables, ws, max_price=30)
        _seed_agent(dynamodb_tables, "b1", "bea")
        ws.conns["b1"] = _conn("b1", "bea", "c-b1")
        result = await ex.place_bid(ws.conns["b1"], job_id, 20, 0.8, "on time")
        assert result["status"] == "ok"

    # ------------------------------------------------------------------
    # deliver_job: past deliveryDeadlineAt → deadline_passed + FAILED
    # ------------------------------------------------------------------

    async def test_deliver_job_past_deadline_rejected_deadline_passed(self, dynamodb_tables, mailbox, ws):
        """deliver_job after deliveryDeadlineAt has passed returns deadline_passed,
        fires _delivery_timeout inline, and leaves the job as FAILED with the
        consumer refunded."""
        cc, pc, job_id = await _run_to_awarded(dynamodb_tables, ws, mailbox)
        # Backdate the delivery deadline.
        dynamodb_tables.Table("goosetown-jobs").update_item(
            Key={"id": job_id},
            UpdateExpression="SET deliveryDeadlineAt = :past",
            ExpressionAttributeValues={":past": Decimal(str(time.time() - 60))},
        )
        mailbox.reset_mock()

        result = await ex.deliver_job(pc, job_id, "too late delivery")

        # (a) Rejection reason is deadline_passed.
        assert result["status"] == "rejected"
        assert result["reason"] == "deadline_passed"
        # (b) Overdue transition fired: job is now FAILED.
        job = _get_job(dynamodb_tables, job_id)
        assert job["status"] == "FAILED"
        # (b) Consumer was refunded the agreed price.
        assert int(_get_agent(dynamodb_tables, "consumer")["walletBalance"]) == 100

    async def test_deliver_job_future_deadline_accepted(self, dynamodb_tables, mailbox, ws):
        """deliver_job within the delivery deadline must succeed normally."""
        cc, pc, job_id = await _run_to_awarded(dynamodb_tables, ws, mailbox)
        result = await ex.deliver_job(pc, job_id, "on-time delivery")
        assert result["status"] == "ok"
        assert _get_job(dynamodb_tables, job_id)["status"] == "DELIVERED"

    # ------------------------------------------------------------------
    # review_job: past reviewDeadlineAt → review_window_closed + COMPLETED
    # ------------------------------------------------------------------

    async def test_review_job_past_deadline_rejected_review_window_closed(self, dynamodb_tables, mailbox, ws):
        """review_job after reviewDeadlineAt has passed returns review_window_closed,
        fires _review_timeout inline (auto-accept), and leaves the job as COMPLETED
        with the provider paid — NOT FAILED."""
        cc, pc, job_id = await _run_to_delivered(dynamodb_tables, ws, mailbox)
        # Backdate the review deadline.
        dynamodb_tables.Table("goosetown-jobs").update_item(
            Key={"id": job_id},
            UpdateExpression="SET reviewDeadlineAt = :past",
            ExpressionAttributeValues={":past": Decimal(str(time.time() - 60))},
        )
        mailbox.reset_mock()

        result = await ex.review_job(cc, job_id, False, "too slow")

        # (a) Rejection reason is review_window_closed.
        assert result["status"] == "rejected"
        assert result["reason"] == "review_window_closed"
        # (b) Auto-accept fired: job is COMPLETED (not FAILED), provider paid.
        job = _get_job(dynamodb_tables, job_id)
        assert job["status"] == "COMPLETED"
        assert job["outcome"] == "SUCCESS_AUTO"
        # (b) Provider received the payout.
        fee = exchange_scoring.platform_fee(20)
        assert int(_get_agent(dynamodb_tables, "provider")["walletBalance"]) == 100 + 20 - fee

    async def test_review_job_future_deadline_accepted(self, dynamodb_tables, mailbox, ws):
        """review_job within the review window must succeed normally."""
        cc, pc, job_id = await _run_to_delivered(dynamodb_tables, ws, mailbox)
        result = await ex.review_job(cc, job_id, True, "")
        assert result["status"] == "ok"
        assert _get_job(dynamodb_tables, job_id)["status"] == "COMPLETED"
