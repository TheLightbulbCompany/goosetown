"""Tests for ViewerRelay."""

from core.services.viewer_relay import ViewerRelay


class TestViewerRegistration:
    def test_register_adds_connection(self):
        relay = ViewerRelay.__new__(ViewerRelay)
        relay._viewer_connections = set()
        relay.register_viewer("conn-abc")
        assert "conn-abc" in relay._viewer_connections

    def test_unregister_removes_connection(self):
        relay = ViewerRelay.__new__(ViewerRelay)
        relay._viewer_connections = {"conn-abc", "conn-def"}
        relay.unregister_viewer("conn-abc")
        assert "conn-abc" not in relay._viewer_connections
        assert "conn-def" in relay._viewer_connections

    def test_unregister_missing_connection_is_noop(self):
        relay = ViewerRelay.__new__(ViewerRelay)
        relay._viewer_connections = set()
        relay.unregister_viewer("conn-xyz")
        assert len(relay._viewer_connections) == 0

    def test_register_deduplicates(self):
        relay = ViewerRelay.__new__(ViewerRelay)
        relay._viewer_connections = set()
        relay.register_viewer("conn-abc")
        relay.register_viewer("conn-abc")
        assert len(relay._viewer_connections) == 1
