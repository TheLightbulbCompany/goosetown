"""Daemon mailbox — the seam between handlers and daemon WebSockets.

Surfaced 2026-05-04 as architecture audit Candidate 5. Closes a long-running
"shallow envelope" pattern where 19 instantiation sites + 32 method calls
across six modules each repeated:

    mgmt = ManagementApiClient()
    try:
        mgmt.send_message(conn_id, {"event": "...", ...})
    except Exception as exc:
        logger.warning("Failed to send X to Y: %s", ..., exc)

The vocabulary that handlers actually want is *events to specific
recipients* — "send ``chat_rejected`` to the initiator", "send
``conversation_invite`` to the target". Today every caller has to know:

  1. Look up the ``connection_id`` (from ``agent_id`` via ``ws_manager``,
     or from the request).
  2. Construct the event envelope.
  3. Send via ``ManagementApiClient.send_message``.
  4. Catch the exception and decide whether to keep going.

That's 4 steps stamped into every handler. ``DaemonMailbox`` is the
single seam that owns those four steps.

Design: one production adapter wrapping ``ManagementApiClient`` + the
``ws_manager`` registry. Tests instantiate ``DaemonMailbox`` directly
with mock dependencies; production callers import the module-level
``get_daemon_mailbox()`` singleton.

This module is the **port**; ``ManagementApiClient`` is the **adapter**
underneath it. The mattpocock-skill rule "one adapter = hypothetical
seam, two adapters = real seam" applies — historically every caller
patched ``MGMT_PATH = "core.services.management_api_client.ManagementApiClient"``,
which means the test adapter already existed informally. This module
formalises it.

This is Phase 1 of Candidate 5. Migrating all 19+ call sites is a
mechanical follow-up that lands in Phase 2/3 PRs to keep review small.
"""

from __future__ import annotations

import logging
from typing import Any

from core.services.management_api_client import ManagementApiClient
from core.services.town_agent_ws import get_town_agent_ws_manager

logger = logging.getLogger(__name__)


class DaemonMailbox:
    """Send events to daemon WebSocket clients via API Gateway.

    Owns the ``connection_id`` lookup, envelope construction, and
    swallow-and-log-on-failure behaviour. Handlers depend on this class,
    not on ``ManagementApiClient`` directly — the audit's deletion test
    on the latter found it earned its keep as a transport but not as a
    vocabulary.

    Args:
        mgmt: Underlying transport. Tests inject a mock; production
            callers omit and let the constructor build a fresh client.
        ws_manager: Connection registry used by ``notify(agent_id, …)``
            to look up ``connection_id``. Tests inject a mock; production
            callers omit and the singleton is fetched.
    """

    def __init__(
        self,
        *,
        mgmt: ManagementApiClient | None = None,
        ws_manager: Any | None = None,
    ) -> None:
        self._mgmt = mgmt if mgmt is not None else ManagementApiClient()
        self._ws_manager = ws_manager if ws_manager is not None else get_town_agent_ws_manager()

    # ---- agent_id-keyed sends ----------------------------------------------

    def notify(self, agent_id: str, event: dict[str, Any]) -> bool:
        """Send ``event`` to the agent's daemon. Looks up ``connection_id``
        from ``ws_manager``.

        Returns True if delivered, False if the agent has no connection
        registered or the underlying transport reports the connection is
        gone. Failures are logged at WARNING and swallowed — callers can
        proceed with downstream logic that doesn't depend on delivery.
        """
        conn = self._ws_manager.get_by_agent_id(agent_id)
        if conn is None:
            logger.debug(
                "DaemonMailbox.notify: no connection for agent %s; event=%r dropped",
                agent_id,
                event.get("event"),
            )
            return False
        return self.notify_connection(conn.connection_id, event)

    # ---- connection_id-keyed sends -----------------------------------------

    def notify_connection(self, connection_id: str, event: dict[str, Any]) -> bool:
        """Send ``event`` to a known WebSocket connection.

        For callers that already have ``connection_id`` in hand (e.g. the
        handler dispatch path that receives it from API Gateway). Returns
        True on delivery, False on gone-connection or transport failure.
        Always logs a WARNING on transport failure; never raises — the
        old ``mgmt.send_message`` raised ``ManagementApiClientError`` and
        every caller wrapped it in ``try/except``.
        """
        try:
            delivered = self._mgmt.send_message(connection_id, event)
        except Exception as exc:
            # Non-gone transport error (throttle, network blip). Do NOT prune —
            # the connection may still be live; let the next send retry it.
            logger.warning(
                "DaemonMailbox.notify_connection: send failed for connection %s, event=%r: %s",
                connection_id[:12],
                event.get("event"),
                exc,
            )
            return False
        if not delivered:
            # GoneException — the connection is dead. Prune it from the registry
            # and the durable connections table so it isn't retried on every
            # broadcast cycle. Unpruned dead connections flooded the event loop
            # and crash-looped the backend (2026-06-26): the stale rows survived
            # task restarts because nothing deleted them from DynamoDB.
            self._prune_dead_connection(connection_id)
        return delivered

    def _prune_dead_connection(self, connection_id: str) -> None:
        """Best-effort removal of a gone connection from the in-memory registry
        and the durable connections table. Never raises — pruning is cleanup,
        not the caller's concern, so a failure here must not change the delivery
        verdict or break the 57 call sites that depend on notify_connection."""
        try:
            self._ws_manager.unregister(connection_id)
        except Exception:
            logger.debug("prune: ws_manager.unregister failed for %s", connection_id[:12], exc_info=True)
        try:
            from core import dynamo

            dynamo.delete_connection(connection_id)
        except Exception:
            logger.debug("prune: delete_connection failed for %s", connection_id[:12], exc_info=True)

    # ---- common patterns ----------------------------------------------------

    def reject(
        self,
        connection_id: str,
        event_type: str,
        reason: str,
        **extra: Any,
    ) -> bool:
        """Send a structured rejection event.

        Replaces the ``_reject`` closure pattern that appears verbatim in
        ``conversation_service.py`` (4 sites), ``activity_service.py``,
        and ``context_transition_service.py``:

            def _reject(reason: str) -> None:
                try:
                    mgmt.send_message(
                        connection_id,
                        {"event": "<x>_rejected", "reason": reason},
                    )
                except Exception as exc:
                    logger.warning("Failed to send <x>_rejected to %s: %s", id, exc)

        Becomes::

            mailbox.reject(connection_id, "<x>_rejected", reason)

        Extra fields can be attached via kwargs, e.g.::

            mailbox.reject(connection_id, "join_rejected", "not_in_proximity",
                           conv_id="conv-1")
        """
        return self.notify_connection(
            connection_id,
            {"event": event_type, "reason": reason, **extra},
        )

    # ---- connection lifecycle ----------------------------------------------

    def disconnect(self, connection_id: str) -> None:
        """Force-close a WebSocket connection. Idempotent on gone connections.

        Used by the kick-old-connection path in
        ``agent_connect_service._handle_agent_connect`` and any future
        admin-kill-switch path. Failures log WARNING and are swallowed
        because we always try to clean up local state regardless.
        """
        try:
            self._mgmt.close_connection(connection_id)
        except Exception as exc:
            logger.warning(
                "DaemonMailbox.disconnect: close failed for connection %s: %s",
                connection_id[:12],
                exc,
            )


# ---------------------------------------------------------------------------
# Singleton accessor.
# ---------------------------------------------------------------------------

_singleton: DaemonMailbox | None = None


def get_daemon_mailbox() -> DaemonMailbox:
    """Return the process-wide DaemonMailbox singleton.

    Production handlers use this. Tests should patch this function to
    inject a fake mailbox::

        with patch("core.services.daemon_mailbox.get_daemon_mailbox", return_value=fake):
            ...

    Or instantiate ``DaemonMailbox(mgmt=..., ws_manager=...)`` directly
    with mock dependencies for the new style.
    """
    global _singleton
    if _singleton is None:
        _singleton = DaemonMailbox()
    return _singleton


def reset_daemon_mailbox() -> None:
    """Test helper — drop the cached singleton between tests.

    Without this, a test that patches ``ManagementApiClient`` would
    contaminate the next test if the singleton was already constructed.
    """
    global _singleton
    _singleton = None
