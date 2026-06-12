"""Tests for TownService.claim_sprite_job — conditional DynamoDB gate.

These tests mock the DynamoDB table directly via ``dynamo.agents_table`` so
they run without moto and with zero network.  The three behaviours under test:

1. Successful claim  — ``update_item`` succeeds → returns True and calls
   ``update_item`` with the correct Key, UpdateExpression, and a
   ConditionExpression.
2. Conditional failure — ``update_item`` raises ``ClientError`` with code
   ``ConditionalCheckFailedException`` → returns False (another producer
   already claimed the job, caller skips SQS send).
3. Other AWS errors  — any other ``ClientError`` (e.g.
   ``ProvisionedThroughputExceededException``) is re-raised so callers can
   surface the failure rather than silently swallowing it.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


def _client_error(code: str) -> ClientError:
    """Build a minimal ``ClientError`` with the given ``Error.Code``."""
    return ClientError(
        error_response={"Error": {"Code": code, "Message": "mocked"}},
        operation_name="UpdateItem",
    )


@pytest.fixture()
def svc_with_mock_table():
    """Return a TownService instance whose agents table is a MagicMock.

    We patch ``core.dynamo.agents_table`` so every call inside the method
    under test returns our pre-configured mock rather than hitting DynamoDB.
    Yields ``(svc, mock_table)`` so individual tests can configure
    ``mock_table.update_item`` return values / side effects.
    """
    from core.services.town_service import TownService

    mock_table = MagicMock()
    with patch("core.dynamo.agents_table", return_value=mock_table):
        yield TownService(), mock_table


class TestClaimSpriteJob:
    # ------------------------------------------------------------------
    # Behaviour 1: successful claim
    # ------------------------------------------------------------------

    def test_claim_returns_true_on_success(self, svc_with_mock_table):
        """When update_item succeeds (no exception), claim_sprite_job returns True."""
        svc, mock_table = svc_with_mock_table
        mock_table.update_item.return_value = {}

        result = svc.claim_sprite_job("agent-1")

        assert result is True

    def test_claim_calls_update_item_with_correct_key(self, svc_with_mock_table):
        """update_item must be called with Key={"id": agent_id}."""
        svc, mock_table = svc_with_mock_table
        mock_table.update_item.return_value = {}

        svc.claim_sprite_job("agent-1")

        call_kwargs = mock_table.update_item.call_args.kwargs
        assert call_kwargs["Key"] == {"id": "agent-1"}

    def test_claim_sets_sprite_status_to_queued(self, svc_with_mock_table):
        """UpdateExpression must SET spriteStatus to 'queued'."""
        svc, mock_table = svc_with_mock_table
        mock_table.update_item.return_value = {}

        svc.claim_sprite_job("agent-1")

        call_kwargs = mock_table.update_item.call_args.kwargs
        assert "spriteStatus" in call_kwargs["UpdateExpression"]
        assert ":queued" in call_kwargs["UpdateExpression"]
        assert call_kwargs["ExpressionAttributeValues"][":queued"] == "queued"

    def test_claim_includes_condition_expression(self, svc_with_mock_table):
        """A ConditionExpression must be present to make the write atomic."""
        svc, mock_table = svc_with_mock_table
        mock_table.update_item.return_value = {}

        svc.claim_sprite_job("agent-1")

        call_kwargs = mock_table.update_item.call_args.kwargs
        assert "ConditionExpression" in call_kwargs
        condition = call_kwargs["ConditionExpression"]
        # Must guard that the agent row exists before claiming
        assert "attribute_exists(id)" in condition

    def test_claim_condition_covers_all_in_progress_statuses(self, svc_with_mock_table):
        """The NOT-IN guard must list every in-progress status — dropping one
        would let a second producer double-enqueue an agent mid-pipeline.
        Mirrors sprite_jobs.IN_PROGRESS_STATUSES (kept in sync by hand because
        DynamoDB condition expressions can't import constants)."""
        svc, mock_table = svc_with_mock_table
        mock_table.update_item.return_value = {}

        svc.claim_sprite_job("agent-1")

        values = mock_table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        statuses = {values[f":s{i}"] for i in range(6)}
        assert statuses == {
            "queued",
            "creating",
            "building_static",
            "static_ready",
            "animating",
            "downloading",
        }
        assert values[":null_type"] == "NULL"

    # ------------------------------------------------------------------
    # Behaviour 2: conditional check failure → False (already claimed)
    # ------------------------------------------------------------------

    def test_conditional_check_failed_returns_false(self, svc_with_mock_table):
        """ConditionalCheckFailedException means another producer won the race.
        claim_sprite_job must return False (not raise)."""
        svc, mock_table = svc_with_mock_table
        mock_table.update_item.side_effect = _client_error("ConditionalCheckFailedException")

        result = svc.claim_sprite_job("agent-1")

        assert result is False

    # ------------------------------------------------------------------
    # Behaviour 3: unexpected AWS errors are re-raised
    # ------------------------------------------------------------------

    def test_other_client_error_is_reraised(self, svc_with_mock_table):
        """Any ClientError other than ConditionalCheckFailedException must
        propagate so callers can handle real infrastructure failures."""
        svc, mock_table = svc_with_mock_table
        mock_table.update_item.side_effect = _client_error("ProvisionedThroughputExceededException")

        with pytest.raises(ClientError) as exc_info:
            svc.claim_sprite_job("agent-1")

        assert "ProvisionedThroughputExceededException" in str(exc_info.value)

    def test_other_client_error_not_swallowed(self, svc_with_mock_table):
        """Sanity-check that generic ClientError (unknown code) is also re-raised."""
        svc, mock_table = svc_with_mock_table
        mock_table.update_item.side_effect = _client_error("InternalServerError")

        with pytest.raises(ClientError):
            svc.claim_sprite_job("agent-1")
