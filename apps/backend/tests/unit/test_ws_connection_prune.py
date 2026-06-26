"""Dead-connection pruning on GoneException (2026-06-26 crash-loop fix).

When a WebSocket delivery hits ``GoneException`` the backend must remove the
dead connection from BOTH the in-memory registry AND the durable ``connections``
DynamoDB table. Before this fix it only logged a warning and retried the same
dead connection on every broadcast cycle — a flood of sequential, blocking
``send_message`` calls that starved the async event loop, timed out ``/health``,
and crash-looped the backend (the stale rows survived the restart because they
were never deleted from DynamoDB, so each replacement task re-seeded and
re-flooded them).

Two fan-out paths, both fixed:
  * ``DaemonMailbox.notify_connection`` — agent/owner-viewer delivery.
  * ``ViewerRelay._broadcast`` — world_state fan-out to browser viewers.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.services.daemon_mailbox import DaemonMailbox
from core.services.viewer_relay import ViewerRelay

# ---------------------------------------------------------------------------
# DaemonMailbox.notify_connection — prune on gone, leave live/transient alone
# ---------------------------------------------------------------------------


def _mailbox(*, send_result=True, send_raises=False):
    mgmt = MagicMock()
    if send_raises:
        mgmt.send_message.side_effect = RuntimeError("transient transport boom")
    else:
        mgmt.send_message.return_value = send_result
    wsm = MagicMock()
    return DaemonMailbox(mgmt=mgmt, ws_manager=wsm), mgmt, wsm


class TestDaemonMailboxPrune:
    def test_gone_prunes_registry_and_dynamo(self):
        mb, _mgmt, wsm = _mailbox(send_result=False)  # False == GoneException
        with patch("core.dynamo.delete_connection") as del_conn:
            result = mb.notify_connection("conn-dead", {"event": "x"})
        assert result is False
        wsm.unregister.assert_called_once_with("conn-dead")
        del_conn.assert_called_once_with("conn-dead")

    def test_live_connection_is_not_pruned(self):
        mb, _mgmt, wsm = _mailbox(send_result=True)
        with patch("core.dynamo.delete_connection") as del_conn:
            result = mb.notify_connection("conn-live", {"event": "x"})
        assert result is True
        wsm.unregister.assert_not_called()
        del_conn.assert_not_called()

    def test_transient_error_does_not_prune(self):
        """A non-gone transport error must NOT evict a possibly-live connection —
        only a definitive GoneException (send_message -> False) prunes."""
        mb, _mgmt, wsm = _mailbox(send_raises=True)
        with patch("core.dynamo.delete_connection") as del_conn:
            result = mb.notify_connection("conn-maybe-live", {"event": "x"})
        assert result is False
        wsm.unregister.assert_not_called()
        del_conn.assert_not_called()

    def test_notify_by_agent_id_prunes_on_gone(self):
        mb, _mgmt, wsm = _mailbox(send_result=False)
        wsm.get_by_agent_id.return_value = SimpleNamespace(connection_id="conn-dead")
        with patch("core.dynamo.delete_connection") as del_conn:
            result = mb.notify("agent-1", {"event": "x"})
        assert result is False
        wsm.unregister.assert_called_once_with("conn-dead")
        del_conn.assert_called_once_with("conn-dead")

    def test_prune_is_best_effort_and_never_raises(self):
        """unregister or the DynamoDB delete blowing up must not propagate —
        the caller still receives the delivery verdict (False), not an exception."""
        mb, _mgmt, wsm = _mailbox(send_result=False)
        wsm.unregister.side_effect = RuntimeError("registry boom")
        with patch("core.dynamo.delete_connection", side_effect=RuntimeError("ddb boom")):
            result = mb.notify_connection("conn-dead", {"event": "x"})
        assert result is False  # no raise


# ---------------------------------------------------------------------------
# ViewerRelay._broadcast — prune gone viewers from memory AND DynamoDB
# ---------------------------------------------------------------------------


class TestViewerRelayPrune:
    def test_broadcast_prunes_gone_from_memory_and_dynamo(self):
        relay = ViewerRelay("ws://gs")
        relay._viewer_connections = {"live-1", "dead-1"}
        mgmt = MagicMock()
        mgmt.send_message.side_effect = lambda cid, data: cid == "live-1"  # dead-1 -> False
        with (
            patch("core.services.management_api_client.ManagementApiClient", return_value=mgmt),
            patch("core.dynamo.delete_connection") as del_conn,
        ):
            asyncio.run(relay._broadcast({"event": "world_state", "agents": []}))
        assert relay._viewer_connections == {"live-1"}
        del_conn.assert_called_once_with("dead-1")

    def test_broadcast_keeps_all_live_viewers(self):
        relay = ViewerRelay("ws://gs")
        relay._viewer_connections = {"a", "b", "c"}
        mgmt = MagicMock()
        mgmt.send_message.return_value = True
        with (
            patch("core.services.management_api_client.ManagementApiClient", return_value=mgmt),
            patch("core.dynamo.delete_connection") as del_conn,
        ):
            asyncio.run(relay._broadcast({"event": "world_state", "agents": []}))
        assert relay._viewer_connections == {"a", "b", "c"}
        assert mgmt.send_message.call_count == 3
        del_conn.assert_not_called()

    def test_broadcast_empty_is_noop(self):
        relay = ViewerRelay("ws://gs")
        relay._viewer_connections = set()
        with patch("core.dynamo.delete_connection") as del_conn:
            asyncio.run(relay._broadcast({"event": "world_state", "agents": []}))
        del_conn.assert_not_called()

    def test_broadcast_transient_error_keeps_viewer(self):
        """A transport exception (not a gone-connection) leaves the viewer in the
        set for the next cycle — it must not be deleted from DynamoDB."""
        relay = ViewerRelay("ws://gs")
        relay._viewer_connections = {"flaky-1"}
        mgmt = MagicMock()
        mgmt.send_message.side_effect = RuntimeError("throttled")
        with (
            patch("core.services.management_api_client.ManagementApiClient", return_value=mgmt),
            patch("core.dynamo.delete_connection") as del_conn,
        ):
            asyncio.run(relay._broadcast({"event": "world_state", "agents": []}))
        assert relay._viewer_connections == {"flaky-1"}
        del_conn.assert_not_called()
