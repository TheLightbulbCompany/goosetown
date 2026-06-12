"""Tests for W6: repeat-counterparty trust damping (anti-farming heuristic).

Three areas covered:
1. exchange_scoring.updated_trust accepts an optional ``alpha`` parameter.
2. _update_trust applies dampened alpha when provider has ≥3 PAYOUT rows
   in their recent ledger sharing the same counterpartyId.
3. Diverse (or missing) counterparty rows → full alpha applied.
4. FAILURE outcomes also dampened consistently — same-pair grief-bombing
   should not tank trust faster than a diverse set of failures would.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from core.services import exchange_scoring
from core.services import exchange_service as ex
from core.services.exchange_scoring import TRUST_EMA_ALPHA

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PROVIDER_ID = "provider-agent-abc"
CONSUMER_ID = "consumer-agent-xyz"


def _seed_agent(db, agent_id: str, name: str = "agent", trust: float | None = None) -> None:
    item = {
        "id": agent_id,
        "agentName": name,
        "displayName": name,
        "isActive": "true",
        "currentLocation": "bank",
        "energy": 100,
        "mood": 0,
        "traits": "",
    }
    if trust is not None:
        item["trustScore"] = Decimal(str(trust))
    db.Table("goosetown-agents").put_item(Item=item)


def _put_ledger_row(
    db,
    agent_id: str,
    entry_type: str,
    counterparty_id: str | None = None,
    created_at: str = "2026-06-12T10:00:00+00:00",
) -> None:
    """Seed a ledger row (PAYOUT or other) for counterparty-diversity tests."""
    item = {
        "id": str(uuid.uuid4()),
        "agentId": agent_id,
        "entryType": entry_type,
        "amount": 10,
        "balanceAfter": 110,
        "refId": str(uuid.uuid4()),
        "createdAt": created_at,
    }
    if counterparty_id is not None:
        item["counterpartyId"] = counterparty_id
    db.Table("goosetown-ledger").put_item(Item=item)


# ---------------------------------------------------------------------------
# Part 1: exchange_scoring.updated_trust alpha parameter
# ---------------------------------------------------------------------------


class TestUpdatedTrustAlphaParam:
    def test_default_alpha_matches_ema_constant(self):
        # With default alpha (0.2): (1-0.2)*0.5 + 0.2*1.0 = 0.6
        result = exchange_scoring.updated_trust(0.5, "SUCCESS")
        assert result == pytest.approx(0.6)

    def test_explicit_default_alpha_same_result(self):
        r_implicit = exchange_scoring.updated_trust(0.5, "SUCCESS")
        r_explicit = exchange_scoring.updated_trust(0.5, "SUCCESS", alpha=TRUST_EMA_ALPHA)
        assert r_implicit == pytest.approx(r_explicit)

    def test_halved_alpha_success_dampened(self):
        # alpha=0.1 (half of 0.2): (1-0.1)*0.5 + 0.1*1.0 = 0.45 + 0.10 = 0.55
        result = exchange_scoring.updated_trust(0.5, "SUCCESS", alpha=0.1)
        assert result == pytest.approx(0.55)

    def test_halved_alpha_failure_dampened(self):
        # alpha=0.1: (1-0.1)*0.5 + 0.1*0.0 = 0.45
        result = exchange_scoring.updated_trust(0.5, "FAILURE_PROVIDER", alpha=0.1)
        assert result == pytest.approx(0.45)

    def test_zero_alpha_leaves_trust_unchanged(self):
        result = exchange_scoring.updated_trust(0.5, "SUCCESS", alpha=0.0)
        assert result == pytest.approx(0.5)

    def test_full_alpha_one_snaps_to_outcome(self):
        # alpha=1.0: outcome score directly
        result = exchange_scoring.updated_trust(0.5, "SUCCESS", alpha=1.0)
        assert result == pytest.approx(1.0)

    def test_unknown_outcome_unchanged_regardless_of_alpha(self):
        result = exchange_scoring.updated_trust(0.5, "UNKNOWN_OUTCOME", alpha=0.1)
        assert result == pytest.approx(0.5)

    def test_result_rounded_to_four_decimal_places(self):
        result = exchange_scoring.updated_trust(0.5, "SUCCESS", alpha=0.1)
        assert result == round(result, 4)


# ---------------------------------------------------------------------------
# Part 2: _update_trust dampening with ≥3 same-counterparty PAYOUT rows
# ---------------------------------------------------------------------------


class TestUpdateTrustDampening:
    def test_three_same_counterparty_payouts_halves_alpha_on_success(self, dynamodb_tables):
        """≥3 PAYOUT rows from the same consumer → alpha halved → dampened trust delta."""
        _seed_agent(dynamodb_tables, PROVIDER_ID, trust=0.5)
        _seed_agent(dynamodb_tables, CONSUMER_ID)
        # Seed 3 PAYOUT rows with the same counterpartyId
        for i in range(3):
            _put_ledger_row(
                dynamodb_tables,
                PROVIDER_ID,
                "PAYOUT",
                counterparty_id=CONSUMER_ID,
                created_at=f"2026-06-12T0{i}:00:00+00:00",
            )
        ex._update_trust(PROVIDER_ID, "SUCCESS", completed_delta=0, failed_delta=0, counterparty_id=CONSUMER_ID)
        agent = dynamodb_tables.Table("goosetown-agents").get_item(Key={"id": PROVIDER_ID})["Item"]
        actual = float(agent["trustScore"])
        # alpha=0.1 (halved from 0.2): (1-0.1)*0.5 + 0.1*1.0 = 0.55
        assert actual == pytest.approx(0.55, abs=1e-4)

    def test_two_same_counterparty_payouts_uses_full_alpha(self, dynamodb_tables):
        """Only 2 prior same-pair PAYOUTs → not yet at threshold → full alpha."""
        _seed_agent(dynamodb_tables, PROVIDER_ID, trust=0.5)
        _seed_agent(dynamodb_tables, CONSUMER_ID)
        for i in range(2):
            _put_ledger_row(
                dynamodb_tables,
                PROVIDER_ID,
                "PAYOUT",
                counterparty_id=CONSUMER_ID,
                created_at=f"2026-06-12T0{i}:00:00+00:00",
            )
        ex._update_trust(PROVIDER_ID, "SUCCESS", completed_delta=0, failed_delta=0, counterparty_id=CONSUMER_ID)
        agent = dynamodb_tables.Table("goosetown-agents").get_item(Key={"id": PROVIDER_ID})["Item"]
        actual = float(agent["trustScore"])
        # full alpha=0.2: (1-0.2)*0.5 + 0.2*1.0 = 0.6
        assert actual == pytest.approx(0.6, abs=1e-4)

    def test_diverse_counterparties_uses_full_alpha(self, dynamodb_tables):
        """PAYOUT rows from different counterparties → diverse → full alpha."""
        _seed_agent(dynamodb_tables, PROVIDER_ID, trust=0.5)
        _seed_agent(dynamodb_tables, CONSUMER_ID)
        # 3 PAYOUTs but from 3 different consumers — no repeat cluster
        for i in range(3):
            _put_ledger_row(
                dynamodb_tables,
                PROVIDER_ID,
                "PAYOUT",
                counterparty_id=f"different-consumer-{i}",
                created_at=f"2026-06-12T0{i}:00:00+00:00",
            )
        ex._update_trust(PROVIDER_ID, "SUCCESS", completed_delta=0, failed_delta=0, counterparty_id=CONSUMER_ID)
        agent = dynamodb_tables.Table("goosetown-agents").get_item(Key={"id": PROVIDER_ID})["Item"]
        actual = float(agent["trustScore"])
        assert actual == pytest.approx(0.6, abs=1e-4)

    def test_rows_without_counterparty_id_count_as_diverse(self, dynamodb_tables):
        """Legacy PAYOUT rows (no counterpartyId) do not trigger dampening."""
        _seed_agent(dynamodb_tables, PROVIDER_ID, trust=0.5)
        _seed_agent(dynamodb_tables, CONSUMER_ID)
        # 3 PAYOUTs without counterpartyId (pre-Wave-1 style)
        for i in range(3):
            _put_ledger_row(
                dynamodb_tables,
                PROVIDER_ID,
                "PAYOUT",
                counterparty_id=None,  # no attribute set
                created_at=f"2026-06-12T0{i}:00:00+00:00",
            )
        ex._update_trust(PROVIDER_ID, "SUCCESS", completed_delta=0, failed_delta=0, counterparty_id=CONSUMER_ID)
        agent = dynamodb_tables.Table("goosetown-agents").get_item(Key={"id": PROVIDER_ID})["Item"]
        actual = float(agent["trustScore"])
        # full alpha: 0.6
        assert actual == pytest.approx(0.6, abs=1e-4)

    def test_no_counterparty_id_argument_uses_full_alpha(self, dynamodb_tables):
        """When counterparty_id not passed (e.g. old call sites), no dampening check."""
        _seed_agent(dynamodb_tables, PROVIDER_ID, trust=0.5)
        # Seed 3 PAYOUTs — should be irrelevant since counterparty_id not passed
        for i in range(3):
            _put_ledger_row(
                dynamodb_tables,
                PROVIDER_ID,
                "PAYOUT",
                counterparty_id=CONSUMER_ID,
                created_at=f"2026-06-12T0{i}:00:00+00:00",
            )
        ex._update_trust(PROVIDER_ID, "SUCCESS", completed_delta=0, failed_delta=0)
        agent = dynamodb_tables.Table("goosetown-agents").get_item(Key={"id": PROVIDER_ID})["Item"]
        actual = float(agent["trustScore"])
        # No counterparty_id → no ledger query → full alpha: 0.6
        assert actual == pytest.approx(0.6, abs=1e-4)

    def test_failure_also_dampened_on_repeat_pair(self, dynamodb_tables):
        """FAILURE outcomes dampened too — same-pair grief-bombing should not tank trust faster."""
        _seed_agent(dynamodb_tables, PROVIDER_ID, trust=0.5)
        _seed_agent(dynamodb_tables, CONSUMER_ID)
        for i in range(3):
            _put_ledger_row(
                dynamodb_tables,
                PROVIDER_ID,
                "PAYOUT",
                counterparty_id=CONSUMER_ID,
                created_at=f"2026-06-12T0{i}:00:00+00:00",
            )
        ex._update_trust(
            PROVIDER_ID, "FAILURE_PROVIDER", completed_delta=0, failed_delta=0, counterparty_id=CONSUMER_ID
        )
        agent = dynamodb_tables.Table("goosetown-agents").get_item(Key={"id": PROVIDER_ID})["Item"]
        actual = float(agent["trustScore"])
        # alpha=0.1 (halved): (1-0.1)*0.5 + 0.1*0.0 = 0.45
        assert actual == pytest.approx(0.45, abs=1e-4)

    def test_failure_full_alpha_when_diverse(self, dynamodb_tables):
        """FAILURE with diverse counterparties uses full alpha (deeper trust drop)."""
        _seed_agent(dynamodb_tables, PROVIDER_ID, trust=0.5)
        _seed_agent(dynamodb_tables, CONSUMER_ID)
        ex._update_trust(
            PROVIDER_ID, "FAILURE_PROVIDER", completed_delta=0, failed_delta=0, counterparty_id=CONSUMER_ID
        )
        agent = dynamodb_tables.Table("goosetown-agents").get_item(Key={"id": PROVIDER_ID})["Item"]
        actual = float(agent["trustScore"])
        # full alpha=0.2: (1-0.2)*0.5 + 0.2*0.0 = 0.4
        assert actual == pytest.approx(0.4, abs=1e-4)

    def test_exactly_three_triggers_dampening(self, dynamodb_tables):
        """Boundary: exactly 3 same-pair PAYOUT rows → dampened (threshold is ≥3)."""
        _seed_agent(dynamodb_tables, PROVIDER_ID, trust=0.5)
        _seed_agent(dynamodb_tables, CONSUMER_ID)
        for i in range(3):
            _put_ledger_row(
                dynamodb_tables,
                PROVIDER_ID,
                "PAYOUT",
                counterparty_id=CONSUMER_ID,
                created_at=f"2026-06-12T0{i}:00:00+00:00",
            )
        ex._update_trust(PROVIDER_ID, "SUCCESS", completed_delta=0, failed_delta=0, counterparty_id=CONSUMER_ID)
        agent = dynamodb_tables.Table("goosetown-agents").get_item(Key={"id": PROVIDER_ID})["Item"]
        assert float(agent["trustScore"]) == pytest.approx(0.55, abs=1e-4)

    def test_non_payout_entry_types_do_not_count(self, dynamodb_tables):
        """ESCROW_REFUND and GRANT rows with the same counterpartyId don't count toward dampening."""
        _seed_agent(dynamodb_tables, PROVIDER_ID, trust=0.5)
        _seed_agent(dynamodb_tables, CONSUMER_ID)
        # 3 ESCROW_REFUNDs from same consumer — should not trigger dampening
        for i in range(3):
            _put_ledger_row(
                dynamodb_tables,
                PROVIDER_ID,
                "ESCROW_REFUND",
                counterparty_id=CONSUMER_ID,
                created_at=f"2026-06-12T0{i}:00:00+00:00",
            )
        ex._update_trust(PROVIDER_ID, "SUCCESS", completed_delta=0, failed_delta=0, counterparty_id=CONSUMER_ID)
        agent = dynamodb_tables.Table("goosetown-agents").get_item(Key={"id": PROVIDER_ID})["Item"]
        actual = float(agent["trustScore"])
        # No PAYOUT rows → full alpha → 0.6
        assert actual == pytest.approx(0.6, abs=1e-4)


# ---------------------------------------------------------------------------
# Part 3: _ledger_append stores counterpartyId when provided
# ---------------------------------------------------------------------------


class TestLedgerAppendCounterparty:
    def test_payout_row_stores_counterparty_id(self, dynamodb_tables):
        """credit_wallet called with counterparty_id persists counterpartyId on PAYOUT row."""
        _seed_agent(dynamodb_tables, PROVIDER_ID)
        ex.credit_wallet(PROVIDER_ID, 10, "PAYOUT", "ref-job-1", counterparty_id=CONSUMER_ID)
        resp = dynamodb_tables.Table("goosetown-ledger").scan()
        rows = [r for r in resp["Items"] if r.get("agentId") == PROVIDER_ID and r.get("entryType") == "PAYOUT"]
        assert len(rows) == 1
        assert rows[0].get("counterpartyId") == CONSUMER_ID

    def test_grant_row_has_no_counterparty_id(self, dynamodb_tables):
        """Signup GRANT rows (no counterparty_id arg) do not get a counterpartyId attribute."""
        _seed_agent(dynamodb_tables, PROVIDER_ID)
        ex.ensure_signup_grant(PROVIDER_ID)
        resp = dynamodb_tables.Table("goosetown-ledger").scan()
        grant_rows = [r for r in resp["Items"] if r.get("agentId") == PROVIDER_ID and r.get("entryType") == "GRANT"]
        assert len(grant_rows) == 1
        assert "counterpartyId" not in grant_rows[0]

    def test_escrow_debit_has_no_counterparty_id(self, dynamodb_tables):
        """ESCROW_DEBIT rows (consumer escrows max_price — no counterparty yet) have no counterpartyId."""
        _seed_agent(dynamodb_tables, CONSUMER_ID)
        ex.debit_wallet(CONSUMER_ID, 10, "ESCROW_DEBIT", "ref-job-2")
        resp = dynamodb_tables.Table("goosetown-ledger").scan()
        debit_rows = [
            r for r in resp["Items"] if r.get("agentId") == CONSUMER_ID and r.get("entryType") == "ESCROW_DEBIT"
        ]
        assert len(debit_rows) == 1
        assert "counterpartyId" not in debit_rows[0]
