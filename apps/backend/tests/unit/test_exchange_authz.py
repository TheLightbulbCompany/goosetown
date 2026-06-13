"""Tests for Wave 2 Task 5: presence-OR-offer remote-bid authz + provider invite.

Wave 1 required a bidder to be physically at the bank (``_at_exchange``) to bid.
Wave 2 relaxes this for the provider-marketplace: a subscribed/invited provider
may bid **remotely** as long as it appears in the job's durable ``offeredTo``
String Set. The authz becomes **presence OR offer**:

* bidder at the bank → allowed (the in-world casual path; presence wins).
* bidder ∈ ``offeredTo`` → allowed remotely.
* neither → rejected ``not_invited``.

``invite_provider`` is the consumer-driven directory→invite path: it appends a
provider to ``offeredTo`` (durable) and pushes a ``job_offered`` notify with
``invited=True``. That makes a directory pick (or an off-category specialist)
biddable remotely.

``resync_agent_jobs`` gains an OPEN-status branch so a provider whose offer
notify was lost can reconstruct it on reconnect (codex P1 durability) — the
``offeredTo`` set is the authoritative record, the event is best-effort.

Patterns (moto + stubbed mailbox/ws) mirror ``test_exchange_lifecycle.py`` and
``test_exchange_matcher.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.services import exchange_service as ex
from core.services.provider_directory import ProviderDirectory

# ---------------------------------------------------------------------------
# Fixtures & helpers (lifted from test_exchange_matcher.py)
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
    raw = _get_job(db, job_id).get("offeredTo")
    return set(raw) if raw else set()


def _offer_targets(mailbox) -> list[str]:
    return [c.args[0] for c in mailbox.notify.call_args_list if c.args[1].get("event") == "job_offered"]


def _register_provider(db, agent_id: str, name: str, categories: list[str], location: str = "bank", **extra) -> None:
    _seed_agent(db, agent_id, name, location=location, **extra)
    ProviderDirectory().register(agent_id, skills=[name], categories=categories, bio=f"{name}'s bio")


async def _post(
    db,
    ws,
    consumer_id="consumer",
    consumer_name="carol",
    *,
    category=None,
    max_price=30,
    **kwargs,
):
    """Seed + connect the consumer, then post a job. Returns (conn, job_id)."""
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
# place_bid — presence OR offer authz
# ---------------------------------------------------------------------------


class TestRemoteBidAuthz:
    async def test_offered_provider_bids_remotely_ok(self, dynamodb_tables, mailbox, ws):
        # Subscriber to "writing" is auto-offered the job, then bids from afar.
        _register_provider(dynamodb_tables, "sub", "sue", ["writing"], location="library")
        _, job_id = await _post(dynamodb_tables, ws, category="writing")
        assert "sub" in _offered_to(dynamodb_tables, job_id)

        # Provider is NOT at the bank (library) — but it's in offeredTo.
        result = await ex.place_bid(_conn("sub", "sue", ctx="town"), job_id, 20, 0.8, "I write")
        assert result["status"] == "ok", result
        assert result["job_id"] == job_id
        # Bid landed on the job.
        assert "sub" in _get_job(dynamodb_tables, job_id).get("bids", {})

    async def test_non_offered_non_present_provider_rejected_not_invited(self, dynamodb_tables, mailbox, ws):
        # A provider in a DIFFERENT category — never offered — and not at the bank.
        _register_provider(dynamodb_tables, "off", "olive", ["research"], location="cafe")
        _, job_id = await _post(dynamodb_tables, ws, category="writing")
        assert "off" not in _offered_to(dynamodb_tables, job_id)

        result = await ex.place_bid(_conn("off", "olive", ctx="town"), job_id, 20, 0.8, "")
        assert result["status"] == "rejected"
        assert result["reason"] == "not_invited"
        assert "weren't offered" in result["message"]
        # No bid recorded.
        assert "off" not in _get_job(dynamodb_tables, job_id).get("bids", {})

    async def test_at_bank_non_offered_provider_bids_ok(self, dynamodb_tables, mailbox, ws):
        # Casual in-world path: bidder is at the bank but was never offered the
        # job (not in offeredTo) — presence wins.
        _, job_id = await _post(dynamodb_tables, ws, category="writing")
        _seed_agent(dynamodb_tables, "walkin", "wendy", location="bank")
        assert "walkin" not in _offered_to(dynamodb_tables, job_id)

        result = await ex.place_bid(_conn("walkin", "wendy"), job_id, 15, 0.6, "")
        assert result["status"] == "ok", result
        assert "walkin" in _get_job(dynamodb_tables, job_id).get("bids", {})

    async def test_missing_offered_to_attr_does_not_crash(self, dynamodb_tables, mailbox, ws):
        # A job with no subscribers leaves offeredTo unset. A remote non-present
        # bidder must get a clean not_invited (not an AttributeError).
        _, job_id = await _post(dynamodb_tables, ws, category="writing")
        assert _get_job(dynamodb_tables, job_id).get("offeredTo") is None
        _seed_agent(dynamodb_tables, "remote", "rita", location="plaza")

        result = await ex.place_bid(_conn("remote", "rita"), job_id, 10, 0.5, "")
        assert result["status"] == "rejected"
        assert result["reason"] == "not_invited"

    async def test_own_job_rejected_before_offer_check(self, dynamodb_tables, mailbox, ws):
        # The consumer is remote (not at bank, not in offeredTo). own_job must
        # fire ahead of the presence-or-offer check.
        cc, job_id = await _post(dynamodb_tables, ws, category="writing")
        # Move the consumer away so it's neither present nor offered.
        dynamodb_tables.Table("goosetown-agents").update_item(
            Key={"id": "consumer"},
            UpdateExpression="SET currentLocation = :loc",
            ExpressionAttributeValues={":loc": "plaza"},
        )
        result = await ex.place_bid(_conn("consumer", "carol"), job_id, 10, 0.5, "")
        assert result["reason"] == "own_job"

    async def test_own_household_rejected_before_offer_check(self, dynamodb_tables, mailbox, ws):
        # Same-owner agent that is NOT offered and NOT at the bank: own_household
        # must win over not_invited (cheaper rejection ordered first).
        _seed_agent(dynamodb_tables, "consumer", "carol", userId="user-1")
        cc = _conn("consumer", "carol", "c-consumer")
        ws.conns["consumer"] = cc
        post = await ex.post_job(cc, "T", "D", 30, "balanced", category="writing")
        job_id = post["job_id"]

        # Same household (userId), remote, not offered.
        _seed_agent(dynamodb_tables, "sibling", "sam", location="plaza", userId="user-1")
        result = await ex.place_bid(_conn("sibling", "sam"), job_id, 10, 0.5, "")
        assert result["reason"] == "own_household"

    async def test_not_open_rejected_for_offered_provider(self, dynamodb_tables, mailbox, ws):
        # Even an offered provider can't bid once the window closed.
        _register_provider(dynamodb_tables, "sub", "sue", ["writing"], location="library")
        _, job_id = await _post(dynamodb_tables, ws, category="writing")
        await ex._close_bid_window(job_id)  # no bids -> EXPIRED

        result = await ex.place_bid(_conn("sub", "sue"), job_id, 20, 0.8, "")
        assert result["reason"] == "not_open"


# ---------------------------------------------------------------------------
# invite_provider — consumer-driven directory→invite path
# ---------------------------------------------------------------------------


class TestInviteProvider:
    async def test_consumer_invite_appends_offered_to_and_notifies(self, dynamodb_tables, mailbox, ws):
        cc, job_id = await _post(dynamodb_tables, ws, category="writing")
        _register_provider(dynamodb_tables, "spec", "iris", ["design"], location="cafe")

        result = await ex.invite_provider(cc, job_id=job_id, provider_id="spec")
        assert result["status"] == "ok", result

        assert "spec" in _offered_to(dynamodb_tables, job_id)
        # job_offered notify with invited=True went to the provider.
        offers = [c.args[1] for c in mailbox.notify.call_args_list if c.args[1].get("event") == "job_offered"]
        invited = [o for o in offers if o.get("invited") is True]
        assert len(invited) == 1
        assert invited[0]["job_id"] == job_id
        assert "spec" in _offer_targets(mailbox)

    async def test_non_consumer_invite_rejected_not_consumer(self, dynamodb_tables, mailbox, ws):
        cc, job_id = await _post(dynamodb_tables, ws, category="writing")
        _register_provider(dynamodb_tables, "spec", "iris", ["design"])
        # Someone other than the consumer tries to invite.
        other = _conn("intruder", "ivan")
        result = await ex.invite_provider(other, job_id=job_id, provider_id="spec")
        assert result["status"] == "rejected"
        assert result["reason"] == "not_consumer"
        assert "spec" not in _offered_to(dynamodb_tables, job_id)

    async def test_invite_on_non_open_job_rejected_not_open(self, dynamodb_tables, mailbox, ws):
        cc, job_id = await _post(dynamodb_tables, ws, category="writing")
        await ex._close_bid_window(job_id)  # -> EXPIRED
        _register_provider(dynamodb_tables, "spec", "iris", ["design"])

        result = await ex.invite_provider(cc, job_id=job_id, provider_id="spec")
        assert result["status"] == "rejected"
        assert result["reason"] == "not_open"
        assert "spec" not in _offered_to(dynamodb_tables, job_id)

    async def test_invite_unknown_job_rejected_not_found(self, dynamodb_tables, mailbox, ws):
        _seed_agent(dynamodb_tables, "consumer", "carol")
        cc = _conn("consumer", "carol")
        result = await ex.invite_provider(cc, job_id="nope", provider_id="spec")
        assert result["status"] == "rejected"
        assert result["reason"] == "not_found"

    async def test_invited_off_category_specialist_can_bid_remotely(self, dynamodb_tables, mailbox, ws):
        # End-to-end: an off-category specialist gets invited, then bids from afar.
        cc, job_id = await _post(dynamodb_tables, ws, category="writing")
        _register_provider(dynamodb_tables, "spec", "iris", ["design"], location="cafe")
        # Not auto-offered (off-category).
        assert "spec" not in _offered_to(dynamodb_tables, job_id)

        invite = await ex.invite_provider(cc, job_id=job_id, provider_id="spec")
        assert invite["status"] == "ok"

        # Now biddable remotely (at cafe, not bank) because it's in offeredTo.
        bid = await ex.place_bid(_conn("spec", "iris"), job_id, 22, 0.7, "I can design")
        assert bid["status"] == "ok", bid
        assert "spec" in _get_job(dynamodb_tables, job_id).get("bids", {})


# ---------------------------------------------------------------------------
# resync — OPEN-status offer reconstruction
# ---------------------------------------------------------------------------


class TestResyncOffers:
    async def test_open_job_with_agent_in_offered_to_resyncs(self, dynamodb_tables, mailbox, ws):
        _register_provider(dynamodb_tables, "sub", "sue", ["writing"], location="library")
        _, job_id = await _post(dynamodb_tables, ws, category="writing")
        assert "sub" in _offered_to(dynamodb_tables, job_id)
        mailbox.notify.reset_mock()  # drop the post-time offer

        sent = ex.resync_agent_jobs("sub")
        assert sent >= 1
        offers = [c.args[1] for c in mailbox.notify.call_args_list if c.args[0] == "sub"]
        resync_offers = [o for o in offers if o.get("event") == "job_offered" and o.get("resync") is True]
        assert len(resync_offers) == 1
        assert resync_offers[0]["job_id"] == job_id
        assert resync_offers[0]["category"] == "writing"

    async def test_open_job_without_agent_in_offered_to_no_resync(self, dynamodb_tables, mailbox, ws):
        # A different provider not in offeredTo gets no resync offer.
        _register_provider(dynamodb_tables, "sub", "sue", ["writing"], location="library")
        _, job_id = await _post(dynamodb_tables, ws, category="writing")
        mailbox.notify.reset_mock()

        ex.resync_agent_jobs("not-offered")
        offers = [
            c.args[1]
            for c in mailbox.notify.call_args_list
            if c.args[0] == "not-offered" and c.args[1].get("event") == "job_offered"
        ]
        assert offers == []

    async def test_consumer_not_resynced_their_own_open_job_as_offer(self, dynamodb_tables, mailbox, ws):
        # The consumer is excluded from offeredTo by the matcher, so resync for
        # the consumer must not produce a job_offered for its own OPEN job.
        _register_provider(dynamodb_tables, "consumer", "carol", ["writing"])
        _register_provider(dynamodb_tables, "sub", "sue", ["writing"])
        _, job_id = await _post(dynamodb_tables, ws, category="writing")
        assert "consumer" not in _offered_to(dynamodb_tables, job_id)
        mailbox.notify.reset_mock()

        ex.resync_agent_jobs("consumer")
        offers = [
            c.args[1]
            for c in mailbox.notify.call_args_list
            if c.args[0] == "consumer" and c.args[1].get("event") == "job_offered"
        ]
        assert offers == []
