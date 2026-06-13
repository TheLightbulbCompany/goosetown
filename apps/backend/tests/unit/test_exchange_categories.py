"""Tests for exchange_categories — slug validation + wildcard helpers (TDD, Task 2).

All tests are table-driven where practical.  Run with:

    cd apps/backend && uv run pytest tests/unit/test_exchange_categories.py -q
"""

from __future__ import annotations

import pytest

from core.services.exchange_categories import (
    SEED_DOMAINS,
    match_categories,
    normalize_category,
    wildcard_parents,
)

# ---------------------------------------------------------------------------
# SEED_DOMAINS
# ---------------------------------------------------------------------------


def test_seed_domains_is_tuple() -> None:
    assert isinstance(SEED_DOMAINS, tuple)


def test_seed_domains_contains_aex_canonical() -> None:
    aex_canonical = {"commerce", "finance", "travel", "entertainment", "technology"}
    assert aex_canonical.issubset(set(SEED_DOMAINS))


def test_seed_domains_contains_goosetown_natural() -> None:
    gt_natural = {"writing", "research", "creative", "conversation"}
    assert gt_natural.issubset(set(SEED_DOMAINS))


def test_seed_domains_all_valid_slugs() -> None:
    """Every seed domain must pass normalize_category."""
    for domain in SEED_DOMAINS:
        result = normalize_category(domain)
        assert result == domain, f"seed domain {domain!r} did not survive normalize_category"


# ---------------------------------------------------------------------------
# normalize_category — valid inputs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("technology", "technology"),
        ("technology.code-review", "technology.code-review"),
        ("writing", "writing"),
        ("a.b-c_d", "a.b-c_d"),
        ("commerce", "commerce"),
        ("finance", "finance"),
        ("travel.booking", "travel.booking"),
        ("entertainment", "entertainment"),
        ("research", "research"),
        ("creative", "creative"),
        ("conversation", "conversation"),
        # Whitespace is stripped before validation
        ("  technology  ", "technology"),
        ("  writing  ", "writing"),
        # Exactly 64 chars — valid
        ("a" * 64, "a" * 64),
        # Single char — valid
        ("a", "a"),
        # Digits and dots and hyphens in non-leading positions
        ("a1", "a1"),
        ("a.1", "a.1"),
        ("a-b", "a-b"),
        ("a_b", "a_b"),
        ("a.b.c", "a.b.c"),
    ],
)
def test_normalize_category_valid(raw: str, expected: str) -> None:
    assert normalize_category(raw) == expected


# ---------------------------------------------------------------------------
# normalize_category — invalid inputs → None
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        # None passthrough
        None,
        # Empty / whitespace-only
        "",
        "   ",
        # Leading digit (remains a leading digit even after lower)
        "1technology",
        "1",
        # Leading dot
        ".technology",
        ".a.b",
        # Leading hyphen (not letter)
        "-technology",
        # Leading underscore (not letter)
        "_technology",
        # Space inside slug (space is not in the allowed charset)
        "tech nology",
        "technology code review",
        # Special chars not in the allowed set
        "technology/code",
        "technology@code",
        "technology!",
        "technology+review",
        "technology:review",
        "technology#review",
        # Too long (65 chars)
        "a" * 65,
        # Empty after strip
        " ",
    ],
)
def test_normalize_category_invalid(raw: str | None) -> None:
    assert normalize_category(raw) is None


# ---------------------------------------------------------------------------
# normalize_category — uppercase is lowercased (not rejected)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        # The spec says "strip + lowercase" before validation — so uppercase
        # inputs are normalised to their lowercase form, not rejected.
        ("Technology", "technology"),
        ("TECHNOLOGY", "technology"),
        ("technology.Code-Review", "technology.code-review"),
        ("  WRITING  ", "writing"),
    ],
)
def test_normalize_category_lowercases_input(raw: str, expected: str) -> None:
    assert normalize_category(raw) == expected


# ---------------------------------------------------------------------------
# normalize_category — idempotence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "slug",
    [
        "technology",
        "technology.code-review",
        "writing",
        "a.b-c_d",
        "a.b.c",
        "research",
    ],
)
def test_normalize_category_idempotent(slug: str) -> None:
    first = normalize_category(slug)
    assert first is not None, f"slug {slug!r} was unexpectedly invalid"
    assert normalize_category(first) == first


# ---------------------------------------------------------------------------
# wildcard_parents — single segment
# ---------------------------------------------------------------------------


def test_wildcard_parents_single_segment() -> None:
    assert wildcard_parents("technology") == ["technology"]


def test_wildcard_parents_single_segment_commerce() -> None:
    assert wildcard_parents("commerce") == ["commerce"]


# ---------------------------------------------------------------------------
# wildcard_parents — two segments
# ---------------------------------------------------------------------------


def test_wildcard_parents_two_segments() -> None:
    result = wildcard_parents("technology.code-review")
    assert result == ["technology.code-review", "technology"]


def test_wildcard_parents_two_segments_travel_booking() -> None:
    result = wildcard_parents("travel.booking")
    assert result == ["travel.booking", "travel"]


# ---------------------------------------------------------------------------
# wildcard_parents — deep (three segments)
# ---------------------------------------------------------------------------


def test_wildcard_parents_three_segments() -> None:
    result = wildcard_parents("a.b.c")
    assert result == ["a.b.c", "a.b", "a"]


def test_wildcard_parents_four_segments() -> None:
    result = wildcard_parents("a.b.c.d")
    assert result == ["a.b.c.d", "a.b.c", "a.b", "a"]


# ---------------------------------------------------------------------------
# wildcard_parents — ordering (most-specific-first)
# ---------------------------------------------------------------------------


def test_wildcard_parents_ordering_most_specific_first() -> None:
    """The first element must be the full slug; subsequent are progressively shorter."""
    parts = wildcard_parents("technology.code-review")
    assert parts[0] == "technology.code-review"
    assert parts[-1] == "technology"


def test_wildcard_parents_ordering_deep() -> None:
    parts = wildcard_parents("a.b.c")
    for i in range(len(parts) - 1):
        assert len(parts[i]) > len(parts[i + 1]), f"expected strictly decreasing length, got {parts}"


def test_wildcard_parents_length_equals_segment_count() -> None:
    """Number of results == number of dot-separated segments."""
    assert len(wildcard_parents("a")) == 1
    assert len(wildcard_parents("a.b")) == 2
    assert len(wildcard_parents("a.b.c")) == 3
    assert len(wildcard_parents("a.b.c.d")) == 4


# ---------------------------------------------------------------------------
# wildcard_parents — all entries are strings (no None)
# ---------------------------------------------------------------------------


def test_wildcard_parents_returns_strings() -> None:
    for cat in ["technology", "technology.code-review", "a.b.c"]:
        results = wildcard_parents(cat)
        assert all(isinstance(r, str) for r in results)


# ---------------------------------------------------------------------------
# match_categories — thin wrapper equivalence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "slug",
    [
        "technology",
        "technology.code-review",
        "writing",
        "a.b.c",
        "commerce",
    ],
)
def test_match_categories_equals_wildcard_parents(slug: str) -> None:
    """match_categories must be a thin wrapper — identical output to wildcard_parents."""
    assert match_categories(slug) == wildcard_parents(slug)


def test_match_categories_single() -> None:
    assert match_categories("technology") == ["technology"]


def test_match_categories_dotted() -> None:
    assert match_categories("technology.code-review") == [
        "technology.code-review",
        "technology",
    ]


# ---------------------------------------------------------------------------
# Semantic correctness — provider subscription matching intent
# ---------------------------------------------------------------------------


def test_subscriber_to_parent_matches_child_job() -> None:
    """A provider subscribed to 'technology' should see a job categorized
    'technology.code-review' — this is the wildcard-subscription guarantee.

    The subscription writes rows for each parent; the job's match_categories
    returns the job's full category + all parents.  The intersection of a
    provider's subscription set and the job's match_categories must be non-empty.
    """
    provider_subscribed_to = {"technology"}  # provider subscribed to the parent
    job_match_keys = set(match_categories("technology.code-review"))
    assert provider_subscribed_to & job_match_keys, (
        "Provider subscribed to 'technology' should match job 'technology.code-review'"
    )


def test_subscriber_to_child_does_not_match_parent_job() -> None:
    """A provider subscribed only to 'technology.code-review' should NOT be
    offered a job categorized just 'technology' (too broad)."""
    provider_subscribed_to = {"technology.code-review"}
    job_match_keys = set(match_categories("technology"))
    assert not (provider_subscribed_to & job_match_keys), (
        "Provider subscribed to child should not match a parent-only job"
    )


def test_subscriber_to_specific_matches_exact_job() -> None:
    """Exact match — provider subscribed to 'technology.code-review', job is too."""
    provider_subscribed_to = {"technology.code-review"}
    job_match_keys = set(match_categories("technology.code-review"))
    assert provider_subscribed_to & job_match_keys
