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
