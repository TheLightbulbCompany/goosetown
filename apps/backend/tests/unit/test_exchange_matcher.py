"""Tests for the Wave 2 synchronous offer matcher (Task 4, spec §3.3, codex P1).

``post_job`` runs the subscription matcher **synchronously in-request** and
persists the matched provider ids onto the job as a durable ``offeredTo`` String
Set — the load-bearing authorization record (codex P1: NOT an async event
consumer). Notifications (``job_offered``) are best-effort fan-out on top of that
durable state; a notify failure must not strand the offer or break the post.

The old all-connections broadcast is gone — offers go ONLY to matched
subscribers. A job with no subscribers still lands on the public board (the
pull path), it just isn't pushed.

Patterns (moto + stubbed mailbox/ws) mirror ``test_exchange_lifecycle.py``.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.services import exchange_service as ex
from core.services.provider_directory import ProviderDirectory

# ---------------------------------------------------------------------------
# Fixtures & helpers (lifted from test_exchange_lifecycle.py)
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


def _offered_to(db, job_id: str) -> set[str]:
    """The job's persisted offeredTo set (empty set when unset)."""
    raw = _get_job(db, job_id).get("offeredTo")
    return set(raw) if raw else set()


def _offer_targets(mailbox) -> list[str]:
    """agent_ids that got a job_offered notify."""
    return [c.args[0] for c in mailbox.notify.call_args_list if c.args[1].get("event") == "job_offered"]


def _register_provider(db, agent_id: str, name: str, categories: list[str], **extra) -> None:
    _seed_agent(db, agent_id, name, **extra)
    ProviderDirectory().register(agent_id, skills=[name], categories=categories, bio=f"{name}'s bio")


async def _post(db, ws, consumer_id="consumer", consumer_name="carol", *, category=None, max_price=30, **kwargs):
    """Seed + connect the consumer, then post a job. Returns (conn, job_id).

    Skips re-seeding when the consumer row already exists (e.g. the consumer
    was also registered as a provider for the own-offer-exclusion case).
    """
    if db.Table("goosetown-agents").get_item(Key={"id": consumer_id}).get("Item") is None:
        _seed_agent(db, consumer_id, consumer_name)
    cc = _conn(consumer_id, consumer_name, f"c-{consumer_id}")
    ws.conns[consumer_id] = cc
    result = await ex.post_job(
        cc,
        "Research tools",
        "Compare three pixel-art tools.",
        max_price,
        "balanced",
        category=category,
        **kwargs,
    )
    assert result["status"] == "ok", result
    return cc, result["job_id"]


# ---------------------------------------------------------------------------
# subscribers_for_job — the new ProviderDirectory method
# ---------------------------------------------------------------------------


class TestSubscribersForJob:
    def test_excludes_the_consumer_even_if_subscribed(self, dynamodb_tables):
        _register_provider(dynamodb_tables, "p1", "pat", ["writing"])
        _register_provider(dynamodb_tables, "consumer", "carol", ["writing"])
        ids = ProviderDirectory().subscribers_for_job("writing", consumer_id="consumer")
        assert "p1" in ids
        assert "consumer" not in ids

    def test_returns_only_category_subscribers(self, dynamodb_tables):
        _register_provider(dynamodb_tables, "p1", "pat", ["writing"])
        _register_provider(dynamodb_tables, "p2", "pam", ["research"])
        ids = ProviderDirectory().subscribers_for_job("writing", consumer_id="x")
        assert ids == ["p1"]

    def test_wildcard_parent_subscriber_is_matched(self, dynamodb_tables):
        # provider subscribed to PARENT `technology` is offered a CHILD job.
        _register_provider(dynamodb_tables, "p1", "pat", ["technology"])
        ids = ProviderDirectory().subscribers_for_job("technology.code-review", consumer_id="x")
        assert ids == ["p1"]

    def test_child_subscriber_not_matched_for_parent_job(self, dynamodb_tables):
        _register_provider(dynamodb_tables, "p1", "pat", ["technology.code-review"])
        ids = ProviderDirectory().subscribers_for_job("technology", consumer_id="x")
        assert ids == []

    def test_min_trust_filters_low_trust_providers(self, dynamodb_tables):
        _register_provider(dynamodb_tables, "low", "lola", ["writing"], trustScore=Decimal("0.3"))
        _register_provider(dynamodb_tables, "high", "hank", ["writing"], trustScore=Decimal("0.9"))
        ids = ProviderDirectory().subscribers_for_job("writing", consumer_id="x", min_trust=0.5)
        assert ids == ["high"]

    def test_min_trust_zero_keeps_everyone(self, dynamodb_tables):
        _register_provider(dynamodb_tables, "low", "lola", ["writing"], trustScore=Decimal("0.3"))
        _register_provider(dynamodb_tables, "high", "hank", ["writing"], trustScore=Decimal("0.9"))
        ids = ProviderDirectory().subscribers_for_job("writing", consumer_id="x", min_trust=0.0)
        assert set(ids) == {"low", "high"}


# ---------------------------------------------------------------------------
# post_job — synchronous offers writing offeredTo
# ---------------------------------------------------------------------------


class TestPostJobOffers:
    async def test_only_subscriber_offered_and_notified(self, dynamodb_tables, mailbox, ws):
        _register_provider(dynamodb_tables, "sub", "sue", ["writing"])
        _register_provider(dynamodb_tables, "nonsub", "ned", ["research"])
        _, job_id = await _post(dynamodb_tables, ws, category="writing")

        assert _offered_to(dynamodb_tables, job_id) == {"sub"}
        targets = _offer_targets(mailbox)
        assert targets == ["sub"]
        assert "nonsub" not in targets

    async def test_non_subscriber_not_offered(self, dynamodb_tables, mailbox, ws):
        _register_provider(dynamodb_tables, "nonsub", "ned", ["research"])
        _, job_id = await _post(dynamodb_tables, ws, category="writing")
        assert _offered_to(dynamodb_tables, job_id) == set()
        assert _offer_targets(mailbox) == []

    async def test_consumer_excluded_from_own_offer(self, dynamodb_tables, mailbox, ws):
        # The consumer is itself a provider subscribed to the job's category.
        _register_provider(dynamodb_tables, "consumer", "carol", ["writing"])
        _register_provider(dynamodb_tables, "sub", "sue", ["writing"])
        _, job_id = await _post(dynamodb_tables, ws, category="writing")

        offered = _offered_to(dynamodb_tables, job_id)
        assert "consumer" not in offered
        assert offered == {"sub"}
        assert "consumer" not in _offer_targets(mailbox)

    async def test_category_defaults_to_general_when_omitted(self, dynamodb_tables, mailbox, ws):
        _, job_id = await _post(dynamodb_tables, ws, category=None)
        assert _get_job(dynamodb_tables, job_id)["category"] == "general"

    async def test_invalid_category_defaults_to_general(self, dynamodb_tables, mailbox, ws):
        _, job_id = await _post(dynamodb_tables, ws, category="NOT A SLUG!!")
        assert _get_job(dynamodb_tables, job_id)["category"] == "general"

    async def test_category_stored_normalized(self, dynamodb_tables, mailbox, ws):
        _, job_id = await _post(dynamodb_tables, ws, category="Technology.Code-Review")
        assert _get_job(dynamodb_tables, job_id)["category"] == "technology.code-review"

    async def test_wildcard_parent_provider_is_offered(self, dynamodb_tables, mailbox, ws):
        # Provider subscribed to the PARENT category is offered a CHILD-category job.
        _register_provider(dynamodb_tables, "parent_sub", "pam", ["technology"])
        _, job_id = await _post(dynamodb_tables, ws, category="technology.code-review")

        assert _offered_to(dynamodb_tables, job_id) == {"parent_sub"}
        assert _offer_targets(mailbox) == ["parent_sub"]

    async def test_offered_to_persisted_and_readable(self, dynamodb_tables, mailbox, ws):
        _register_provider(dynamodb_tables, "a", "aria", ["writing"])
        _register_provider(dynamodb_tables, "b", "bex", ["writing"])
        _, job_id = await _post(dynamodb_tables, ws, category="writing")
        # Read straight from DynamoDB — durable job state, not a fleeting event.
        assert _offered_to(dynamodb_tables, job_id) == {"a", "b"}

    async def test_no_subscribers_empty_offered_to_still_on_board(self, dynamodb_tables, mailbox, ws):
        _, job_id = await _post(dynamodb_tables, ws, category="writing")
        assert _offered_to(dynamodb_tables, job_id) == set()
        # Still appears on the public board (pull path) despite no push offers.
        board_ids = [j["job_id"] for j in ex.list_board_jobs()]
        assert job_id in board_ids

    async def test_result_carries_category_and_offered_count(self, dynamodb_tables, mailbox, ws):
        _register_provider(dynamodb_tables, "a", "aria", ["writing"])
        _register_provider(dynamodb_tables, "b", "bex", ["writing"])
        cc = _conn("consumer", "carol", "c-consumer")
        ws.conns["consumer"] = cc
        _seed_agent(dynamodb_tables, "consumer", "carol")
        result = await ex.post_job(cc, "T", "D", 30, "balanced", category="writing")
        assert result["status"] == "ok"
        assert result["category"] == "writing"
        assert result["offered_count"] == 2

    async def test_old_all_connections_broadcast_is_gone(self, dynamodb_tables, mailbox, ws):
        # A connected non-subscriber must NOT receive a broadcast — the
        # all-connections fan-out was replaced by the targeted offer loop.
        _seed_agent(dynamodb_tables, "bystander", "bart")
        ws.conns["bystander"] = _conn("bystander", "bart", "c-bystander")
        await _post(dynamodb_tables, ws, category="writing")
        # No notify_connection broadcast happened at all.
        assert mailbox.notify_connection.call_count == 0

    async def test_offer_event_payload_shape(self, dynamodb_tables, mailbox, ws):
        _register_provider(dynamodb_tables, "sub", "sue", ["writing"])
        _, job_id = await _post(
            dynamodb_tables,
            ws,
            category="writing",
            success_criteria=["must compare 3 tools"],
        )
        offer = next(c.args[1] for c in mailbox.notify.call_args_list if c.args[1].get("event") == "job_offered")
        assert offer["job_id"] == job_id
        assert offer["category"] == "writing"
        assert offer["max_price"] == 30
        assert offer["title"] == "Research tools"
        assert "description" in offer
        assert offer["criteria"] == ["must compare 3 tools"]
        assert offer["consumer_name"] == "carol"
        # Bid-window deadline so the provider's daemon can prune a lapsed offer
        # (non-bidders get no terminal event otherwise — codex P2).
        assert isinstance(offer["bid_deadline"], float)
        assert offer["bid_deadline"] > 0

    async def test_notify_failure_does_not_break_post(self, dynamodb_tables, mailbox, ws):
        # mailbox.notify raising must not strand the offer: offeredTo is the
        # durable record and the job still posts ok.
        _register_provider(dynamodb_tables, "sub", "sue", ["writing"])

        def _boom(agent_id, event):
            if event.get("event") == "job_offered":
                raise RuntimeError("daemon socket gone")
            return True

        mailbox.notify.side_effect = _boom

        cc = _conn("consumer", "carol", "c-consumer")
        ws.conns["consumer"] = cc
        _seed_agent(dynamodb_tables, "consumer", "carol")
        result = await ex.post_job(cc, "T", "D", 30, "balanced", category="writing")

        assert result["status"] == "ok"
        # offeredTo committed despite the notify blowing up.
        assert _offered_to(dynamodb_tables, result["job_id"]) == {"sub"}
