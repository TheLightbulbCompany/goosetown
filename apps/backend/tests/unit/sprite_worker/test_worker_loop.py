"""Tests for sprite_worker.__main__ helpers.

We test the small, pure helper functions only; main() is NOT tested end-to-end
because it requires live AWS + a running backend. Import the module as
``worker_main`` — signal.signal calls at import are fine in pytest (main thread).
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

import sprite_worker.__main__ as worker_main
from sprite_worker.processor import Outcome

# ---------------------------------------------------------------------------
# _apply_outcome
# ---------------------------------------------------------------------------


def _make_sqs():
    sqs = MagicMock()
    sqs.delete_message = MagicMock()
    sqs.send_message = MagicMock()
    sqs.change_message_visibility = MagicMock()
    return sqs


def _make_msg(receipt: str = "receipt-123", body: str = '{"agent_id": "abc"}') -> dict:
    return {"ReceiptHandle": receipt, "Body": body}


def test_apply_outcome_delete_calls_delete_message():
    sqs = _make_sqs()
    worker_main._apply_outcome(sqs, "https://q", _make_msg("receipt-123"), Outcome.DELETE)
    sqs.delete_message.assert_called_once_with(QueueUrl="https://q", ReceiptHandle="receipt-123")
    sqs.send_message.assert_not_called()
    sqs.change_message_visibility.assert_not_called()


def test_apply_outcome_release_calls_nothing():
    sqs = _make_sqs()
    worker_main._apply_outcome(sqs, "https://q", _make_msg("receipt-456"), Outcome.RELEASE)
    sqs.delete_message.assert_not_called()
    sqs.send_message.assert_not_called()
    sqs.change_message_visibility.assert_not_called()


def test_apply_outcome_credit_pause_reenqueues_and_deletes():
    """CREDIT_PAUSE must re-enqueue with DelaySeconds=900, delete the original,
    emit the credit metric, and never call change_message_visibility."""
    sqs = _make_sqs()
    msg_body = '{"agent_id": "xyz"}'
    msg = _make_msg("receipt-789", msg_body)

    # Track call order to assert send happens before delete.
    parent = MagicMock()
    parent.attach_mock(sqs.send_message, "send_message")
    parent.attach_mock(sqs.delete_message, "delete_message")

    with patch.object(worker_main, "_emit_credit_metric") as mock_metric:
        worker_main._apply_outcome(sqs, "https://q", msg, Outcome.CREDIT_PAUSE)

    mock_metric.assert_called_once()
    sqs.send_message.assert_called_once_with(
        QueueUrl="https://q",
        MessageBody=msg_body,
        DelaySeconds=worker_main.CREDIT_PAUSE_SECONDS,
    )
    sqs.delete_message.assert_called_once_with(QueueUrl="https://q", ReceiptHandle="receipt-789")
    sqs.change_message_visibility.assert_not_called()

    # send must precede delete in the call order.
    call_names = [c[0] for c in parent.mock_calls]
    assert call_names.index("send_message") < call_names.index("delete_message")


def test_apply_outcome_credit_pause_delay_seconds_is_sqs_maximum():
    """CREDIT_PAUSE_SECONDS must equal 900, which is the SQS maximum DelaySeconds.
    Values > 900 cause an API error, so this constant must not be bumped."""
    assert worker_main.CREDIT_PAUSE_SECONDS == 900


# ---------------------------------------------------------------------------
# _boot_sweep
# ---------------------------------------------------------------------------


def _make_agent(agent_id: str, sprite_status: str | None) -> dict:
    return {"id": agent_id, "displayName": "Test", "appearance": "a goose", "spriteStatus": sprite_status}


def test_boot_sweep_enqueues_stuck_agents():
    """Agents with in-progress statuses should be re-enqueued (force=True).
    Agents with None, 'complete', or already-clean status should not."""
    agents = [
        _make_agent("uuid-a", None),  # not stuck
        _make_agent("uuid-b", "complete"),  # done
        _make_agent("uuid-c", "queued"),  # stuck
        _make_agent("uuid-d", "downloading"),  # stuck
        _make_agent("uuid-e", "creating"),  # stuck
    ]

    with (
        patch("core.services.town_service.TownService") as MockService,
        patch("core.services.sprite_jobs.enqueue_signup") as mock_enqueue,
    ):
        MockService.return_value.get_all_active_agents.return_value = agents
        mock_enqueue.return_value = True

        swept = worker_main._boot_sweep()

    assert swept == 3
    # Must be called with force=True for each stuck agent
    for c in mock_enqueue.call_args_list:
        assert c.kwargs.get("force") is True


def test_boot_sweep_returns_zero_on_scan_failure():
    """If get_all_active_agents raises, _boot_sweep returns 0 and does not re-raise."""
    with patch("core.services.town_service.TownService") as MockService:
        MockService.return_value.get_all_active_agents.side_effect = RuntimeError("DDB timeout")

        result = worker_main._boot_sweep()

    assert result == 0


# ---------------------------------------------------------------------------
# _heartbeat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_extends_visibility_then_stops_when_event_set():
    """_heartbeat must extend visibility at least once and then exit when the
    stop event is set. We monkeypatch HEARTBEAT_SECONDS to 0 so the wait
    times out immediately."""
    sqs = MagicMock()
    # to_thread wraps the synchronous call; we need it not to block.
    sqs.change_message_visibility = MagicMock()

    stop = asyncio.Event()
    call_count = 0

    async def fake_to_thread(fn, *args, **kwargs):
        nonlocal call_count
        result = fn(*args, **kwargs)
        call_count += 1
        # Stop after first extension so the test doesn't loop.
        stop.set()
        return result

    original_heartbeat_seconds = worker_main.HEARTBEAT_SECONDS
    worker_main.HEARTBEAT_SECONDS = 0  # immediate timeout so first wait fires

    try:
        with patch("sprite_worker.__main__.asyncio.to_thread", side_effect=fake_to_thread):
            await worker_main._heartbeat(sqs, "https://q", "receipt-hb", stop)
    finally:
        worker_main.HEARTBEAT_SECONDS = original_heartbeat_seconds

    assert call_count >= 1


@pytest.mark.asyncio
async def test_heartbeat_extension_failure_does_not_kill_loop():
    """A failing change_message_visibility must not propagate — the loop continues."""
    sqs = MagicMock()
    stop = asyncio.Event()
    call_count = 0

    async def fake_to_thread(fn, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        stop.set()
        raise RuntimeError("visibility update failed")

    original_heartbeat_seconds = worker_main.HEARTBEAT_SECONDS
    worker_main.HEARTBEAT_SECONDS = 0

    try:
        with patch("sprite_worker.__main__.asyncio.to_thread", side_effect=fake_to_thread):
            # Must not raise
            await worker_main._heartbeat(sqs, "https://q", "receipt-err", stop)
    finally:
        worker_main.HEARTBEAT_SECONDS = original_heartbeat_seconds

    assert call_count >= 1
