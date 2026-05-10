"""Tests for sqs_drainer."""

import asyncio
import json
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError


@patch("core.services.sqs_drainer._events")
@patch("core.services.sqs_drainer._sqs")
def test_create_agent_queue_creates_queue_rule_target(mock_sqs, mock_eb):
    from core.services.sqs_drainer import create_agent_queue

    sqs = MagicMock()
    sqs.create_queue.return_value = {"QueueUrl": "https://sqs.us-east-1.amazonaws.com/123/q"}
    sqs.get_queue_attributes.return_value = {"Attributes": {"QueueArn": "arn:aws:sqs:::q"}}
    mock_sqs.return_value = sqs

    eb = MagicMock()
    mock_eb.return_value = eb

    url = create_agent_queue("alice-uuid")

    sqs.create_queue.assert_called_once()
    sqs.set_queue_attributes.assert_called_once()
    eb.put_rule.assert_called_once()
    eb.put_targets.assert_called_once()
    assert url == "https://sqs.us-east-1.amazonaws.com/123/q"


@patch("core.services.sqs_drainer._events")
@patch("core.services.sqs_drainer._sqs")
def test_create_agent_queue_sets_eventbridge_rule_with_correct_pattern(mock_sqs, mock_eb):
    from core.services.sqs_drainer import create_agent_queue

    agent_id = "1234-5678-abcd"
    sqs = MagicMock()
    sqs.create_queue.return_value = {"QueueUrl": "https://sqs.us-east-1.amazonaws.com/123/q"}
    sqs.get_queue_attributes.return_value = {"Attributes": {"QueueArn": "arn:aws:sqs:::q"}}
    mock_sqs.return_value = sqs

    eb = MagicMock()
    mock_eb.return_value = eb

    create_agent_queue(agent_id)

    # Verify put_rule was called with a pattern that contains the agent id prefix
    put_rule_kwargs = eb.put_rule.call_args
    pattern_str = put_rule_kwargs.kwargs.get("EventPattern") or put_rule_kwargs.args[0] if put_rule_kwargs.args else ""
    if not pattern_str:
        # keyword arg
        pattern_str = put_rule_kwargs[1].get("EventPattern", "")
    # The pattern should reference the agent_id as a prefix
    assert agent_id in pattern_str


@patch("core.services.sqs_drainer._events")
@patch("core.services.sqs_drainer._sqs")
def test_delete_agent_queue_removes_targets_rule_queue(mock_sqs, mock_eb):
    from core.services.sqs_drainer import delete_agent_queue

    sqs = MagicMock()
    sqs.get_queue_url.return_value = {"QueueUrl": "https://sqs.us-east-1.amazonaws.com/123/q"}
    mock_sqs.return_value = sqs

    eb = MagicMock()
    mock_eb.return_value = eb

    delete_agent_queue("alice-uuid")
    eb.remove_targets.assert_called_once()
    eb.delete_rule.assert_called_once()
    sqs.delete_queue.assert_called_once()


@patch("core.services.sqs_drainer._events")
@patch("core.services.sqs_drainer._sqs")
def test_delete_agent_queue_ignores_not_found_errors(mock_sqs, mock_eb):
    """delete_agent_queue must be idempotent: ResourceNotFoundException is swallowed."""
    from core.services.sqs_drainer import delete_agent_queue

    sqs = MagicMock()
    sqs.get_queue_url.side_effect = ClientError(
        {"Error": {"Code": "AWS.SimpleQueueService.NonExistentQueue", "Message": "Queue does not exist"}},
        "GetQueueUrl",
    )
    mock_sqs.return_value = sqs

    eb = MagicMock()
    eb.remove_targets.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "Rule not found"}},
        "RemoveTargets",
    )
    eb.delete_rule.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "Rule not found"}},
        "DeleteRule",
    )
    mock_eb.return_value = eb

    # Should not raise
    delete_agent_queue("alice-uuid")


@patch("core.services.sqs_drainer._sqs")
def test_drain_for_agent_no_op_when_no_queue(mock_sqs):
    from core.services.sqs_drainer import drain_for_agent

    sqs = MagicMock()
    sqs.get_queue_url.side_effect = ClientError(
        {
            "Error": {
                "Code": "AWS.SimpleQueueService.NonExistentQueue",
                "Message": "Queue does not exist",
            }
        },
        "GetQueueUrl",
    )
    mock_sqs.return_value = sqs

    result = asyncio.run(drain_for_agent("alice-uuid"))
    assert result == 0
    sqs.receive_message.assert_not_called()


@patch("core.services.sqs_drainer._apply_event_to_agent")
@patch("core.services.sqs_drainer._sqs")
def test_drain_for_agent_processes_messages(mock_sqs, mock_apply):
    from core.services.sqs_drainer import drain_for_agent

    sqs = MagicMock()
    sqs.get_queue_url.return_value = {"QueueUrl": "https://q"}
    sqs.receive_message.side_effect = [
        {
            "Messages": [
                {
                    "Body": json.dumps({"detail": {"tag": "ambient.cat", "wake": False}}),
                    "ReceiptHandle": "h1",
                },
                {
                    "Body": json.dumps({"detail": {"tag": "personal.alice.x", "wake": True}}),
                    "ReceiptHandle": "h2",
                },
            ]
        },
        {"Messages": []},  # empty, end loop
    ]
    mock_sqs.return_value = sqs

    async def _fake_apply(agent_id, event):
        pass

    mock_apply.side_effect = _fake_apply

    drained = asyncio.run(drain_for_agent("alice-uuid"))
    assert drained == 2
    assert sqs.delete_message.call_count == 2
    assert mock_apply.call_count == 2


@patch("core.services.sqs_drainer._apply_event_to_agent")
@patch("core.services.sqs_drainer._sqs")
def test_drain_for_agent_deletes_message_even_on_apply_error(mock_sqs, mock_apply):
    """Messages must be deleted even when _apply_event_to_agent raises, to avoid reprocessing."""
    from core.services.sqs_drainer import drain_for_agent

    sqs = MagicMock()
    sqs.get_queue_url.return_value = {"QueueUrl": "https://q"}
    sqs.receive_message.side_effect = [
        {
            "Messages": [
                {
                    "Body": json.dumps({"detail": {"tag": "ambient.x", "wake": False}}),
                    "ReceiptHandle": "h1",
                },
            ]
        },
        {"Messages": []},
    ]
    mock_sqs.return_value = sqs

    async def _boom(agent_id, event):
        raise RuntimeError("apply exploded")

    mock_apply.side_effect = _boom

    drained = asyncio.run(drain_for_agent("alice-uuid"))
    assert drained == 1
    sqs.delete_message.assert_called_once()


@patch("core.services.sqs_drainer._apply_event_to_agent")
@patch("core.services.sqs_drainer._sqs")
def test_drain_for_agent_handles_non_eventbridge_wrapped_body(mock_sqs, mock_apply):
    """Body without a 'detail' key should be passed through directly as the event."""
    from core.services.sqs_drainer import drain_for_agent

    received_events = []

    async def _capture(agent_id, event):
        received_events.append(event)

    mock_apply.side_effect = _capture

    sqs = MagicMock()
    sqs.get_queue_url.return_value = {"QueueUrl": "https://q"}
    sqs.receive_message.side_effect = [
        {
            "Messages": [
                {
                    "Body": json.dumps({"tag": "personal.foo", "wake": True}),
                    "ReceiptHandle": "h1",
                }
            ]
        },
        {"Messages": []},
    ]
    mock_sqs.return_value = sqs

    asyncio.run(drain_for_agent("alice-uuid"))

    assert len(received_events) == 1
    assert received_events[0]["tag"] == "personal.foo"


@patch("core.services.sqs_drainer._sqs")
def test_apply_event_no_op_when_agent_not_connected(mock_sqs):
    """_apply_event_to_agent is a no-op (but safe) when the agent has no live WS connection."""
    from core.services.sqs_drainer import _apply_event_to_agent
    from core.services.town_agent_ws import get_town_agent_ws_manager

    ws_manager = get_town_agent_ws_manager()
    # Ensure agent is NOT registered
    assert ws_manager.get_by_agent_id("not-connected-agent") is None

    # Should not raise
    asyncio.run(_apply_event_to_agent("not-connected-agent", {"tag": "test", "wake": False}))
