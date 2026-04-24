"""Regression tests for ``get_active_agents_for_user`` — the GSI-query
replacement for the /apartment endpoint's former scan-then-filter pattern.

Before the fix, ``GET /apartment`` called ``get_active_agents`` (which
queries the ``isActive-joinedAt-index`` GSI for every active agent in
the system) and then filtered in Python by ``userId``. /apartment polls
every 2s per logged-in user, so the old cost scaled with
``town_population × users_online``; the fix queries the
``userId-agentName-index`` directly so cost scales only with
``agents-per-user``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from core.services.town_service import TownService


def _mock_table_with_items(items: list[dict]) -> MagicMock:
    table = MagicMock()
    table.query.return_value = {"Items": items}
    return table


def test_queries_userId_agentName_index_not_isActive_scan():
    """The new method must hit ``userId-agentName-index`` (filtered by
    ``isActive=true`` server-side), not ``isActive-joinedAt-index``."""
    table = _mock_table_with_items([{"id": "a-1", "userId": "user-1", "agentName": "jim"}])
    with patch("core.services.town_service.dynamo.agents_table", return_value=table):
        TownService().get_active_agents_for_user("user-1")

    table.query.assert_called_once()
    kwargs = table.query.call_args.kwargs
    assert kwargs["IndexName"] == "userId-agentName-index"
    # Not scanning the isActive-joinedAt index.
    assert kwargs["IndexName"] != "isActive-joinedAt-index"


def test_key_condition_matches_requested_user_id():
    """The query's KeyCondition should bind the userId PK to the arg."""
    table = _mock_table_with_items([])
    with patch("core.services.town_service.dynamo.agents_table", return_value=table):
        TownService().get_active_agents_for_user("user-xyz")

    # boto3's Key objects render to a dict via `.get_expression()`; the
    # simplest assertion is to stringify and look for our user id.
    key_cond = table.query.call_args.kwargs["KeyConditionExpression"]
    assert "user-xyz" in str(key_cond.get_expression()) or "user-xyz" in repr(key_cond)


def test_filter_expression_limits_to_active_agents():
    """Server-side FilterExpression must drop inactive agents so the
    caller never sees opted-out accounts or deleted agents."""
    table = _mock_table_with_items([])
    with patch("core.services.town_service.dynamo.agents_table", return_value=table):
        TownService().get_active_agents_for_user("user-1")

    filter_expr = table.query.call_args.kwargs["FilterExpression"]
    # boto3 Condition: .get_expression() returns
    # {"format": "...", "operator": "=", "values": (Attr, "true")}
    expr = filter_expr.get_expression()
    assert expr["operator"] == "="
    values = expr["values"]
    assert values[0].name == "isActive"
    assert values[1] == "true"


def test_returns_empty_list_when_user_has_no_agents():
    """Fresh user → GSI query returns no items → empty list (not None,
    not KeyError)."""
    table = _mock_table_with_items([])
    with patch("core.services.town_service.dynamo.agents_table", return_value=table):
        result = TownService().get_active_agents_for_user("new-user")

    assert result == []


def test_decimal_to_native_applied_to_returned_items():
    """DynamoDB returns numeric fields as ``Decimal`` — the method should
    convert them to native Python ints/floats so callers don't need to
    re-serialise."""
    from decimal import Decimal

    table = _mock_table_with_items(
        [
            {
                "id": "a-1",
                "userId": "user-1",
                "agentName": "jim",
                "isActive": "true",
                "energy": Decimal("75"),
                "mood": Decimal("10"),
            }
        ]
    )
    with patch("core.services.town_service.dynamo.agents_table", return_value=table):
        result = TownService().get_active_agents_for_user("user-1")

    assert len(result) == 1
    assert isinstance(result[0]["energy"], int)
    assert result[0]["energy"] == 75
