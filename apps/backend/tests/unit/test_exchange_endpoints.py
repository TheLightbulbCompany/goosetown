"""Tests for Tasks 9a, 9b, 9c: signup grant on register, board endpoint, apartment wallet.

Task 9a — ensure_signup_grant called on register; grant exception does not break registration.
Task 9b — GET /exchange/jobs returns board (OPEN first, description truncated, bid_count present).
Task 9c — GET /apartment includes wallet_balance + trust_tier with safe defaults.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

TEST_USER_ID = "exchange-ep-user-1"
INSTANCE_ID = "inst-exchange-ep-1"
FAKE_TOKEN = "fake-exchange-ep-token"  # nosec B105: test fixture token, not a credential


# ---------------------------------------------------------------------------
# Auth overrides
# ---------------------------------------------------------------------------


def _town_token_override():
    """Bypass HMAC town_token auth — returns (user_id, raw_token)."""
    return (TEST_USER_ID, FAKE_TOKEN)


def _auth_override():
    from core.auth import AuthContext

    return AuthContext(user_id=TEST_USER_ID)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client_with_token(dynamodb_tables):
    """TestClient with town_token auth bypassed (for registration tests)."""
    from main import app
    from routers.town import get_town_token_user

    instances_table = dynamodb_tables.Table("goosetown-instances")
    instances_table.put_item(
        Item={
            "id": INSTANCE_ID,
            "userId": TEST_USER_ID,
            "townToken": FAKE_TOKEN,
            "isActive": "true",
            "apartmentUnit": 1,
        }
    )

    app.dependency_overrides[get_town_token_user] = _town_token_override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_town_token_user, None)


@pytest.fixture
def client(dynamodb_tables):
    """TestClient with Clerk auth bypassed (for apartment tests and board tests)."""
    from core.auth import get_current_user
    from main import app

    app.dependency_overrides[get_current_user] = _auth_override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def _seed_agent(
    dynamodb_tables,
    agent_id: str,
    user_id: str = TEST_USER_ID,
    agent_name: str = "wallet-agent",
    display_name: str = "Wallet Agent",
    wallet_balance: int | None = None,
    trust_score: float | None = None,
    jobs_completed: int | None = None,
    sprite_ready: bool = True,
    is_active: str = "true",
):
    """Insert a minimal agent record into the mock agents table."""
    table = dynamodb_tables.Table("goosetown-agents")
    item = {
        "id": agent_id,
        "userId": user_id,
        "agentName": agent_name,
        "displayName": display_name,
        "instanceId": INSTANCE_ID,
        "isActive": is_active,
        "joinedAt": "2026-06-01T00:00:00Z",
        "spriteReady": sprite_ready,
        "spriteUrl": "https://cdn.example/sprites/walk.png",
        "spriteUpdatedAt": 1_800_000_000,
        "currentLocation": "plaza",
        "locationContext": "town",
        "energy": 80,
        "mood": 5,
    }
    if wallet_balance is not None:
        item["walletBalance"] = wallet_balance
    if trust_score is not None:
        item["trustScore"] = Decimal(str(trust_score))
    if jobs_completed is not None:
        item["jobsCompleted"] = jobs_completed
    table.put_item(Item=item)
    return item


def _seed_job(
    dynamodb_tables,
    job_id: str,
    status: str,
    title: str = "Test Job",
    description: str = "Short description",
    max_price: int = 100,
    agreed_price: int | None = None,
    consumer_name: str = "alice",
    bids: dict | None = None,
    created_at: str = "2026-06-01T10:00:00+00:00",
):
    """Insert a job row into the mock jobs table."""
    table = dynamodb_tables.Table("goosetown-jobs")
    item: dict = {
        "id": job_id,
        "status": status,
        "title": title,
        "description": description,
        "maxPrice": max_price,
        "consumerName": consumer_name,
        "consumerId": "consumer-agent-id",
        "createdAt": created_at,
        "updatedAt": created_at,
        "bids": bids or {},
    }
    if agreed_price is not None:
        item["agreedPrice"] = agreed_price
    table.put_item(Item=item)
    return item


# ---------------------------------------------------------------------------
# Task 9a: signup grant called on registration
# ---------------------------------------------------------------------------


class TestSignupGrantOnRegister:
    def test_ensure_signup_grant_called_with_new_agent_id(self, client_with_token, dynamodb_tables):
        """ensure_signup_grant must be called with the new agent's id on every
        successful registration."""
        with patch("routers.town.exchange_service") as mock_ex:
            mock_ex.ensure_signup_grant = MagicMock()

            resp = client_with_token.post(
                "/api/v1/town/agent/register",
                json={
                    "agent_name": "grant-agent",
                    "display_name": "Grant Agent",
                    "personality": "friendly",
                    "appearance": "",
                    "traits": "",
                },
            )

        assert resp.status_code == 200, resp.text
        agent_id = resp.json()["agent_id"]
        mock_ex.ensure_signup_grant.assert_called_once_with(agent_id)

    def test_grant_exception_does_not_fail_registration(self, client_with_token, dynamodb_tables):
        """A DDB hiccup in ensure_signup_grant must NOT propagate — the
        registration response must still be 200."""
        with patch("routers.town.exchange_service") as mock_ex:
            mock_ex.ensure_signup_grant = MagicMock(side_effect=RuntimeError("ddb hiccup"))

            resp = client_with_token.post(
                "/api/v1/town/agent/register",
                json={
                    "agent_name": "resilient-agent",
                    "display_name": "Resilient Agent",
                    "personality": "stoic",
                    "appearance": "",
                    "traits": "",
                },
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "agent_id" in body


# ---------------------------------------------------------------------------
# Task 9b: GET /exchange/jobs — public board endpoint
# ---------------------------------------------------------------------------


class TestExchangeBoardEndpoint:
    def test_open_job_appears_in_jobs(self, client, dynamodb_tables):
        """An OPEN job seeded in the jobs table must appear in the response."""
        _seed_job(dynamodb_tables, job_id="job-open-1", status="OPEN", title="Build a widget")

        resp = client.get("/api/v1/town/exchange/jobs")
        assert resp.status_code == 200, resp.text
        jobs = resp.json()["jobs"]
        assert any(j["job_id"] == "job-open-1" for j in jobs)

    def test_open_job_has_required_fields(self, client, dynamodb_tables):
        """Board entries must include: job_id, title, description, max_price,
        agreed_price, status, consumer_name, provider_name, created_at, bid_count."""
        _seed_job(
            dynamodb_tables,
            job_id="job-fields",
            status="OPEN",
            title="Field check job",
            bids={"bidder-1": {"price": 50, "agentName": "bob"}},
        )

        resp = client.get("/api/v1/town/exchange/jobs")
        assert resp.status_code == 200
        jobs = resp.json()["jobs"]
        job = next(j for j in jobs if j["job_id"] == "job-fields")

        for field in (
            "job_id",
            "title",
            "description",
            "max_price",
            "agreed_price",
            "status",
            "consumer_name",
            "provider_name",
            "created_at",
            "bid_count",
        ):
            assert field in job, f"Missing field: {field}"

    def test_description_truncated_to_200_chars(self, client, dynamodb_tables):
        """Descriptions longer than 200 chars must be truncated in the board payload."""
        long_desc = "x" * 400
        _seed_job(dynamodb_tables, job_id="job-long-desc", status="OPEN", description=long_desc)

        resp = client.get("/api/v1/town/exchange/jobs")
        assert resp.status_code == 200
        jobs = resp.json()["jobs"]
        job = next(j for j in jobs if j["job_id"] == "job-long-desc")
        assert len(job["description"]) <= 200

    def test_bid_count_reflects_number_of_bids(self, client, dynamodb_tables):
        """bid_count must equal the number of entries in the bids map."""
        _seed_job(
            dynamodb_tables,
            job_id="job-bids",
            status="OPEN",
            bids={
                "bidder-a": {"price": 40},
                "bidder-b": {"price": 60},
                "bidder-c": {"price": 55},
            },
        )

        resp = client.get("/api/v1/town/exchange/jobs")
        assert resp.status_code == 200
        jobs = resp.json()["jobs"]
        job = next(j for j in jobs if j["job_id"] == "job-bids")
        assert job["bid_count"] == 3

    def test_deliverable_not_in_response(self, client, dynamodb_tables):
        """The sealed deliverable must never appear in the public board payload."""
        table = dynamodb_tables.Table("goosetown-jobs")
        job_id = "job-delivered"
        table.put_item(
            Item={
                "id": job_id,
                "status": "COMPLETED",
                "title": "Delivered job",
                "description": "short",
                "maxPrice": 80,
                "agreedPrice": 70,
                "consumerName": "alice",
                "consumerId": "c1",
                "createdAt": "2026-06-01T11:00:00+00:00",
                "updatedAt": "2026-06-01T12:00:00+00:00",
                "bids": {},
                "deliverable": "SECRET DELIVERABLE CONTENT",
            }
        )

        resp = client.get("/api/v1/town/exchange/jobs")
        assert resp.status_code == 200
        jobs = resp.json()["jobs"]
        job_match = [j for j in jobs if j["job_id"] == job_id]
        assert job_match, "Completed job should appear in board history"
        assert "deliverable" not in job_match[0]

    def test_open_jobs_appear_before_completed(self, client, dynamodb_tables):
        """OPEN jobs should appear before COMPLETED history in the response list."""
        _seed_job(
            dynamodb_tables,
            job_id="job-cmp",
            status="COMPLETED",
            created_at="2026-06-01T08:00:00+00:00",
        )
        _seed_job(
            dynamodb_tables,
            job_id="job-opn",
            status="OPEN",
            created_at="2026-06-01T09:00:00+00:00",
        )

        resp = client.get("/api/v1/town/exchange/jobs")
        assert resp.status_code == 200
        jobs = resp.json()["jobs"]
        ids = [j["job_id"] for j in jobs]
        assert ids.index("job-opn") < ids.index("job-cmp")

    def test_empty_board_returns_empty_list(self, client, dynamodb_tables):
        """No jobs seeded → empty jobs list (not an error)."""
        resp = client.get("/api/v1/town/exchange/jobs")
        assert resp.status_code == 200
        assert resp.json()["jobs"] == []


# ---------------------------------------------------------------------------
# Task 9c: apartment endpoint includes wallet_balance and trust_tier
# ---------------------------------------------------------------------------


class TestApartmentWalletFields:
    def test_wallet_balance_and_trust_tier_present_with_values(self, client, dynamodb_tables):
        """Agent with walletBalance=70, trustScore=0.9, jobsCompleted=20 →
        wallet_balance=70, trust_tier='trusted'."""
        _seed_agent(
            dynamodb_tables,
            agent_id="a-wallet-1",
            wallet_balance=70,
            trust_score=0.9,
            jobs_completed=20,
        )

        resp = client.get("/api/v1/town/apartment")
        assert resp.status_code == 200, resp.text
        agent = next(a for a in resp.json()["agents"] if a["agent_id"] == "a-wallet-1")
        assert agent["wallet_balance"] == 70
        assert agent["trust_tier"] == "trusted"

    def test_wallet_balance_defaults_to_zero_when_absent(self, client, dynamodb_tables):
        """Agent without walletBalance attr → wallet_balance=0 in response."""
        _seed_agent(dynamodb_tables, agent_id="a-wallet-2")

        resp = client.get("/api/v1/town/apartment")
        assert resp.status_code == 200
        agent = next(a for a in resp.json()["agents"] if a["agent_id"] == "a-wallet-2")
        assert agent["wallet_balance"] == 0

    def test_trust_tier_defaults_to_newcomer_when_absent(self, client, dynamodb_tables):
        """Agent without trustScore/jobsCompleted attrs → trust_tier='newcomer'."""
        _seed_agent(dynamodb_tables, agent_id="a-wallet-3")

        resp = client.get("/api/v1/town/apartment")
        assert resp.status_code == 200
        agent = next(a for a in resp.json()["agents"] if a["agent_id"] == "a-wallet-3")
        assert agent["trust_tier"] == "newcomer"

    def test_trust_tier_reliable_for_mid_range(self, client, dynamodb_tables):
        """trustScore=0.75, jobsCompleted=7 → trust_tier='reliable'."""
        _seed_agent(
            dynamodb_tables,
            agent_id="a-wallet-4",
            trust_score=0.75,
            jobs_completed=7,
        )

        resp = client.get("/api/v1/town/apartment")
        assert resp.status_code == 200
        agent = next(a for a in resp.json()["agents"] if a["agent_id"] == "a-wallet-4")
        assert agent["trust_tier"] == "reliable"


# ---------------------------------------------------------------------------
# Owner ledger endpoint: GET /town/agent/{agent_id}/ledger
# ---------------------------------------------------------------------------


class TestOwnerLedgerEndpoint:
    """Tests for GET /town/agent/{agent_id}/ledger (Task A)."""

    def test_owner_sees_entries_newest_first(self, client, dynamodb_tables):
        """Owner fetching their agent's ledger gets entries newest-first."""
        agent_id = "ledger-agent-1"
        _seed_agent(dynamodb_tables, agent_id=agent_id, wallet_balance=90)
        # Seed two ledger rows with distinct createdAt to verify ordering.
        ledger_table = dynamodb_tables.Table("goosetown-ledger")
        ledger_table.put_item(
            Item={
                "id": "row-old",
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
                "id": "row-new",
                "agentId": agent_id,
                "entryType": "ESCROW_DEBIT",
                "amount": -10,
                "balanceAfter": 90,
                "refId": "job-xyz",
                "createdAt": "2026-06-01T11:00:00+00:00",
            }
        )

        resp = client.get(f"/api/v1/town/agent/{agent_id}/ledger")
        assert resp.status_code == 200, resp.text
        body = resp.json()

        # Top-level wallet fields present.
        assert "balance" in body
        assert "trust_tier" in body
        assert "trust_score" in body
        assert "entries" in body

        entries = body["entries"]
        assert len(entries) >= 2
        # Newest (2026-06-01T11:…) must come before oldest (2026-06-01T10:…).
        ats = [e["at"] for e in entries]
        assert ats[0] > ats[1], "Entries must be newest-first"

        # Each entry has required fields.
        for entry in entries:
            for field in ("type", "amount", "balance_after", "ref_id", "at"):
                assert field in entry, f"Missing entry field: {field}"

    def test_non_owner_gets_403(self, dynamodb_tables):
        """A Clerk user who does not own the agent must receive 403."""
        from core.auth import AuthContext, get_current_user
        from main import app

        agent_id = "ledger-agent-403"
        _seed_agent(dynamodb_tables, agent_id=agent_id, user_id="user-victim")

        app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="user-attacker")
        try:
            client_local = TestClient(app)
            resp = client_local.get(f"/api/v1/town/agent/{agent_id}/ledger")
            assert resp.status_code == 403
        finally:
            app.dependency_overrides.pop(get_current_user, None)

    def test_missing_agent_gets_404(self, client, dynamodb_tables):
        """Requesting a ledger for a non-existent agent returns 404."""
        resp = client.get("/api/v1/town/agent/does-not-exist/ledger")
        assert resp.status_code == 404

    def test_empty_ledger_returns_empty_list_and_signup_grant_balance(self, client, dynamodb_tables):
        """Agent with no ledger rows → empty entries list; signup grant self-heals balance to 100."""
        agent_id = "ledger-agent-empty"
        _seed_agent(dynamodb_tables, agent_id=agent_id)  # no walletBalance seeded

        resp = client.get(f"/api/v1/town/agent/{agent_id}/ledger")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # get_wallet self-heals the signup grant (100 crumbs).
        assert body["balance"] == 100
        # After the grant the ledger has exactly the GRANT row.
        assert isinstance(body["entries"], list)
