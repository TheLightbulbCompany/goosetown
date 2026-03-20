"""Tests for WebSocket connection service (DynamoDB-backed)."""

import time

from core.services.ws_connection_service import WsConnectionService


class TestRegister:
    def test_register_agent_connection(self, dynamodb_tables):
        svc = WsConnectionService()
        svc.register(
            connection_id="conn-abc",
            user_id="user-1",
            connection_type="agent",
            agent_name="goose-1",
        )
        conn = svc.get_by_connection_id("conn-abc")
        assert conn is not None
        assert conn["connectionId"] == "conn-abc"
        assert conn["userId"] == "user-1"
        assert conn["connectionType"] == "agent"
        assert conn["agentName"] == "goose-1"
        assert "connectedAt" in conn
        assert "ttl" in conn

    def test_register_viewer_connection(self, dynamodb_tables):
        svc = WsConnectionService()
        svc.register(
            connection_id="conn-viewer-1",
            user_id="user-2",
            connection_type="viewer",
        )
        conn = svc.get_by_connection_id("conn-viewer-1")
        assert conn is not None
        assert conn["connectionType"] == "viewer"
        assert conn.get("agentName") is None

    def test_register_overwrites_existing(self, dynamodb_tables):
        svc = WsConnectionService()
        svc.register("conn-1", "user-1", "agent", agent_name="goose-1")
        svc.register("conn-1", "user-1", "agent", agent_name="goose-2")
        conn = svc.get_by_connection_id("conn-1")
        assert conn["agentName"] == "goose-2"


class TestUnregister:
    def test_unregister_removes_connection(self, dynamodb_tables):
        svc = WsConnectionService()
        svc.register("conn-1", "user-1", "agent", agent_name="goose-1")
        svc.unregister("conn-1")
        assert svc.get_by_connection_id("conn-1") is None

    def test_unregister_nonexistent_is_safe(self, dynamodb_tables):
        svc = WsConnectionService()
        svc.unregister("conn-does-not-exist")  # should not raise


class TestAgentConnectionLookup:
    def test_is_agent_connected(self, dynamodb_tables):
        svc = WsConnectionService()
        assert svc.is_agent_connected("goose-1") is False

        svc.register("conn-1", "user-1", "agent", agent_name="goose-1")
        assert svc.is_agent_connected("goose-1") is True

    def test_is_agent_connected_after_disconnect(self, dynamodb_tables):
        svc = WsConnectionService()
        svc.register("conn-1", "user-1", "agent", agent_name="goose-1")
        svc.unregister("conn-1")
        assert svc.is_agent_connected("goose-1") is False

    def test_get_agent_connection_id(self, dynamodb_tables):
        svc = WsConnectionService()
        assert svc.get_agent_connection_id("goose-1") is None

        svc.register("conn-abc", "user-1", "agent", agent_name="goose-1")
        assert svc.get_agent_connection_id("goose-1") == "conn-abc"

    def test_get_agent_connection_id_returns_latest(self, dynamodb_tables):
        """If an agent reconnects, the new connection should be returned."""
        svc = WsConnectionService()
        svc.register("conn-old", "user-1", "agent", agent_name="goose-1")
        svc.unregister("conn-old")
        svc.register("conn-new", "user-1", "agent", agent_name="goose-1")
        assert svc.get_agent_connection_id("goose-1") == "conn-new"


class TestViewerConnections:
    def test_get_viewer_connection_ids(self, dynamodb_tables):
        svc = WsConnectionService()
        svc.register("conn-v1", "user-1", "viewer")
        svc.register("conn-v2", "user-2", "viewer")
        svc.register("conn-a1", "user-3", "agent", agent_name="goose-1")

        viewers = svc.get_viewer_connection_ids()
        assert "conn-v1" in viewers
        assert "conn-v2" in viewers
        assert "conn-a1" not in viewers

    def test_get_viewer_connection_ids_empty(self, dynamodb_tables):
        svc = WsConnectionService()
        assert svc.get_viewer_connection_ids() == []


class TestGetAllAgentConnections:
    def test_returns_all_agent_connections(self, dynamodb_tables):
        svc = WsConnectionService()
        svc.register("conn-1", "user-1", "agent", agent_name="goose-1")
        svc.register("conn-2", "user-2", "agent", agent_name="goose-2")
        svc.register("conn-v1", "user-3", "viewer")

        agents = svc.get_all_agent_connections()
        names = [a["agentName"] for a in agents]
        assert "goose-1" in names
        assert "goose-2" in names
        assert len(agents) == 2
