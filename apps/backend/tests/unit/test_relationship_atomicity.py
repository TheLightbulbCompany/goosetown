"""Unit tests for atomic-``ADD``-based relationship updates.

Regression test for the read-modify-write race fixed in PR after #195.
Before the fix, ``TownService.update_relationship`` did a ``get_item``
then ``update_item`` with a pre-computed ``affinityScore``, so two
concurrent calls could both read the same current value and race to
write the same new value — losing one of the deltas.

These tests drive the service with an in-memory DynamoDB table mock and
assert:

* No ``get_item`` is called on the relationships table (the pre-read
  was the whole source of the race).
* The ``UpdateExpression`` uses ``ADD`` for ``affinityScore`` and
  ``interactionCount`` — atomic counter increments.
* When the atomic ADD pushes the score out of [-100, 100], a
  follow-up ``SET`` clamps it. No clamp when the score stays in range.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from core.services.town_service import TownService


def _make_table_mock(affinity_after_add: int = 30):
    """Build a mock relationships-table whose ``update_item`` returns a fake
    post-ADD ``affinityScore``. Captures all calls for assertion.
    """
    table = MagicMock()
    # Default post-ADD return — callers can override per-test via the kwarg.
    table.update_item.return_value = {
        "Attributes": {
            "agentAId": "agent-A",
            "agentBId": "agent-B",
            "affinityScore": affinity_after_add,
            "interactionCount": 5,
        }
    }
    return table


def test_update_uses_atomic_add_no_pre_read():
    """A single ``update_item`` with ADD should be issued — no ``get_item``
    prefix. The old get-then-set pattern is what caused the race."""
    table = _make_table_mock(affinity_after_add=15)
    with patch("core.services.town_service.dynamo.relationships_table", return_value=table):
        TownService().update_relationship("agent-A", "agent-B", affinity_delta=5)

    table.get_item.assert_not_called()
    table.update_item.assert_called_once()
    update_expr = table.update_item.call_args.kwargs["UpdateExpression"]
    # ADD both counters; SET the timestamp.
    assert "ADD" in update_expr
    assert "affinityScore :delta" in update_expr
    assert "interactionCount :one" in update_expr
    # Delta value passed through unchanged — no Python-side arithmetic.
    values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
    assert values[":delta"] == 5


def test_no_clamp_when_post_add_in_range():
    """If the atomic ADD lands inside [-100, 100], no clamp update fires —
    only the one primary ``update_item`` call."""
    table = _make_table_mock(affinity_after_add=30)
    with patch("core.services.town_service.dynamo.relationships_table", return_value=table):
        TownService().update_relationship("agent-A", "agent-B", affinity_delta=5)

    # Exactly one update_item call — no follow-up clamp.
    assert table.update_item.call_count == 1


def test_clamp_on_overflow_above_100():
    """If the ADD overshoots +100, a follow-up ``SET`` clamps to 100.
    Two ``update_item`` calls expected — the atomic ADD, then the clamp."""
    # Pretend a long history of +1 interactions pushed the ADD to 105.
    table = _make_table_mock(affinity_after_add=105)
    with patch("core.services.town_service.dynamo.relationships_table", return_value=table):
        TownService().update_relationship("agent-A", "agent-B", affinity_delta=10)

    assert table.update_item.call_count == 2
    # The second call is the clamp.
    clamp_call = table.update_item.call_args_list[1]
    assert clamp_call.kwargs["UpdateExpression"] == "SET affinityScore = :clamped"
    assert clamp_call.kwargs["ExpressionAttributeValues"][":clamped"] == 100


def test_clamp_on_underflow_below_neg100():
    """Symmetric case: if the ADD undershoots -100, the clamp lands at -100."""
    table = _make_table_mock(affinity_after_add=-110)
    with patch("core.services.town_service.dynamo.relationships_table", return_value=table):
        TownService().update_relationship("agent-A", "agent-B", affinity_delta=-15)

    assert table.update_item.call_count == 2
    clamp_call = table.update_item.call_args_list[1]
    assert clamp_call.kwargs["ExpressionAttributeValues"][":clamped"] == -100


def test_zero_delta_skips_add_and_clamp():
    """If ``affinity_delta == 0`` the update shouldn't touch affinityScore
    at all — only interactionCount + timestamp — and there's no clamp
    path to walk."""
    table = _make_table_mock(affinity_after_add=30)
    with patch("core.services.town_service.dynamo.relationships_table", return_value=table):
        TownService().update_relationship("agent-A", "agent-B", affinity_delta=0)

    assert table.update_item.call_count == 1
    expr = table.update_item.call_args.kwargs["UpdateExpression"]
    values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
    # Still increments interactionCount atomically.
    assert "interactionCount :one" in expr
    # No affinity delta passed.
    assert "affinityScore" not in expr
    assert ":delta" not in values


def test_relationship_type_set_alongside_atomic_add():
    """``new_type`` goes into the SET clause; ``affinity_delta`` into ADD.
    Verifies both coexist in a single UpdateExpression."""
    table = _make_table_mock(affinity_after_add=50)
    with patch("core.services.town_service.dynamo.relationships_table", return_value=table):
        TownService().update_relationship("agent-A", "agent-B", affinity_delta=5, new_type="friend")

    expr = table.update_item.call_args.kwargs["UpdateExpression"]
    values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
    assert "SET " in expr and "ADD " in expr
    assert "relationshipType = :rtype" in expr
    assert values[":rtype"] == "friend"
    assert "affinityScore :delta" in expr
    assert values[":delta"] == 5


def test_key_ordering_stable_across_arg_order():
    """The pair (a, b) and (b, a) must hit the same composite key so the
    same relationship row gets updated regardless of argument order.
    Pre-existing behaviour preserved by the new implementation."""
    table_1 = _make_table_mock(affinity_after_add=30)
    table_2 = _make_table_mock(affinity_after_add=30)

    with patch("core.services.town_service.dynamo.relationships_table", return_value=table_1):
        TownService().update_relationship("zzz-agent", "aaa-agent", affinity_delta=5)
    with patch("core.services.town_service.dynamo.relationships_table", return_value=table_2):
        TownService().update_relationship("aaa-agent", "zzz-agent", affinity_delta=5)

    key_1 = table_1.update_item.call_args.kwargs["Key"]
    key_2 = table_2.update_item.call_args.kwargs["Key"]
    assert key_1 == key_2
    # Sorted alphabetically: "aaa-agent" < "zzz-agent".
    assert key_1 == {"agentAId": "aaa-agent", "agentBId": "zzz-agent"}
