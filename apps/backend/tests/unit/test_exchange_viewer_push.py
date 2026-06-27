"""_push_economy_to_viewers builds the economy_event payload and pushes it to
viewers best-effort (a relay failure must not propagate into the economy flow)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from core.services import exchange_service as ex


def test_payload_shape_and_push():
    relay = MagicMock()
    with patch("core.services.exchange_service.get_viewer_relay", return_value=relay):
        ex._push_economy_to_viewers("payout", "p1", "Pat", "c1", "Carol", 18, "Write a haiku")
    relay.push_event.assert_called_once_with(
        {
            "event": "economy_event",
            "type": "payout",
            "actorId": "p1",
            "actorName": "Pat",
            "counterpartyId": "c1",
            "counterpartyName": "Carol",
            "amount": 18,
            "title": "Write a haiku",
        }
    )


def test_no_counterparty_and_no_amount():
    relay = MagicMock()
    with patch("core.services.exchange_service.get_viewer_relay", return_value=relay):
        ex._push_economy_to_viewers("posted", "c1", "Carol", None, None, 30, "Haiku")
    payload = relay.push_event.call_args.args[0]
    assert payload["counterpartyId"] is None and payload["counterpartyName"] is None
    assert payload["amount"] == 30 and payload["type"] == "posted"


def test_push_failure_is_swallowed():
    relay = MagicMock()
    relay.push_event.side_effect = RuntimeError("relay down")
    with patch("core.services.exchange_service.get_viewer_relay", return_value=relay):
        # Must not raise — best-effort.
        ex._push_economy_to_viewers("awarded", "p1", "Pat", "c1", "Carol", 20, "Haiku")
