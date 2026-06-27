"""ViewerRelay.push_event — discrete (non-coalescing) fan-out of an event to
all viewer connections, pruning gone ones. Used for economy_event cues."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from core.services.viewer_relay import ViewerRelay


def test_fan_out_delivers_to_all_and_prunes_gone():
    relay = ViewerRelay("ws://gs")
    relay._viewer_connections = {"live-1", "dead-1", "live-2"}
    mgmt = MagicMock()
    # send_message returns False ONLY for the gone connection.
    mgmt.send_message.side_effect = lambda cid, data: cid != "dead-1"
    payload = {"event": "economy_event", "type": "payout", "title": "Haiku"}
    with (
        patch("core.services.management_api_client.ManagementApiClient", return_value=mgmt),
        patch("core.dynamo.delete_connection") as del_conn,
    ):
        asyncio.run(relay._fan_out(list(relay._viewer_connections), payload))
    sent_to = {c.args[0] for c in mgmt.send_message.call_args_list}
    assert sent_to == {"live-1", "dead-1", "live-2"}
    assert relay._viewer_connections == {"live-1", "live-2"}  # dead pruned in-memory
    del_conn.assert_called_once_with("dead-1")  # and from the durable table


def test_push_event_no_running_loop_is_noop():
    relay = ViewerRelay("ws://gs")
    relay._viewer_connections = {"live-1"}
    # No running event loop here → must return without raising.
    relay.push_event({"event": "economy_event", "type": "posted"})


def test_push_event_schedules_fan_out():
    async def run():
        relay = ViewerRelay("ws://gs")
        relay._viewer_connections = {"live-1"}
        seen = {}

        async def fake_fan_out(conns, data):
            seen["conns"] = conns
            seen["data"] = data

        relay._fan_out = fake_fan_out  # type: ignore[method-assign]
        relay.push_event({"event": "economy_event", "type": "awarded"})
        await asyncio.sleep(0.01)  # let the scheduled task run
        return seen

    seen = asyncio.run(run())
    assert seen["data"]["type"] == "awarded"
    assert seen["conns"] == ["live-1"]
