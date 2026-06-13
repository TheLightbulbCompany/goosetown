"""Tests for the harness WakeAdapter abstraction + per-agent coalescing.

Wave 2 Task 9 (Paperclip model). Covers:
- the factory ``get_wake_adapter`` selecting the right adapter by harness type
  (and falling back to NoneAdapter for unknown types — graceful degradation).
- ``NoneAdapter`` is a no-op (the compatibility floor).
- ``OpenClawAdapter`` / ``HttpAdapter`` POST the thin context to the configured
  URL (urllib stubbed; request asserted).
- ``ClaudeCodeAdapter`` wake doesn't raise and ensures the hook path.
- ``WakeCoordinator`` coalescing/single-flight: a burst of N ``request_wake``
  calls collapses to a single ``adapter.wake`` call; a wake requested while one
  is in-flight fires exactly one trailing wake.
- daemon integration: a ``job_offered`` event triggers ``request_wake`` (and
  still writes TOWN_OFFERS.md); a cosmetic event (``state_update``) does NOT.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from goosetown.daemon import TownDaemon
from goosetown.wake import (
    ClaudeCodeAdapter,
    HttpAdapter,
    NoneAdapter,
    OpenClawAdapter,
    WakeAdapter,
    WakeCoordinator,
    get_wake_adapter,
)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class TestFactory:
    def test_claude_code(self):
        adapter = get_wake_adapter("claude-code", {})
        assert isinstance(adapter, ClaudeCodeAdapter)

    def test_openclaw(self):
        adapter = get_wake_adapter(
            "openclaw", {"openclaw_gateway_url": "http://localhost:9999/wake"}
        )
        assert isinstance(adapter, OpenClawAdapter)
        assert adapter.gateway_url == "http://localhost:9999/wake"

    def test_http(self):
        adapter = get_wake_adapter(
            "http", {"wake_endpoint": "https://example.invalid/hook"}
        )
        assert isinstance(adapter, HttpAdapter)
        assert adapter.endpoint == "https://example.invalid/hook"

    def test_none(self):
        assert isinstance(get_wake_adapter("none", {}), NoneAdapter)

    def test_unknown_falls_back_to_none(self):
        # Graceful: an unrecognized harness type must not blow up the daemon.
        assert isinstance(get_wake_adapter("totally-made-up", {}), NoneAdapter)

    def test_empty_falls_back_to_none(self):
        assert isinstance(get_wake_adapter("", {}), NoneAdapter)


# ---------------------------------------------------------------------------
# NoneAdapter
# ---------------------------------------------------------------------------


class TestNoneAdapter:
    async def test_wake_is_noop(self):
        adapter = NoneAdapter()
        # No raise, no side effect — relies on the heartbeat.
        result = await adapter.wake("agent-1", {"event": "job_offered"})
        assert result is None


# ---------------------------------------------------------------------------
# OpenClawAdapter / HttpAdapter — HTTP model
# ---------------------------------------------------------------------------


class _CapturingURLOpen:
    """Stub for urllib.request.urlopen that records the Request it was given."""

    def __init__(self) -> None:
        self.requests: list = []

    def __call__(self, request, timeout=None):  # noqa: ANN001
        self.requests.append((request, timeout))

        class _Resp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def read(self_inner):
                return b""

        return _Resp()


class TestOpenClawAdapter:
    async def test_wake_posts_thin_context(self):
        cap = _CapturingURLOpen()
        adapter = OpenClawAdapter("http://localhost:8123/agent/wake")
        with patch("urllib.request.urlopen", cap):
            await adapter.wake("agent-7", {"event": "job_offered", "job_id": "j1"})

        assert len(cap.requests) == 1
        request, timeout = cap.requests[0]
        assert request.full_url == "http://localhost:8123/agent/wake"
        assert request.get_method() == "POST"
        body = json.loads(request.data.decode("utf-8"))
        assert body["agent_id"] == "agent-7"
        assert body["thin_context"]["event"] == "job_offered"
        assert body["thin_context"]["job_id"] == "j1"
        assert timeout is not None and timeout <= 5

    async def test_wake_swallows_errors(self):
        adapter = OpenClawAdapter("http://localhost:1/dead")

        def _boom(request, timeout=None):  # noqa: ANN001
            raise OSError("connection refused")

        with patch("urllib.request.urlopen", _boom):
            # Must not raise — best-effort, log-and-swallow.
            await adapter.wake("agent-7", {"event": "job_offered"})

    async def test_no_url_is_noop(self):
        adapter = OpenClawAdapter("")
        cap = _CapturingURLOpen()
        with patch("urllib.request.urlopen", cap):
            await adapter.wake("agent-7", {"event": "job_offered"})
        assert cap.requests == []


class TestHttpAdapter:
    async def test_wake_posts_to_endpoint(self):
        cap = _CapturingURLOpen()
        adapter = HttpAdapter(
            "https://hooks.example.invalid/gt",
            headers={"X-Token": "secret"},
        )
        with patch("urllib.request.urlopen", cap):
            await adapter.wake("a-9", {"event": "job_awarded", "job_id": "j2"})

        assert len(cap.requests) == 1
        request, _ = cap.requests[0]
        assert request.full_url == "https://hooks.example.invalid/gt"
        assert request.get_method() == "POST"
        # Custom header carried through.
        assert request.get_header("X-token") == "secret"
        body = json.loads(request.data.decode("utf-8"))
        assert body["agent_id"] == "a-9"
        assert body["thin_context"]["event"] == "job_awarded"

    async def test_wake_swallows_errors(self):
        adapter = HttpAdapter("https://dead.invalid/x")

        def _boom(request, timeout=None):  # noqa: ANN001
            raise OSError("dns failure")

        with patch("urllib.request.urlopen", _boom):
            await adapter.wake("a-9", {"event": "job_awarded"})


# ---------------------------------------------------------------------------
# ClaudeCodeAdapter — Process model
# ---------------------------------------------------------------------------


class TestClaudeCodeAdapter:
    async def test_wake_does_not_raise_without_workspace(self):
        adapter = ClaudeCodeAdapter(workspace_path=None)
        # No workspace → wake is a no-op (the installed hook + heartbeat are
        # the floor). Must not raise.
        await adapter.wake("agent-1", {"event": "job_offered"})

    async def test_wake_ensures_hook_marker(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        (workspace / ".claude").mkdir(parents=True)
        adapter = ClaudeCodeAdapter(workspace_path=workspace)
        await adapter.wake("agent-1", {"event": "job_offered"})
        # The claude-code adapter relies on the installed Stop/TeammateIdle
        # hook; its "wake" touches a trigger file the hook can observe so an
        # idle loop re-checks promptly. We assert the touch happened.
        marker = workspace / ".claude" / "goosetown-wake"
        assert marker.exists()


# ---------------------------------------------------------------------------
# WakeCoordinator — coalescing / single-flight
# ---------------------------------------------------------------------------


class _CountingAdapter(WakeAdapter):
    """Fake adapter that counts wakes and holds them open on demand."""

    def __init__(self, gate: asyncio.Event | None = None) -> None:
        self.calls: list[dict] = []
        self._gate = gate

    async def wake(self, agent_id: str, thin_context: dict) -> None:
        self.calls.append(thin_context)
        if self._gate is not None:
            await self._gate.wait()


class TestWakeCoordinator:
    async def test_single_request_fires_one_wake(self):
        adapter = _CountingAdapter()
        coord = WakeCoordinator("agent-1", adapter, debounce_sec=0.01)
        coord.request_wake({"event": "job_offered"})
        await coord.aclose()
        assert len(adapter.calls) == 1

    async def test_burst_collapses_to_one(self):
        adapter = _CountingAdapter()
        coord = WakeCoordinator("agent-1", adapter, debounce_sec=0.05)
        # Fire a burst within the debounce window — all should collapse.
        for i in range(10):
            coord.request_wake({"event": "job_offered", "n": i})
        await coord.aclose()
        assert len(adapter.calls) == 1

    async def test_request_during_inflight_fires_exactly_one_trailing(self):
        gate = asyncio.Event()
        adapter = _CountingAdapter(gate=gate)
        coord = WakeCoordinator("agent-1", adapter, debounce_sec=0.0)

        # First request → debounce (0) → starts the in-flight wake, which
        # blocks on the gate.
        coord.request_wake({"event": "job_offered", "n": 0})
        # Give the loop a moment to start the in-flight wake.
        for _ in range(50):
            await asyncio.sleep(0)
            if len(adapter.calls) == 1:
                break
        assert len(adapter.calls) == 1

        # Several more requests while in-flight → should collapse to a single
        # trailing wake (re-wake-pending flag), not N overlapping ones.
        for i in range(5):
            coord.request_wake({"event": "job_offered", "n": i + 1})
        await asyncio.sleep(0.02)
        # Still only the one in-flight wake — the trailing one is pending.
        assert len(adapter.calls) == 1

        # Release the in-flight wake; exactly one trailing wake should run.
        gate.set()
        await coord.aclose()
        assert len(adapter.calls) == 2

    async def test_sequential_waits_each_fire(self):
        adapter = _CountingAdapter()
        coord = WakeCoordinator("agent-1", adapter, debounce_sec=0.0)
        coord.request_wake({"event": "a"})
        await asyncio.sleep(0.02)
        coord.request_wake({"event": "b"})
        await coord.aclose()
        # Two distinct, non-overlapping waits → two calls.
        assert len(adapter.calls) == 2

    async def test_adapter_error_does_not_break_coordinator(self):
        class _BoomAdapter(WakeAdapter):
            def __init__(self) -> None:
                self.calls = 0

            async def wake(self, agent_id, thin_context):  # noqa: ANN001
                self.calls += 1
                raise RuntimeError("boom")

        adapter = _BoomAdapter()
        coord = WakeCoordinator("agent-1", adapter, debounce_sec=0.0)
        coord.request_wake({"event": "a"})
        await asyncio.sleep(0.02)
        # A second request still works after the first one raised.
        coord.request_wake({"event": "b"})
        await coord.aclose()
        assert adapter.calls == 2


# ---------------------------------------------------------------------------
# Daemon integration
# ---------------------------------------------------------------------------


@pytest.fixture
def daemon(tmp_path: Path) -> TownDaemon:
    state_dir = tmp_path / "state"
    workspace = tmp_path / "workspace"
    state_dir.mkdir()
    workspace.mkdir()
    return TownDaemon(
        ws_url="wss://example.invalid",
        token="t",
        agent_name="testagent",
        state_dir=state_dir,
        workspace_path=workspace,
    )


class _RecordingCoordinator:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def request_wake(self, thin_context: dict) -> None:
        self.requests.append(thin_context)


class TestDaemonWakeWiring:
    def test_job_offered_triggers_wake_and_writes_offers(self, daemon: TownDaemon):
        coord = _RecordingCoordinator()
        daemon._wake_coordinator = coord
        daemon._handle_event(
            {
                "event": "job_offered",
                "job_id": "j1",
                "title": "Write a poem",
                "category": "writing",
                "max_price": 50,
                "consumer_name": "alice",
            }
        )
        # Wake requested...
        assert len(coord.requests) == 1
        assert coord.requests[0]["event"] == "job_offered"
        assert coord.requests[0]["job_id"] == "j1"
        # ...and the files are still written (wake is in addition, not instead).
        offers = (daemon._workspace_path / "TOWN_OFFERS.md").read_text()
        assert "Write a poem" in offers

    def test_job_awarded_triggers_wake(self, daemon: TownDaemon):
        coord = _RecordingCoordinator()
        daemon._wake_coordinator = coord
        daemon._handle_event(
            {
                "event": "job_awarded",
                "job_id": "j2",
                "title": "A task",
                "description": "do the thing",
                "agreed_price": 30,
                "deadline_sec": 600,
                "provider_name": "bob",
            }
        )
        assert any(r["event"] == "job_awarded" for r in coord.requests)

    def test_cosmetic_event_does_not_trigger_wake(self, daemon: TownDaemon):
        coord = _RecordingCoordinator()
        daemon._wake_coordinator = coord
        daemon._handle_event(
            {
                "event": "state_update",
                "agent": {"location": "plaza", "activity": "idle"},
                "nearby": [],
            }
        )
        assert coord.requests == []

    def test_bid_received_does_not_trigger_wake(self, daemon: TownDaemon):
        # bid_received is a count-only cosmetic update for the consumer — no wake.
        coord = _RecordingCoordinator()
        daemon._wake_coordinator = coord
        daemon.state["exchange"] = {
            "jobs": {"j3": {"job_id": "j3", "role": "consumer"}}
        }
        daemon._handle_event({"event": "bid_received", "job_id": "j3", "bid_count": 2})
        assert coord.requests == []

    def test_owner_message_triggers_wake(self, daemon: TownDaemon):
        coord = _RecordingCoordinator()
        daemon._wake_coordinator = coord
        daemon._handle_event(
            {
                "event": "owner_message",
                "text": "hello there",
                "message_id": "m1",
                "timestamp": 123,
            }
        )
        assert any(r["event"] == "owner_message" for r in coord.requests)

    def test_no_coordinator_is_safe(self, daemon: TownDaemon):
        # The daemon may run without a coordinator (residents/in-process);
        # events must not blow up when _wake_coordinator is None.
        daemon._wake_coordinator = None
        daemon._handle_event(
            {
                "event": "job_offered",
                "job_id": "j9",
                "title": "ok",
                "consumer_name": "x",
            }
        )
        # And the file is still written.
        offers = (daemon._workspace_path / "TOWN_OFFERS.md").read_text()
        assert "ok" in offers
