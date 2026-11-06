"""Shared fixtures for all unit tests.

Provides module-level stubs that prevent unit tests from accidentally
hitting AWS when the code-under-test gains new service dependencies.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def stub_stances_service():
    """Auto-patch core.services.stances_service in daemon_bridge for all unit tests.

    _send_to_daemon now calls stances_service.get_pair_stances for each nearby
    agent.  Without this stub, tests that exercise nearby-agent paths but don't
    patch stances_service would attempt a real TownService → DynamoDB call.

    Tests that need specific stance data should add their own
    ``patch("core.services.daemon_bridge.stances_service", ...)`` context
    manager — the innermost patch wins.
    """
    no_stances = MagicMock()
    no_stances.get_pair_stances.return_value = None
    with patch("core.services.daemon_bridge.stances_service", no_stances):
        yield


@pytest.fixture
def atomic_moto_requests():
    """Make each moto DynamoDB request atomic, as real DynamoDB is.

    Moto isn't thread-safe: a failed ``transact_write_items`` restores a
    deep-copied table snapshot, wiping writes other threads committed
    meanwhile. Serializing per request keeps the interleaving *between*
    requests (the races under test) and removes moto's own.
    """
    import threading

    from moto.dynamodb.responses import DynamoHandler

    lock = threading.Lock()
    original = DynamoHandler.call_action

    def locked(self):
        with lock:
            return original(self)

    with patch.object(DynamoHandler, "call_action", locked):
        yield
