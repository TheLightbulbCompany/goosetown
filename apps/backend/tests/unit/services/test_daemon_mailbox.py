"""Tests for the DaemonMailbox adapter.

Covers the four operations of the seam between handlers and daemon
WebSockets:

* ``notify(agent_id, event)`` — looks up connection_id, sends.
* ``notify_connection(connection_id, event)`` — direct send.
* ``reject(connection_id, event_type, reason, **extra)`` — envelope helper.
* ``disconnect(connection_id)`` — close.

Plus the singleton lifecycle (``get_daemon_mailbox`` /
``reset_daemon_mailbox``).

Surfaced 2026-05-04 as architecture audit Candidate 5.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.services.daemon_mailbox import (
    DaemonMailbox,
    get_daemon_mailbox,
    reset_daemon_mailbox,
)

# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _make_mgmt_mock(send_returns: bool = True, raises: Exception | None = None) -> MagicMock:
    mock = MagicMock()
    if raises is not None:
        mock.send_message.side_effect = raises
    else:
        mock.send_message.return_value = send_returns
    return mock


def _make_ws_manager_mock(connection_for_agent: dict | None = None) -> MagicMock:
    mock = MagicMock()
    mapping = connection_for_agent or {}
    mock.get_by_agent_id.side_effect = lambda aid: mapping.get(aid)
    return mock


# ---------------------------------------------------------------------------
# notify(agent_id, event) — lookup-and-send.
# ---------------------------------------------------------------------------


class TestNotify:
    def test_notify_looks_up_connection_and_sends(self):
        mgmt = _make_mgmt_mock(send_returns=True)
        ws = _make_ws_manager_mock({"agent-1": SimpleNamespace(connection_id="conn-A1")})
        mb = DaemonMailbox(mgmt=mgmt, ws_manager=ws)

        ok = mb.notify("agent-1", {"event": "hello"})

        assert ok is True
        ws.get_by_agent_id.assert_called_once_with("agent-1")
        mgmt.send_message.assert_called_once_with("conn-A1", {"event": "hello"})

    def test_notify_returns_false_when_no_connection_registered(self):
        """An agent without a registered connection (e.g. their daemon is
        offline) is a routine occurrence — return False, log debug, keep
        going. The transport is never touched."""
        mgmt = _make_mgmt_mock()
        ws = _make_ws_manager_mock({})  # nothing registered
        mb = DaemonMailbox(mgmt=mgmt, ws_manager=ws)

        ok = mb.notify("agent-ghost", {"event": "hello"})

        assert ok is False
        mgmt.send_message.assert_not_called()

    def test_notify_returns_false_when_connection_is_gone(self):
        """Underlying transport returned False (GoneException)."""
        mgmt = _make_mgmt_mock(send_returns=False)
        ws = _make_ws_manager_mock({"agent-1": SimpleNamespace(connection_id="conn-A1")})
        mb = DaemonMailbox(mgmt=mgmt, ws_manager=ws)

        ok = mb.notify("agent-1", {"event": "hello"})

        assert ok is False

    def test_notify_swallows_transport_exception(self):
        """The old pattern wrapped every send in try/except. Mailbox
        owns that — callers should never see ``ManagementApiClientError``
        or any other exception bubble up."""
        from core.services.management_api_client import ManagementApiClientError

        mgmt = _make_mgmt_mock(raises=ManagementApiClientError("boom"))
        ws = _make_ws_manager_mock({"agent-1": SimpleNamespace(connection_id="conn-A1")})
        mb = DaemonMailbox(mgmt=mgmt, ws_manager=ws)

        ok = mb.notify("agent-1", {"event": "hello"})

        assert ok is False  # logged + swallowed


# ---------------------------------------------------------------------------
# notify_connection(connection_id, event) — direct send.
# ---------------------------------------------------------------------------


class TestNotifyConnection:
    def test_sends_directly_without_lookup(self):
        mgmt = _make_mgmt_mock(send_returns=True)
        ws = _make_ws_manager_mock()
        mb = DaemonMailbox(mgmt=mgmt, ws_manager=ws)

        ok = mb.notify_connection("conn-direct", {"event": "ack"})

        assert ok is True
        ws.get_by_agent_id.assert_not_called()  # no agent_id involved
        mgmt.send_message.assert_called_once_with("conn-direct", {"event": "ack"})

    def test_swallows_transport_exception(self):
        mgmt = _make_mgmt_mock(raises=RuntimeError("boom"))
        mb = DaemonMailbox(mgmt=mgmt, ws_manager=_make_ws_manager_mock())

        ok = mb.notify_connection("conn-direct", {"event": "ack"})

        assert ok is False


# ---------------------------------------------------------------------------
# reject(connection_id, event_type, reason, **extra) — envelope helper.
# ---------------------------------------------------------------------------


class TestReject:
    def test_builds_event_reason_envelope(self):
        mgmt = _make_mgmt_mock(send_returns=True)
        mb = DaemonMailbox(mgmt=mgmt, ws_manager=_make_ws_manager_mock())

        mb.reject("conn-x", "join_rejected", "not_in_proximity")

        mgmt.send_message.assert_called_once_with(
            "conn-x",
            {"event": "join_rejected", "reason": "not_in_proximity"},
        )

    def test_includes_extra_fields(self):
        """The audit example: ``mailbox.reject(conn_id, "join_rejected",
        "not_in_proximity", conv_id="conv-1")`` — the ``conv_id`` extra
        flows through."""
        mgmt = _make_mgmt_mock(send_returns=True)
        mb = DaemonMailbox(mgmt=mgmt, ws_manager=_make_ws_manager_mock())

        mb.reject("conn-x", "chat_rejected", "target_busy", conv_id="conv-1", target_id="tid")

        mgmt.send_message.assert_called_once_with(
            "conn-x",
            {
                "event": "chat_rejected",
                "reason": "target_busy",
                "conv_id": "conv-1",
                "target_id": "tid",
            },
        )

    def test_returns_transport_result(self):
        mgmt = _make_mgmt_mock(send_returns=False)
        mb = DaemonMailbox(mgmt=mgmt, ws_manager=_make_ws_manager_mock())

        ok = mb.reject("conn-x", "join_rejected", "x")
        assert ok is False


# ---------------------------------------------------------------------------
# disconnect(connection_id) — close.
# ---------------------------------------------------------------------------


class TestDisconnect:
    def test_calls_close_connection(self):
        mgmt = _make_mgmt_mock()
        mb = DaemonMailbox(mgmt=mgmt, ws_manager=_make_ws_manager_mock())

        mb.disconnect("conn-old")

        mgmt.close_connection.assert_called_once_with("conn-old")

    def test_swallows_close_exception(self):
        """Idempotent on gone connections — and the audit's call site in
        ``_handle_agent_connect`` already wraps close_connection in
        try/except for the same reason."""
        mgmt = _make_mgmt_mock()
        mgmt.close_connection.side_effect = RuntimeError("already gone")
        mb = DaemonMailbox(mgmt=mgmt, ws_manager=_make_ws_manager_mock())

        # Should not raise.
        mb.disconnect("conn-old")


# ---------------------------------------------------------------------------
# Singleton lifecycle.
# ---------------------------------------------------------------------------


class TestSingleton:
    def setup_method(self):
        reset_daemon_mailbox()

    def teardown_method(self):
        reset_daemon_mailbox()

    def test_get_daemon_mailbox_returns_same_instance(self):
        with (
            patch("core.services.daemon_mailbox.ManagementApiClient"),
            patch("core.services.daemon_mailbox.get_town_agent_ws_manager"),
        ):
            first = get_daemon_mailbox()
            second = get_daemon_mailbox()
        assert first is second

    def test_reset_drops_the_cached_instance(self):
        with (
            patch("core.services.daemon_mailbox.ManagementApiClient"),
            patch("core.services.daemon_mailbox.get_town_agent_ws_manager"),
        ):
            first = get_daemon_mailbox()
            reset_daemon_mailbox()
            second = get_daemon_mailbox()
        assert first is not second

    def test_get_lazily_constructs_dependencies(self):
        """The singleton should not construct ``ManagementApiClient`` at
        import time — that would touch boto3 + Settings before
        ``settings.validate()`` runs at startup. Constructor is called
        only on first ``get_daemon_mailbox()``."""
        with (
            patch("core.services.daemon_mailbox.ManagementApiClient") as mgmt_cls,
            patch("core.services.daemon_mailbox.get_town_agent_ws_manager") as ws_factory,
        ):
            # Module already loaded — neither helper should have been
            # called yet (the previous test's reset wiped the singleton).
            mgmt_cls.assert_not_called()
            ws_factory.assert_not_called()

            get_daemon_mailbox()

            mgmt_cls.assert_called_once_with()
            ws_factory.assert_called_once_with()


# ---------------------------------------------------------------------------
# Default-construction (production) sanity check.
# ---------------------------------------------------------------------------


class TestDefaultConstruction:
    def test_constructor_lazily_resolves_dependencies(self):
        """Production callers do ``DaemonMailbox()`` with no kwargs and
        expect the dependencies to be resolved at that point — not at
        import time. Mirrors the singleton's lazy behaviour for callers
        who choose to skip the singleton."""
        with (
            patch("core.services.daemon_mailbox.ManagementApiClient") as mgmt_cls,
            patch("core.services.daemon_mailbox.get_town_agent_ws_manager") as ws_factory,
        ):
            mgmt_cls.return_value = "fake-mgmt"
            ws_factory.return_value = "fake-ws"
            mb = DaemonMailbox()
        assert mb._mgmt == "fake-mgmt"
        assert mb._ws_manager == "fake-ws"

    def test_constructor_accepts_explicit_dependencies(self):
        """Tests inject mocks via kwargs; production imports the module
        with no kwargs."""
        mgmt = MagicMock()
        ws = MagicMock()
        mb = DaemonMailbox(mgmt=mgmt, ws_manager=ws)
        assert mb._mgmt is mgmt
        assert mb._ws_manager is ws


# Used by TestNotify.test_notify_swallows_transport_exception — needs the
# real exception class to verify the broad except clause catches it.
def test_management_api_client_error_is_importable():
    from core.services.management_api_client import ManagementApiClientError

    assert issubclass(ManagementApiClientError, Exception)
