"""Tests for exchange_service wallet & ledger primitives (Task 5)."""

from __future__ import annotations

import pytest
from boto3.dynamodb.conditions import Attr

from core.services.exchange_service import (
    SIGNUP_GRANT,
    InsufficientFunds,
    credit_wallet,
    debit_wallet,
    ensure_signup_grant,
    get_wallet,
)


def _seed_agent(dynamodb, agent_id: str = "agent-1", name: str = "alice") -> None:
    dynamodb.Table("goosetown-agents").put_item(Item={"id": agent_id, "agentName": name, "isActive": "true"})


def _ledger_rows(dynamodb, agent_id: str) -> list[dict]:
    resp = dynamodb.Table("goosetown-ledger").scan(FilterExpression=Attr("agentId").eq(agent_id))
    return resp.get("Items", [])


# ---------------------------------------------------------------------------
# ensure_signup_grant
# ---------------------------------------------------------------------------


class TestEnsureSignupGrant:
    def test_fresh_agent_gets_grant(self, dynamodb_tables):
        _seed_agent(dynamodb_tables)
        ensure_signup_grant("agent-1")

        agent = dynamodb_tables.Table("goosetown-agents").get_item(Key={"id": "agent-1"})["Item"]
        assert int(agent["walletBalance"]) == SIGNUP_GRANT

    def test_fresh_agent_creates_one_ledger_row(self, dynamodb_tables):
        _seed_agent(dynamodb_tables)
        ensure_signup_grant("agent-1")

        rows = _ledger_rows(dynamodb_tables, "agent-1")
        assert len(rows) == 1
        row = rows[0]
        assert row["entryType"] == "GRANT"
        assert int(row["amount"]) == SIGNUP_GRANT
        assert int(row["balanceAfter"]) == SIGNUP_GRANT
        assert row["refId"] == "signup"

    def test_idempotent_balance_stays_same(self, dynamodb_tables):
        _seed_agent(dynamodb_tables)
        ensure_signup_grant("agent-1")
        ensure_signup_grant("agent-1")  # second call — must be no-op

        agent = dynamodb_tables.Table("goosetown-agents").get_item(Key={"id": "agent-1"})["Item"]
        assert int(agent["walletBalance"]) == SIGNUP_GRANT

    def test_idempotent_only_one_ledger_row(self, dynamodb_tables):
        _seed_agent(dynamodb_tables)
        ensure_signup_grant("agent-1")
        ensure_signup_grant("agent-1")

        rows = _ledger_rows(dynamodb_tables, "agent-1")
        assert len(rows) == 1

    def test_nonexistent_agent_no_exception(self, dynamodb_tables):
        # Should silently return with no ledger row
        ensure_signup_grant("ghost-agent")

    def test_nonexistent_agent_no_ledger_row(self, dynamodb_tables):
        ensure_signup_grant("ghost-agent")
        rows = _ledger_rows(dynamodb_tables, "ghost-agent")
        assert rows == []


# ---------------------------------------------------------------------------
# debit_wallet
# ---------------------------------------------------------------------------


class TestDebitWallet:
    def test_debit_reduces_balance(self, dynamodb_tables):
        _seed_agent(dynamodb_tables)
        new_balance = debit_wallet("agent-1", 30, "ESCROW_DEBIT", "job-1")

        assert new_balance == SIGNUP_GRANT - 30

    def test_debit_updates_agent_row(self, dynamodb_tables):
        _seed_agent(dynamodb_tables)
        debit_wallet("agent-1", 30, "ESCROW_DEBIT", "job-1")

        agent = dynamodb_tables.Table("goosetown-agents").get_item(Key={"id": "agent-1"})["Item"]
        assert int(agent["walletBalance"]) == SIGNUP_GRANT - 30

    def test_debit_creates_ledger_row(self, dynamodb_tables):
        _seed_agent(dynamodb_tables)
        debit_wallet("agent-1", 30, "ESCROW_DEBIT", "job-1")

        # Filter to just the ESCROW_DEBIT row (there'll also be a GRANT row)
        rows = [r for r in _ledger_rows(dynamodb_tables, "agent-1") if r["entryType"] == "ESCROW_DEBIT"]
        assert len(rows) == 1
        row = rows[0]
        assert int(row["amount"]) == -30
        assert int(row["balanceAfter"]) == SIGNUP_GRANT - 30
        assert row["refId"] == "job-1"
        assert row["entryType"] == "ESCROW_DEBIT"

    def test_debit_insufficient_funds_raises(self, dynamodb_tables):
        _seed_agent(dynamodb_tables)
        with pytest.raises(InsufficientFunds):
            debit_wallet("agent-1", 200, "ESCROW_DEBIT", "job-1")

    def test_debit_insufficient_funds_balance_unchanged(self, dynamodb_tables):
        _seed_agent(dynamodb_tables)
        with pytest.raises(InsufficientFunds):
            debit_wallet("agent-1", 200, "ESCROW_DEBIT", "job-1")

        agent = dynamodb_tables.Table("goosetown-agents").get_item(Key={"id": "agent-1"})["Item"]
        assert int(agent["walletBalance"]) == SIGNUP_GRANT

    def test_debit_insufficient_funds_no_debit_ledger_row(self, dynamodb_tables):
        _seed_agent(dynamodb_tables)
        with pytest.raises(InsufficientFunds):
            debit_wallet("agent-1", 200, "ESCROW_DEBIT", "job-1")

        rows = [r for r in _ledger_rows(dynamodb_tables, "agent-1") if r["entryType"] == "ESCROW_DEBIT"]
        assert rows == []


# ---------------------------------------------------------------------------
# credit_wallet
# ---------------------------------------------------------------------------


class TestCreditWallet:
    def test_credit_increases_balance(self, dynamodb_tables):
        _seed_agent(dynamodb_tables)
        new_balance = credit_wallet("agent-1", 50, "PAYOUT", "job-1")

        assert new_balance == SIGNUP_GRANT + 50

    def test_credit_creates_ledger_row(self, dynamodb_tables):
        _seed_agent(dynamodb_tables)
        credit_wallet("agent-1", 50, "PAYOUT", "job-1")

        rows = [r for r in _ledger_rows(dynamodb_tables, "agent-1") if r["entryType"] == "PAYOUT"]
        assert len(rows) == 1
        row = rows[0]
        assert int(row["amount"]) == 50


# ---------------------------------------------------------------------------
# get_wallet
# ---------------------------------------------------------------------------


class TestGetWallet:
    def test_get_wallet_returns_correct_balance(self, dynamodb_tables):
        _seed_agent(dynamodb_tables)
        wallet = get_wallet("agent-1")

        assert wallet["balance"] == SIGNUP_GRANT

    def test_get_wallet_self_heals_grant(self, dynamodb_tables):
        """get_wallet on a fresh agent (no prior ensure_signup_grant) grants automatically."""
        _seed_agent(dynamodb_tables)
        wallet = get_wallet("agent-1")

        assert wallet["balance"] == SIGNUP_GRANT
        rows = _ledger_rows(dynamodb_tables, "agent-1")
        assert len(rows) == 1
        assert rows[0]["entryType"] == "GRANT"

    def test_get_wallet_defaults(self, dynamodb_tables):
        _seed_agent(dynamodb_tables)
        wallet = get_wallet("agent-1")

        assert wallet["trustScore"] == pytest.approx(0.5)
        assert wallet["trustTier"] == "newcomer"
        assert wallet["jobsCompleted"] == 0
        assert wallet["jobsFailed"] == 0
        assert wallet["jobsPosted"] == 0
