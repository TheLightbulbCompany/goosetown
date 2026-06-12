"""Tests for W4: job-scoped messaging (job_msg).

Covers:
- ws_models validator: job_msg requires job_id + msg_text.
- Role gating: stranger → not_participant.
- Status gating: OPEN, COMPLETED → not_active; AWARDED/DELIVERED → allowed.
- Per-side cap: 21st message from same sender rejected; counterparty cap independent.
- Event delivery: counterparty gets job_message event; sender gets exchange_ok.
- Messages persisted in order on the job row.
- Rate limit: rate_limited rejection when _check_rate returns False.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from core.services import exchange_service as ex
from core.ws_models import TownAgentAct

# ---------------------------------------------------------------------------
# Fixtures & helpers
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


def _seed_awarded_job(db, job_id: str, consumer_id: str, provider_id: str) -> None:
    """Write an AWARDED job directly to DynamoDB for testing."""
    db.Table("goosetown-jobs").put_item(
        Item={
            "id": job_id,
            "consumerId": consumer_id,
            "consumerName": "carol",
            "providerId": provider_id,
            "providerName": "dave",
            "title": "Test Job",
            "description": "A test job",
            "maxPrice": 30,
            "agreedPrice": 20,
            "status": "AWARDED",
            "bids": {},
            "createdAt": "2026-06-12T00:00:00+00:00",
            "updatedAt": "2026-06-12T00:01:00+00:00",
        }
    )


# ---------------------------------------------------------------------------
# ws_models validator
# ---------------------------------------------------------------------------


class TestWsModelsValidator:
    def test_job_msg_missing_job_id_raises(self):
        with pytest.raises(ValidationError, match="job_id"):
            TownAgentAct(type="town_agent_act", action="job_msg", msg_text="hello")

    def test_job_msg_missing_msg_text_raises(self):
        with pytest.raises(ValidationError, match="msg_text"):
            TownAgentAct(type="town_agent_act", action="job_msg", job_id="j1")

    def test_job_msg_empty_msg_text_raises(self):
        with pytest.raises(ValidationError, match="msg_text"):
            TownAgentAct(type="town_agent_act", action="job_msg", job_id="j1", msg_text="   ")

    def test_job_msg_msg_text_too_long_raises(self):
        with pytest.raises(ValidationError):
            TownAgentAct(type="town_agent_act", action="job_msg", job_id="j1", msg_text="x" * 2001)

    def test_job_msg_valid_passes(self):
        act = TownAgentAct(type="town_agent_act", action="job_msg", job_id="j1", msg_text="hello")
        assert act.action == "job_msg"
        assert act.job_id == "j1"
        assert act.msg_text == "hello"

    def test_job_msg_msg_text_at_max_length_passes(self):
        act = TownAgentAct(type="town_agent_act", action="job_msg", job_id="j1", msg_text="x" * 2000)
        assert len(act.msg_text) == 2000


# ---------------------------------------------------------------------------
# Role gating
# ---------------------------------------------------------------------------


class TestRoleGating:
    async def test_stranger_gets_not_participant(self, dynamodb_tables, mailbox, ws):
        consumer_id, provider_id, stranger_id = "c1", "p1", "stranger"
        job_id = "job-role-test"
        _seed_agent(dynamodb_tables, stranger_id, "stranger")
        _seed_awarded_job(dynamodb_tables, job_id, consumer_id, provider_id)

        stranger_conn = _conn(stranger_id, "stranger", "conn-stranger")
        result = await ex.job_message(stranger_conn, job_id=job_id, text="hello")

        assert result["status"] == "rejected"
        assert result["reason"] == "not_participant"
        # Counterparty must NOT have received a job_message event.
        notified_events = [c.args[1]["event"] for c in mailbox.notify.call_args_list]
        assert "job_message" not in notified_events

    async def test_consumer_can_send(self, dynamodb_tables, mailbox, ws):
        consumer_id, provider_id = "c2", "p2"
        job_id = "job-consumer-send"
        _seed_agent(dynamodb_tables, consumer_id, "carol")
        _seed_awarded_job(dynamodb_tables, job_id, consumer_id, provider_id)

        cc = _conn(consumer_id, "carol", "conn-c2")
        result = await ex.job_message(cc, job_id=job_id, text="hey provider")
        assert result["status"] == "ok"
        assert result["job_id"] == job_id

    async def test_provider_can_send(self, dynamodb_tables, mailbox, ws):
        consumer_id, provider_id = "c3", "p3"
        job_id = "job-provider-send"
        _seed_agent(dynamodb_tables, provider_id, "dave")
        _seed_awarded_job(dynamodb_tables, job_id, consumer_id, provider_id)

        pc = _conn(provider_id, "dave", "conn-p3")
        result = await ex.job_message(pc, job_id=job_id, text="got it, working on it")
        assert result["status"] == "ok"


# ---------------------------------------------------------------------------
# Status gating
# ---------------------------------------------------------------------------


class TestStatusGating:
    async def _test_status(self, dynamodb_tables, mailbox, ws, status: str, expect_ok: bool):
        consumer_id, provider_id = "csg", "psg"
        job_id = f"job-status-{status}"
        _seed_agent(dynamodb_tables, consumer_id, "carol")
        # Write job with specified status.
        dynamodb_tables.Table("goosetown-jobs").put_item(
            Item={
                "id": job_id,
                "consumerId": consumer_id,
                "providerId": provider_id,
                "consumerName": "carol",
                "providerName": "dave",
                "title": "T",
                "description": "D",
                "maxPrice": 10,
                "status": status,
                "bids": {},
                "createdAt": "2026-06-12T00:00:00+00:00",
                "updatedAt": "2026-06-12T00:00:00+00:00",
            }
        )
        cc = _conn(consumer_id, "carol", "conn-csg")
        result = await ex.job_message(cc, job_id=job_id, text="hello")
        if expect_ok:
            assert result["status"] == "ok", f"Expected ok for status={status}, got: {result}"
        else:
            assert result["status"] == "rejected"
            assert result["reason"] == "not_active"

    async def test_open_rejected(self, dynamodb_tables, mailbox, ws):
        await self._test_status(dynamodb_tables, mailbox, ws, "OPEN", expect_ok=False)

    async def test_completed_rejected(self, dynamodb_tables, mailbox, ws):
        await self._test_status(dynamodb_tables, mailbox, ws, "COMPLETED", expect_ok=False)

    async def test_failed_rejected(self, dynamodb_tables, mailbox, ws):
        await self._test_status(dynamodb_tables, mailbox, ws, "FAILED", expect_ok=False)

    async def test_awarded_allowed(self, dynamodb_tables, mailbox, ws):
        await self._test_status(dynamodb_tables, mailbox, ws, "AWARDED", expect_ok=True)

    async def test_delivered_allowed(self, dynamodb_tables, mailbox, ws):
        await self._test_status(dynamodb_tables, mailbox, ws, "DELIVERED", expect_ok=True)


# ---------------------------------------------------------------------------
# Per-side cap (20 messages per sender)
# ---------------------------------------------------------------------------


class TestPerSideCap:
    async def test_consumer_cap_enforced(self, dynamodb_tables, mailbox, ws):
        """21st message from the consumer is rejected; cap is per-side."""
        consumer_id, provider_id = "ccap", "pcap"
        job_id = "job-cap-test"
        _seed_agent(dynamodb_tables, consumer_id, "carol")

        # Pre-fill 20 consumer messages directly on the job row.
        messages = [
            {"from": consumer_id, "fromName": "carol", "text": f"msg {i}", "at": "2026-06-12T00:00:00+00:00"}
            for i in range(20)
        ]
        dynamodb_tables.Table("goosetown-jobs").put_item(
            Item={
                "id": job_id,
                "consumerId": consumer_id,
                "providerId": provider_id,
                "consumerName": "carol",
                "providerName": "dave",
                "title": "T",
                "description": "D",
                "maxPrice": 10,
                "status": "AWARDED",
                "bids": {},
                "messages": messages,
                "createdAt": "2026-06-12T00:00:00+00:00",
                "updatedAt": "2026-06-12T00:00:00+00:00",
            }
        )
        cc = _conn(consumer_id, "carol", "conn-ccap")
        result = await ex.job_message(cc, job_id=job_id, text="one too many")
        assert result["status"] == "rejected"
        assert result["reason"] == "msg_cap"

    async def test_provider_cap_independent_from_consumer(self, dynamodb_tables, mailbox, ws):
        """Provider can send even if consumer has hit cap."""
        consumer_id, provider_id = "ccap2", "pcap2"
        job_id = "job-cap-independent"
        _seed_agent(dynamodb_tables, provider_id, "dave")

        # 20 consumer messages pre-loaded.
        messages = [
            {"from": consumer_id, "fromName": "carol", "text": f"msg {i}", "at": "2026-06-12T00:00:00+00:00"}
            for i in range(20)
        ]
        dynamodb_tables.Table("goosetown-jobs").put_item(
            Item={
                "id": job_id,
                "consumerId": consumer_id,
                "providerId": provider_id,
                "consumerName": "carol",
                "providerName": "dave",
                "title": "T",
                "description": "D",
                "maxPrice": 10,
                "status": "AWARDED",
                "bids": {},
                "messages": messages,
                "createdAt": "2026-06-12T00:00:00+00:00",
                "updatedAt": "2026-06-12T00:00:00+00:00",
            }
        )
        # Provider has 0 messages — should succeed.
        pc = _conn(provider_id, "dave", "conn-pcap2")
        result = await ex.job_message(pc, job_id=job_id, text="provider reply")
        assert result["status"] == "ok"


# ---------------------------------------------------------------------------
# Event delivery and message persistence
# ---------------------------------------------------------------------------


class TestEventDeliveryAndPersistence:
    async def test_counterparty_gets_job_message_event(self, dynamodb_tables, mailbox, ws):
        """Consumer sends → provider gets job_message event with correct fields."""
        consumer_id, provider_id = "cev", "pev"
        job_id = "job-event-test"
        _seed_agent(dynamodb_tables, consumer_id, "carol")
        _seed_awarded_job(dynamodb_tables, job_id, consumer_id, provider_id)

        cc = _conn(consumer_id, "carol", "conn-cev")
        result = await ex.job_message(cc, job_id=job_id, text="are you making progress?")
        assert result["status"] == "ok"

        # Provider (counterparty) gets job_message event.
        notify_calls = {c.args[0]: c.args[1] for c in mailbox.notify.call_args_list}
        assert provider_id in notify_calls
        evt = notify_calls[provider_id]
        assert evt["event"] == "job_message"
        assert evt["job_id"] == job_id
        assert evt["from_name"] == "carol"
        assert evt["text"] == "are you making progress?"

        # Sender (consumer) gets exchange_ok.
        assert consumer_id in notify_calls
        sender_evt = notify_calls[consumer_id]
        assert sender_evt["event"] == "exchange_ok"
        assert sender_evt["verb"] == "job_msg"

    async def test_message_persisted_on_job_row(self, dynamodb_tables, mailbox, ws):
        """Message appears on the job row's messages list after send."""
        consumer_id, provider_id = "cpers", "ppers"
        job_id = "job-persist-test"
        _seed_agent(dynamodb_tables, consumer_id, "carol")
        _seed_awarded_job(dynamodb_tables, job_id, consumer_id, provider_id)

        cc = _conn(consumer_id, "carol", "conn-cpers")
        await ex.job_message(cc, job_id=job_id, text="first message")
        await ex.job_message(cc, job_id=job_id, text="second message")

        job = _get_job(dynamodb_tables, job_id)
        messages = job.get("messages") or []
        assert len(messages) == 2
        assert messages[0]["from"] == consumer_id
        assert messages[0]["text"] == "first message"
        assert messages[1]["text"] == "second message"

    async def test_sender_only_gets_exchange_ok_not_job_message(self, dynamodb_tables, mailbox, ws):
        """The sender does NOT get a job_message event — only exchange_ok."""
        consumer_id, provider_id = "csend", "psend"
        job_id = "job-sender-event-test"
        _seed_agent(dynamodb_tables, consumer_id, "carol")
        _seed_awarded_job(dynamodb_tables, job_id, consumer_id, provider_id)

        cc = _conn(consumer_id, "carol", "conn-csend")
        await ex.job_message(cc, job_id=job_id, text="hello")

        # consumer_id should NOT have received a job_message event
        consumer_calls = [c.args[1] for c in mailbox.notify.call_args_list if c.args[0] == consumer_id]
        job_msg_events = [e for e in consumer_calls if e.get("event") == "job_message"]
        assert job_msg_events == [], "Sender should not receive job_message event (only exchange_ok)"


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------


class TestRateLimit:
    async def test_rate_limited_rejected(self, dynamodb_tables, mailbox, ws):
        consumer_id, provider_id = "crl", "prl"
        job_id = "job-rate-test"
        _seed_agent(dynamodb_tables, consumer_id, "carol")
        _seed_awarded_job(dynamodb_tables, job_id, consumer_id, provider_id)

        cc = _conn(consumer_id, "carol", "conn-crl")
        with patch("core.services.exchange_service._check_rate", return_value=False):
            with patch("core.services.exchange_service._rate_limit_minutes_left", return_value=42):
                result = await ex.job_message(cc, job_id=job_id, text="hello")

        assert result["status"] == "rejected"
        assert result["reason"] == "rate_limited"
        assert "42" in result["message"]


# ---------------------------------------------------------------------------
# Job not found
# ---------------------------------------------------------------------------


class TestJobNotFound:
    async def test_nonexistent_job_rejected(self, dynamodb_tables, mailbox, ws):
        consumer_id = "cnotfound"
        _seed_agent(dynamodb_tables, consumer_id, "carol")
        cc = _conn(consumer_id, "carol", "conn-nf")
        result = await ex.job_message(cc, job_id="nonexistent-job-id", text="hello")
        assert result["status"] == "rejected"
        assert result["reason"] == "not_found"
