"""Tests for the ProviderDirectory seam (Wave 2 Task 3, ADR-0008).

The directory is the ONE place a future OpenSearch swap happens. These tests
pin its observable contract against the moto-backed ``provider-subscriptions``
projection + the agent profile attrs written via ``TownService``.

Wildcard match semantics (spec §3.3, line 42 — "prefix-on-dot matching"):

    subscribing to ``technology``             catches a job ``technology`` OR ``technology.*``
    subscribing to ``technology.code-review`` catches ONLY ``technology.code-review``

i.e. a *parent* subscription catches *child* jobs; a *child* subscription does
NOT catch its parent or siblings. We achieve that by writing one EXACT row per
subscribed category (no parent expansion on the write side) and expanding the
*job's* category to its wildcard parents at lookup time. The asymmetry is what
makes parent-catches-child true and child-catches-parent false. See the
docstring on ProviderDirectory.register for the reasoning.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.services import exchange_scoring
from core.services.provider_directory import ProviderDirectory, get_provider_directory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_agent(db, agent_id: str, name: str, **extra) -> None:
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
    item.update(extra)
    db.Table("goosetown-agents").put_item(Item=item)


def _agent(db, agent_id: str) -> dict:
    return db.Table("goosetown-agents").get_item(Key={"id": agent_id})["Item"]


def _sub_rows(db, agent_id: str | None = None) -> list[dict]:
    resp = db.Table("goosetown-provider-subscriptions").scan()
    rows = resp.get("Items", [])
    if agent_id is not None:
        rows = [r for r in rows if r.get("providerId") == agent_id]
    return rows


# ---------------------------------------------------------------------------
# register — profile attrs + subscription rows
# ---------------------------------------------------------------------------


class TestRegister:
    def test_writes_profile_attrs_on_agent(self, dynamodb_tables):
        _seed_agent(dynamodb_tables, "p1", "pat")
        pd = ProviderDirectory()
        profile = pd.register(
            "p1",
            skills=["haiku", "research"],
            categories=["writing", "research"],
            bio="I write things.",
            min_price=5,
        )
        row = _agent(dynamodb_tables, "p1")
        assert row["isProvider"] is True
        assert row["providerSkills"] == ["haiku", "research"]
        assert row["providerCategories"] == ["writing", "research"]
        assert row["providerBio"] == "I write things."
        assert int(row["minPrice"]) == 5
        # Returned profile mirrors the written state.
        assert profile["skills"] == ["haiku", "research"]
        assert profile["categories"] == ["writing", "research"]
        assert profile["bio"] == "I write things."
        assert profile["min_price"] == 5

    def test_writes_one_exact_subscription_row_per_category(self, dynamodb_tables):
        _seed_agent(dynamodb_tables, "p1", "pat")
        ProviderDirectory().register("p1", skills=[], categories=["writing", "research"], bio="")
        rows = _sub_rows(dynamodb_tables, "p1")
        keys = sorted(r["category"] for r in rows)
        # Exact-only writes: writing + research, no surprise extra rows.
        assert keys == ["research", "writing"]
        for r in rows:
            assert r["providerId"] == "p1"

    def test_dotted_category_writes_exact_key_only(self, dynamodb_tables):
        # A child subscription writes ONLY the child key (the parent is matched
        # at job-lookup time, not by a written parent row).
        _seed_agent(dynamodb_tables, "p1", "pat")
        ProviderDirectory().register("p1", skills=[], categories=["technology.code-review"], bio="")
        keys = sorted(r["category"] for r in _sub_rows(dynamodb_tables, "p1"))
        assert keys == ["technology.code-review"]

    def test_sort_key_reflects_trust(self, dynamodb_tables):
        _seed_agent(dynamodb_tables, "p1", "pat", trustScore=Decimal("0.7"))
        ProviderDirectory().register("p1", skills=[], categories=["writing"], bio="")
        row = _sub_rows(dynamodb_tables, "p1")[0]
        # f"{0.7:08.4f}#p1" == "000.7000#p1"
        assert row["providerSort"] == ProviderDirectory.SUBS_SORT(0.7, "p1")
        assert row["providerSort"] == "000.7000#p1"

    def test_invalid_categories_dropped(self, dynamodb_tables):
        _seed_agent(dynamodb_tables, "p1", "pat")
        profile = ProviderDirectory().register("p1", skills=[], categories=["Writing", "has space", "research"], bio="")
        # "Writing" normalizes to "writing"; "has space" is invalid → dropped.
        assert profile["categories"] == ["writing", "research"]
        keys = sorted(r["category"] for r in _sub_rows(dynamodb_tables, "p1"))
        assert keys == ["research", "writing"]

    def test_caps_skills_categories_bio(self, dynamodb_tables):
        _seed_agent(dynamodb_tables, "p1", "pat")
        profile = ProviderDirectory().register(
            "p1",
            skills=[f"s{i}" for i in range(20)],
            categories=[f"cat{i}" for i in range(20)],
            bio="x" * 1000,
        )
        assert len(profile["skills"]) == 12
        assert len(profile["categories"]) == 12
        assert len(profile["bio"]) == 500

    def test_default_trust_when_unset(self, dynamodb_tables):
        _seed_agent(dynamodb_tables, "p1", "pat")  # no trustScore attr
        ProviderDirectory().register("p1", skills=[], categories=["writing"], bio="")
        row = _sub_rows(dynamodb_tables, "p1")[0]
        assert row["providerSort"] == ProviderDirectory.SUBS_SORT(exchange_scoring.INITIAL_TRUST, "p1")


# ---------------------------------------------------------------------------
# subscribers_for — the wildcard match contract
# ---------------------------------------------------------------------------


class TestSubscribersFor:
    def test_parent_subscription_catches_child_job(self, dynamodb_tables):
        _seed_agent(dynamodb_tables, "p1", "pat")
        ProviderDirectory().register("p1", skills=[], categories=["technology"], bio="")
        # Provider subscribed to "technology" IS returned for "technology.code-review".
        assert ProviderDirectory().subscribers_for("technology.code-review") == ["p1"]

    def test_exact_subscription_catches_exact_job(self, dynamodb_tables):
        _seed_agent(dynamodb_tables, "p1", "pat")
        ProviderDirectory().register("p1", skills=[], categories=["technology.code-review"], bio="")
        assert ProviderDirectory().subscribers_for("technology.code-review") == ["p1"]

    def test_child_subscription_does_not_catch_parent_job(self, dynamodb_tables):
        _seed_agent(dynamodb_tables, "p1", "pat")
        ProviderDirectory().register("p1", skills=[], categories=["technology.code-review"], bio="")
        # Subscribed to the child; the parent job must NOT reach this provider.
        assert ProviderDirectory().subscribers_for("technology") == []

    def test_child_subscription_does_not_catch_sibling_job(self, dynamodb_tables):
        _seed_agent(dynamodb_tables, "p1", "pat")
        ProviderDirectory().register("p1", skills=[], categories=["technology.code-review"], bio="")
        assert ProviderDirectory().subscribers_for("technology.data") == []

    def test_trust_descending_order_across_providers(self, dynamodb_tables):
        _seed_agent(dynamodb_tables, "low", "lola", trustScore=Decimal("0.3"))
        _seed_agent(dynamodb_tables, "high", "hank", trustScore=Decimal("0.9"))
        pd = ProviderDirectory()
        pd.register("low", skills=[], categories=["writing"], bio="")
        pd.register("high", skills=[], categories=["writing"], bio="")
        assert ProviderDirectory().subscribers_for("writing") == ["high", "low"]

    def test_distinct_provider_ids_across_wildcard_keys(self, dynamodb_tables):
        # A provider subscribed to BOTH a parent and a child shouldn't be listed
        # twice for a child job that matches under both keys.
        _seed_agent(dynamodb_tables, "p1", "pat")
        ProviderDirectory().register(
            "p1",
            skills=[],
            categories=["technology", "technology.code-review"],
            bio="",
        )
        assert ProviderDirectory().subscribers_for("technology.code-review") == ["p1"]

    def test_empty_when_no_subscribers(self, dynamodb_tables):
        assert ProviderDirectory().subscribers_for("writing") == []


# ---------------------------------------------------------------------------
# re-register replaces rows (no stale rows)
# ---------------------------------------------------------------------------


class TestReregister:
    def test_reregister_replaces_rows(self, dynamodb_tables):
        _seed_agent(dynamodb_tables, "p1", "pat")
        pd = ProviderDirectory()
        pd.register("p1", skills=[], categories=["writing"], bio="")
        assert ProviderDirectory().subscribers_for("writing") == ["p1"]

        # Re-register with a different category set.
        pd.register("p1", skills=[], categories=["research"], bio="")
        # Old category no longer matches.
        assert ProviderDirectory().subscribers_for("writing") == []
        # New category matches.
        assert ProviderDirectory().subscribers_for("research") == ["p1"]
        # No stale rows linger.
        keys = sorted(r["category"] for r in _sub_rows(dynamodb_tables, "p1"))
        assert keys == ["research"]

    def test_reregister_updates_trust_in_sort_key(self, dynamodb_tables):
        _seed_agent(dynamodb_tables, "p1", "pat", trustScore=Decimal("0.5"))
        pd = ProviderDirectory()
        pd.register("p1", skills=[], categories=["writing"], bio="")
        # Trust changes, then re-register.
        dynamodb_tables.Table("goosetown-agents").update_item(
            Key={"id": "p1"},
            UpdateExpression="SET trustScore = :t",
            ExpressionAttributeValues={":t": Decimal("0.8")},
        )
        pd.register("p1", skills=[], categories=["writing"], bio="")
        row = _sub_rows(dynamodb_tables, "p1")[0]
        assert row["providerSort"] == ProviderDirectory.SUBS_SORT(0.8, "p1")


# ---------------------------------------------------------------------------
# unregister
# ---------------------------------------------------------------------------


class TestUnregister:
    def test_unregister_clears_rows_and_flips_flag(self, dynamodb_tables):
        _seed_agent(dynamodb_tables, "p1", "pat")
        pd = ProviderDirectory()
        pd.register("p1", skills=[], categories=["writing", "research"], bio="")
        assert _sub_rows(dynamodb_tables, "p1")

        pd.unregister("p1")
        assert _sub_rows(dynamodb_tables, "p1") == []
        assert _agent(dynamodb_tables, "p1")["isProvider"] is False
        assert ProviderDirectory().subscribers_for("writing") == []

    def test_unregister_no_rows_is_safe(self, dynamodb_tables):
        _seed_agent(dynamodb_tables, "p1", "pat")
        # Never registered — unregister is a no-op that still flips the flag.
        ProviderDirectory().unregister("p1")
        assert _agent(dynamodb_tables, "p1")["isProvider"] is False


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


class TestSearch:
    def test_category_filter_trust_desc(self, dynamodb_tables):
        _seed_agent(dynamodb_tables, "low", "lola", trustScore=Decimal("0.3"))
        _seed_agent(dynamodb_tables, "high", "hank", trustScore=Decimal("0.9"))
        pd = ProviderDirectory()
        pd.register("low", skills=["a"], categories=["writing"], bio="lo bio")
        pd.register("high", skills=["b"], categories=["writing"], bio="hi bio")
        results = ProviderDirectory().search(category="writing")
        assert [r["agent_id"] for r in results] == ["high", "low"]
        # Shape of a result row.
        top = results[0]
        assert top["name"] == "hank"
        assert top["bio"] == "hi bio"
        assert top["skills"] == ["b"]
        assert top["trust_score"] == pytest.approx(0.9)
        assert top["trust_tier"] == exchange_scoring.trust_tier(0.9, 0)
        assert "jobs_completed" in top

    def test_category_filter_catches_child_via_wildcard(self, dynamodb_tables):
        _seed_agent(dynamodb_tables, "p1", "pat")
        ProviderDirectory().register("p1", skills=[], categories=["technology"], bio="")
        results = ProviderDirectory().search(category="technology.code-review")
        assert [r["agent_id"] for r in results] == ["p1"]

    def test_skill_filter(self, dynamodb_tables):
        _seed_agent(dynamodb_tables, "p1", "pat")
        _seed_agent(dynamodb_tables, "p2", "peg")
        pd = ProviderDirectory()
        pd.register("p1", skills=["haiku", "research"], categories=["writing"], bio="")
        pd.register("p2", skills=["essay"], categories=["writing"], bio="")
        results = ProviderDirectory().search(category="writing", skill="haiku")
        assert [r["agent_id"] for r in results] == ["p1"]

    def test_min_trust_filter(self, dynamodb_tables):
        _seed_agent(dynamodb_tables, "low", "lola", trustScore=Decimal("0.3"))
        _seed_agent(dynamodb_tables, "high", "hank", trustScore=Decimal("0.9"))
        pd = ProviderDirectory()
        pd.register("low", skills=[], categories=["writing"], bio="")
        pd.register("high", skills=[], categories=["writing"], bio="")
        results = ProviderDirectory().search(category="writing", min_trust=0.5)
        assert [r["agent_id"] for r in results] == ["high"]

    def test_empty_category_result(self, dynamodb_tables):
        assert ProviderDirectory().search(category="writing") == []

    def test_no_category_fallback_returns_providers(self, dynamodb_tables):
        _seed_agent(dynamodb_tables, "p1", "pat")
        _seed_agent(dynamodb_tables, "p2", "peg")
        _seed_agent(dynamodb_tables, "notprov", "ned")  # never a provider
        pd = ProviderDirectory()
        pd.register("p1", skills=[], categories=["writing"], bio="")
        pd.register("p2", skills=[], categories=["research"], bio="")
        results = ProviderDirectory().search()  # no category → scan fallback
        ids = {r["agent_id"] for r in results}
        assert ids == {"p1", "p2"}
        assert "notprov" not in ids

    def test_no_category_fallback_applies_skill_and_min_trust(self, dynamodb_tables):
        _seed_agent(dynamodb_tables, "p1", "pat", trustScore=Decimal("0.9"))
        _seed_agent(dynamodb_tables, "p2", "peg", trustScore=Decimal("0.2"))
        pd = ProviderDirectory()
        pd.register("p1", skills=["haiku"], categories=["writing"], bio="")
        pd.register("p2", skills=["haiku"], categories=["writing"], bio="")
        results = ProviderDirectory().search(skill="haiku", min_trust=0.5)
        assert [r["agent_id"] for r in results] == ["p1"]

    def test_results_omit_wallet_pii(self, dynamodb_tables):
        _seed_agent(
            dynamodb_tables,
            "p1",
            "pat",
            walletBalance=999,
            userId="secret-user",
        )
        ProviderDirectory().register("p1", skills=[], categories=["writing"], bio="")
        for results in (
            ProviderDirectory().search(category="writing"),
            ProviderDirectory().search(),
        ):
            assert results
            row = results[0]
            assert "walletBalance" not in row
            assert "wallet" not in row
            assert "userId" not in row
            assert "balance" not in row
            assert 999 not in row.values()
            assert "secret-user" not in row.values()


# ---------------------------------------------------------------------------
# module singleton
# ---------------------------------------------------------------------------


def test_get_provider_directory_singleton():
    assert get_provider_directory() is get_provider_directory()
    assert isinstance(get_provider_directory(), ProviderDirectory)


# ---------------------------------------------------------------------------
# reindex_trust — Task 7: directory trust-denorm on settlement
# ---------------------------------------------------------------------------


class TestReindexTrust:
    def test_reindex_updates_sort_key_for_provider(self, dynamodb_tables):
        """After reindex_trust, subscription rows' SK encodes the NEW trust score."""
        _seed_agent(dynamodb_tables, "p1", "pat", trustScore=Decimal("0.5"))
        pd = ProviderDirectory()
        pd.register("p1", skills=[], categories=["writing", "research"], bio="")

        # Verify initial sort key encodes trust 0.5.
        rows_before = _sub_rows(dynamodb_tables, "p1")
        for r in rows_before:
            assert r["providerSort"] == ProviderDirectory.SUBS_SORT(0.5, "p1")

        # Reindex to trust 0.8 (simulating a settlement trust bump).
        pd.reindex_trust("p1", 0.8)

        rows_after = _sub_rows(dynamodb_tables, "p1")
        # Same number of rows.
        assert len(rows_after) == len(rows_before)
        # All rows now encode trust 0.8.
        expected_sort = ProviderDirectory.SUBS_SORT(0.8, "p1")
        for r in rows_after:
            assert r["providerSort"] == expected_sort, f"row {r['category']} has stale sort key"

    def test_reindex_reflected_in_subscribers_for(self, dynamodb_tables):
        """After reindex_trust, subscribers_for returns providers ranked by new trust."""
        _seed_agent(dynamodb_tables, "low", "lola", trustScore=Decimal("0.3"))
        _seed_agent(dynamodb_tables, "high", "hank", trustScore=Decimal("0.9"))
        pd = ProviderDirectory()
        pd.register("low", skills=[], categories=["writing"], bio="")
        pd.register("high", skills=[], categories=["writing"], bio="")

        # Initially: high first, low second.
        assert ProviderDirectory().subscribers_for("writing") == ["high", "low"]

        # Boost "low" above "high" via reindex.
        pd.reindex_trust("low", 0.95)

        # Now "low" (with trust 0.95) should rank above "high" (still 0.9).
        ordered = ProviderDirectory().subscribers_for("writing")
        assert ordered[0] == "low", f"Expected 'low' first after trust boost, got: {ordered}"
        assert ordered[1] == "high"

    def test_reindex_reflected_in_search(self, dynamodb_tables):
        """After reindex_trust, directory search returns providers in new trust order."""
        _seed_agent(dynamodb_tables, "low", "lola", trustScore=Decimal("0.3"))
        _seed_agent(dynamodb_tables, "high", "hank", trustScore=Decimal("0.9"))
        pd = ProviderDirectory()
        pd.register("low", skills=[], categories=["writing"], bio="")
        pd.register("high", skills=[], categories=["writing"], bio="")

        # Boost "low" via reindex.
        pd.reindex_trust("low", 0.95)

        results = ProviderDirectory().search(category="writing")
        ids = [r["agent_id"] for r in results]
        assert ids[0] == "low"
        assert ids[1] == "high"

    def test_reindex_non_provider_is_noop(self, dynamodb_tables):
        """reindex_trust on an agent with no subscription rows is a safe no-op."""
        _seed_agent(dynamodb_tables, "nonprov", "ned")
        pd = ProviderDirectory()
        # No register call — no subscription rows.
        # Must not raise, must not create any rows.
        pd.reindex_trust("nonprov", 0.7)
        assert _sub_rows(dynamodb_tables, "nonprov") == []

    def test_reindex_idempotent_when_trust_unchanged(self, dynamodb_tables):
        """reindex_trust with the same trust value leaves rows untouched."""
        _seed_agent(dynamodb_tables, "p1", "pat", trustScore=Decimal("0.5"))
        pd = ProviderDirectory()
        pd.register("p1", skills=[], categories=["writing"], bio="")

        rows_before = _sub_rows(dynamodb_tables, "p1")
        pd.reindex_trust("p1", 0.5)  # same trust — no change expected
        rows_after = _sub_rows(dynamodb_tables, "p1")

        assert len(rows_after) == len(rows_before)
        for r in rows_after:
            assert r["providerSort"] == ProviderDirectory.SUBS_SORT(0.5, "p1")
