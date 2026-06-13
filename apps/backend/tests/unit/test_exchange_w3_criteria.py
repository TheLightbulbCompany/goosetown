"""Tests for W3: success criteria + per-item review verdicts (SUCCESS_PARTIAL).

Covers:
- ws_models validator: success_criteria field constraints, criteria_verdicts constraints.
- post_job stores sanitized successCriteria; criteria appear in job_awarded (winner) and
  job_posted broadcast.
- review_job with criteria_verdicts: SUCCESS full accept, SUCCESS_PARTIAL with any false
  verdict (COMPLETED, correct trust math), mismatch-length rejected (bad_verdicts).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from core.services import exchange_scoring
from core.services import exchange_service as ex
from core.ws_models import TownAgentAct

# ---------------------------------------------------------------------------
# Fixtures & helpers (mirrors test_exchange_lifecycle.py)
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


def _get_agent(db, agent_id: str) -> dict:
    return db.Table("goosetown-agents").get_item(Key={"id": agent_id})["Item"]


def _notified_events(mailbox) -> list[tuple[str, str]]:
    return [(c.args[0], c.args[1]["event"]) for c in mailbox.notify.call_args_list]


async def _full_lifecycle(db, ws, mailbox, *, criteria=None):
    """Post → bid → award → deliver. Returns (consumer_conn, provider_conn, job_id)."""
    _seed_agent(db, "consumer", "carol", userId="user-a")
    _seed_agent(db, "provider", "pete", userId="user-b")
    cc = _conn("consumer", "carol", "c-consumer")
    pc = _conn("provider", "pete", "c-provider")
    ws.conns["consumer"] = cc
    ws.conns["provider"] = pc

    result = await ex.post_job(cc, "Write a haiku", "A haiku about dusk.", 30, "balanced", success_criteria=criteria)
    assert result["status"] == "ok", result
    job_id = result["job_id"]

    await ex.place_bid(pc, job_id, 27, 0.9, "I can do this")
    await ex._close_bid_window(job_id)  # awards provider

    await ex.deliver_job(pc, job_id, "dusk settles slow / the fountain hums in bronze light / geese drift homeward")
    return cc, pc, job_id


# ---------------------------------------------------------------------------
# ws_models validator: success_criteria
# ---------------------------------------------------------------------------


class TestWsModelsSuccessCriteria:
    def test_valid_criteria_accepted(self):
        msg = TownAgentAct(
            type="town_agent_act",
            action="post_job",
            title="T",
            description="D",
            max_price=10,
            success_criteria=["criterion one", "criterion two"],
        )
        assert msg.success_criteria == ["criterion one", "criterion two"]

    def test_criteria_on_non_post_job_rejected(self):
        with pytest.raises(ValidationError, match="success_criteria.*only valid with.*post_job"):
            TownAgentAct(
                type="town_agent_act",
                action="bid",
                job_id="j1",
                price=10,
                success_criteria=["criterion"],
            )

    def test_more_than_8_criteria_rejected(self):
        with pytest.raises(ValidationError, match="at most 8"):
            TownAgentAct(
                type="town_agent_act",
                action="post_job",
                title="T",
                description="D",
                max_price=10,
                success_criteria=[f"item {i}" for i in range(9)],
            )

    def test_empty_criterion_rejected(self):
        with pytest.raises(ValidationError, match="non-empty"):
            TownAgentAct(
                type="town_agent_act",
                action="post_job",
                title="T",
                description="D",
                max_price=10,
                success_criteria=["   "],
            )

    def test_criterion_over_200_chars_rejected(self):
        with pytest.raises(ValidationError, match="200 characters"):
            TownAgentAct(
                type="town_agent_act",
                action="post_job",
                title="T",
                description="D",
                max_price=10,
                success_criteria=["x" * 201],
            )

    def test_exactly_8_criteria_accepted(self):
        msg = TownAgentAct(
            type="town_agent_act",
            action="post_job",
            title="T",
            description="D",
            max_price=10,
            success_criteria=[f"item {i}" for i in range(8)],
        )
        assert len(msg.success_criteria) == 8

    def test_no_criteria_accepted(self):
        msg = TownAgentAct(
            type="town_agent_act",
            action="post_job",
            title="T",
            description="D",
            max_price=10,
        )
        assert msg.success_criteria is None


# ---------------------------------------------------------------------------
# ws_models validator: criteria_verdicts
# ---------------------------------------------------------------------------


class TestWsModelsCriteriaVerdicts:
    def test_verdicts_on_review_job_accepted(self):
        msg = TownAgentAct(
            type="town_agent_act",
            action="review_job",
            job_id="j1",
            accept=True,
            criteria_verdicts=[True, False, True],
        )
        assert msg.criteria_verdicts == [True, False, True]

    def test_verdicts_on_non_review_job_rejected(self):
        with pytest.raises(ValidationError, match="criteria_verdicts.*only valid with.*review_job"):
            TownAgentAct(
                type="town_agent_act",
                action="deliver_job",
                job_id="j1",
                deliverable="work",
                criteria_verdicts=[True],
            )


# ---------------------------------------------------------------------------
# post_job: success_criteria stored and broadcast
# ---------------------------------------------------------------------------


class TestPostJobCriteria:
    async def test_criteria_stored_on_job_row(self, dynamodb_tables, mailbox, ws):
        _seed_agent(dynamodb_tables, "consumer", "carol")
        cc = _conn("consumer", "carol", "c-consumer")
        ws.conns["consumer"] = cc
        result = await ex.post_job(
            cc,
            "Write a haiku",
            "desc",
            30,
            "balanced",
            success_criteria=["must be 5-7-5", "no rhyme scheme"],
        )
        assert result["status"] == "ok"
        job = _get_job(dynamodb_tables, result["job_id"])
        assert job["successCriteria"] == ["must be 5-7-5", "no rhyme scheme"]

    async def test_criteria_sanitized_strip_truncate_drop_empty(self, dynamodb_tables, mailbox, ws):
        _seed_agent(dynamodb_tables, "consumer", "carol")
        cc = _conn("consumer", "carol", "c-consumer")
        ws.conns["consumer"] = cc
        result = await ex.post_job(
            cc,
            "T",
            "D",
            30,
            "balanced",
            success_criteria=["  valid  ", "", "x" * 250, "   "],
        )
        assert result["status"] == "ok"
        job = _get_job(dynamodb_tables, result["job_id"])
        criteria = job["successCriteria"]
        assert "valid" in criteria  # stripped
        assert "" not in criteria  # empties dropped
        assert all(len(c) <= 200 for c in criteria)  # truncated

    async def test_criteria_capped_at_8_items(self, dynamodb_tables, mailbox, ws):
        _seed_agent(dynamodb_tables, "consumer", "carol")
        cc = _conn("consumer", "carol", "c-consumer")
        ws.conns["consumer"] = cc
        result = await ex.post_job(
            cc,
            "T",
            "D",
            30,
            "balanced",
            success_criteria=[f"item {i}" for i in range(12)],
        )
        assert result["status"] == "ok"
        job = _get_job(dynamodb_tables, result["job_id"])
        assert len(job["successCriteria"]) == 8

    async def test_criteria_in_broadcast_to_bidders(self, dynamodb_tables, mailbox, ws):
        _seed_agent(dynamodb_tables, "other", "olly")
        ws.conns["other"] = _conn("other", "olly", "c-other")
        _seed_agent(dynamodb_tables, "consumer", "carol")
        cc = _conn("consumer", "carol", "c-consumer")
        ws.conns["consumer"] = cc
        await ex.post_job(
            cc,
            "T",
            "D",
            30,
            "balanced",
            success_criteria=["must use alliteration"],
        )
        # Find the job_posted broadcast to other agents
        broadcast_calls = mailbox.notify_connection.call_args_list
        broadcast_payloads = [c.args[1] for c in broadcast_calls if c.args[1].get("event") == "job_posted"]
        assert broadcast_payloads, "job_posted broadcast not sent"
        assert broadcast_payloads[0]["success_criteria"] == ["must use alliteration"]

    async def test_no_criteria_no_field_stored(self, dynamodb_tables, mailbox, ws):
        _seed_agent(dynamodb_tables, "consumer", "carol")
        cc = _conn("consumer", "carol", "c-consumer")
        ws.conns["consumer"] = cc
        result = await ex.post_job(cc, "T", "D", 30, "balanced")
        job = _get_job(dynamodb_tables, result["job_id"])
        assert "successCriteria" not in job


# ---------------------------------------------------------------------------
# job_awarded: criteria visible in winner's copy
# ---------------------------------------------------------------------------


class TestJobAwardedCriteria:
    async def test_criteria_in_winner_awarded_event(self, dynamodb_tables, mailbox, ws):
        criteria = ["must be haiku format", "must mention the plaza fountain"]
        cc, pc, job_id = await _full_lifecycle(dynamodb_tables, ws, mailbox, criteria=criteria)

        # Find job_awarded events sent to the provider (winner)
        awarded_calls = [
            c
            for c in mailbox.notify.call_args_list
            if c.args[0] == "provider" and c.args[1].get("event") == "job_awarded"
        ]
        assert awarded_calls, "job_awarded not sent to provider"
        event = awarded_calls[0].args[1]
        assert event.get("success_criteria") == criteria
        # description also present (spec requirement)
        assert "description" in event

    async def test_no_criteria_no_field_in_awarded(self, dynamodb_tables, mailbox, ws):
        cc, pc, job_id = await _full_lifecycle(dynamodb_tables, ws, mailbox, criteria=None)

        awarded_calls = [
            c
            for c in mailbox.notify.call_args_list
            if c.args[0] == "provider" and c.args[1].get("event") == "job_awarded"
        ]
        event = awarded_calls[0].args[1]
        assert "success_criteria" not in event


# ---------------------------------------------------------------------------
# review_job: SUCCESS full accept (all verdicts true)
# ---------------------------------------------------------------------------


class TestReviewJobFullSuccess:
    async def test_full_accept_with_all_true_verdicts_is_success(self, dynamodb_tables, mailbox, ws):
        criteria = ["criterion A", "criterion B"]
        cc, pc, job_id = await _full_lifecycle(dynamodb_tables, ws, mailbox, criteria=criteria)

        result = await ex.review_job(cc, job_id, True, "", criteria_verdicts=[True, True])
        assert result["status"] == "ok"
        assert result["outcome"] == "SUCCESS"

        job = _get_job(dynamodb_tables, job_id)
        assert job["status"] == "COMPLETED"
        assert job["outcome"] == "SUCCESS"
        assert job["criteriaVerdicts"] == [True, True]

    async def test_full_accept_no_criteria_is_success(self, dynamodb_tables, mailbox, ws):
        cc, pc, job_id = await _full_lifecycle(dynamodb_tables, ws, mailbox, criteria=None)
        result = await ex.review_job(cc, job_id, True, "")
        assert result["outcome"] == "SUCCESS"


# ---------------------------------------------------------------------------
# review_job: SUCCESS_PARTIAL — any false verdict
# ---------------------------------------------------------------------------


class TestReviewJobPartialSuccess:
    async def test_accept_with_false_verdict_yields_partial(self, dynamodb_tables, mailbox, ws):
        criteria = ["criterion A", "criterion B", "criterion C"]
        cc, pc, job_id = await _full_lifecycle(dynamodb_tables, ws, mailbox, criteria=criteria)

        result = await ex.review_job(cc, job_id, True, "", criteria_verdicts=[True, False, True])
        assert result["status"] == "ok"
        assert result["outcome"] == "SUCCESS_PARTIAL"

        job = _get_job(dynamodb_tables, job_id)
        assert job["status"] == "COMPLETED"
        assert job["outcome"] == "SUCCESS_PARTIAL"
        assert job["criteriaVerdicts"] == [True, False, True]

    async def test_partial_job_completed_event_carries_verdicts(self, dynamodb_tables, mailbox, ws):
        criteria = ["A", "B"]
        cc, pc, job_id = await _full_lifecycle(dynamodb_tables, ws, mailbox, criteria=criteria)
        await ex.review_job(cc, job_id, True, "", criteria_verdicts=[True, False])

        completed_calls = [c for c in mailbox.notify.call_args_list if c.args[1].get("event") == "job_completed"]
        assert completed_calls, "job_completed not sent"
        for call in completed_calls:
            assert call.args[1].get("criteria_verdicts") == [True, False]

    async def test_partial_trust_math(self, dynamodb_tables, mailbox, ws):
        """Accept with false verdict: trust update uses SUCCESS_PARTIAL score (0.6).

        Provider starts at INITIAL_TRUST (0.5).
        EMA: new_trust = 0.8 * 0.5 + 0.2 * 0.6 = 0.52
        """
        criteria = ["A", "B"]
        cc, pc, job_id = await _full_lifecycle(dynamodb_tables, ws, mailbox, criteria=criteria)
        await ex.review_job(cc, job_id, True, "", criteria_verdicts=[True, False])

        provider = _get_agent(dynamodb_tables, "provider")
        trust = float(provider.get("trustScore", exchange_scoring.INITIAL_TRUST))
        expected = round(
            (1 - exchange_scoring.TRUST_EMA_ALPHA) * exchange_scoring.INITIAL_TRUST
            + exchange_scoring.TRUST_EMA_ALPHA * exchange_scoring.OUTCOME_SCORES["SUCCESS_PARTIAL"],
            4,
        )
        assert abs(trust - expected) < 0.001, f"Expected trust {expected}, got {trust}"

    async def test_partial_provider_still_paid(self, dynamodb_tables, mailbox, ws):
        """Payment is unchanged for SUCCESS_PARTIAL — only trust attribution differs."""
        criteria = ["A", "B"]
        cc, pc, job_id = await _full_lifecycle(dynamodb_tables, ws, mailbox, criteria=criteria)
        job_before = _get_job(dynamodb_tables, job_id)
        agreed = int(job_before["agreedPrice"])

        await ex.review_job(cc, job_id, True, "", criteria_verdicts=[True, False])

        provider = _get_agent(dynamodb_tables, "provider")
        fee = exchange_scoring.platform_fee(agreed)
        expected_balance = 100 + (agreed - fee)  # signup grant + payout
        assert int(provider["walletBalance"]) == expected_balance

    async def test_all_false_verdicts_still_accepted_completed(self, dynamodb_tables, mailbox, ws):
        """Verdicts are evidence, not a rejection gate — even all-false is COMPLETED."""
        criteria = ["A", "B"]
        cc, pc, job_id = await _full_lifecycle(dynamodb_tables, ws, mailbox, criteria=criteria)
        result = await ex.review_job(cc, job_id, True, "", criteria_verdicts=[False, False])
        assert result["outcome"] == "SUCCESS_PARTIAL"
        job = _get_job(dynamodb_tables, job_id)
        assert job["status"] == "COMPLETED"


# ---------------------------------------------------------------------------
# review_job: mismatch length rejected
# ---------------------------------------------------------------------------


class TestReviewJobBadVerdicts:
    async def test_length_mismatch_rejected(self, dynamodb_tables, mailbox, ws):
        criteria = ["A", "B", "C"]
        cc, pc, job_id = await _full_lifecycle(dynamodb_tables, ws, mailbox, criteria=criteria)

        result = await ex.review_job(cc, job_id, True, "", criteria_verdicts=[True, False])
        assert result["status"] == "rejected"
        assert result["reason"] == "bad_verdicts"
        # Job must remain DELIVERED (not settled)
        job = _get_job(dynamodb_tables, job_id)
        assert job["status"] == "DELIVERED"

    async def test_verdicts_on_job_without_criteria_ignored(self, dynamodb_tables, mailbox, ws):
        """criteria_verdicts are silently ignored when job has no successCriteria."""
        cc, pc, job_id = await _full_lifecycle(dynamodb_tables, ws, mailbox, criteria=None)
        # Pass verdicts even though job has none — should accept without error.
        result = await ex.review_job(cc, job_id, True, "", criteria_verdicts=[True, False])
        assert result["status"] == "ok"
        assert result["outcome"] == "SUCCESS"

    async def test_empty_verdicts_list_on_job_without_criteria_ignored(self, dynamodb_tables, mailbox, ws):
        cc, pc, job_id = await _full_lifecycle(dynamodb_tables, ws, mailbox, criteria=None)
        result = await ex.review_job(cc, job_id, True, "", criteria_verdicts=[])
        assert result["status"] == "ok"
