"""Harness wake adapters + per-agent coalescing (Wave 2 Task 9).

We can't push a turn into a harness we don't own, so a "wake" is an
event-*triggered* nudge for the agent's loop to run *now* — "something changed,
read your files." The files (TOWN_OFFERS.md, TOWN_JOBS.md, …) are the payload;
the wake only says *go look*. This is the Paperclip model: Process adapters
(claude-code) vs HTTP adapters (openclaw / webhook) hanging off a per-agent
wakeup queue with coalescing.

Two pieces:

``WakeAdapter`` (+ subclasses + ``get_wake_adapter`` factory)
    The transport. ``async def wake(agent_id, thin_context)``. Best-effort:
    adapters swallow+log their own errors so a dead gateway never breaks the
    event handler that triggered the wake.

``WakeCoordinator``
    The per-agent coalescing / single-flight wrapper (codex P2). Holds at most
    ONE in-flight wake per agent plus a debounce window and a "re-wake pending"
    flag. A burst of N ``request_wake`` calls collapses to a single wake (files
    are *set* state — one read sees all the changes); a wake requested while one
    is in-flight sets the pending flag and fires exactly one trailing wake when
    the in-flight one completes, rather than starting an overlapping turn (which
    would race the same workspace → duplicate bids / stale reads).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request
from pathlib import Path

_log = logging.getLogger("town_daemon.wake")

# Short timeout for HTTP wakes — the wake is a fire-and-forget nudge, not an RPC
# we wait on. A slow/dead gateway must not stall the daemon's event loop.
_HTTP_TIMEOUT_SEC = 3.0

# Default debounce window for the coordinator. A burst of events within this
# window collapses to a single wake.
_DEFAULT_DEBOUNCE_SEC = 0.5


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


class WakeAdapter:
    """Base class. ``wake`` = "something changed, read your files" (thin)."""

    async def wake(self, agent_id: str, thin_context: dict) -> None:
        """Nudge the agent's loop to run now. Best-effort; never raises."""
        raise NotImplementedError


class NoneAdapter(WakeAdapter):
    """No-op adapter — the compatibility floor.

    The agent polls its files on its own heartbeat; there is no out-of-band
    wake. Universal, graceful degradation, today's behaviour. Also the fallback
    the factory returns for an unknown harness type.
    """

    async def wake(self, agent_id: str, thin_context: dict) -> None:  # noqa: ARG002
        return None


class ClaudeCodeAdapter(WakeAdapter):
    """Process model — relies on the installed Stop/TeammateIdle hook.

    The real wake mechanism for claude-code is the ``check-town-status.sh``
    Stop/TeammateIdle hook (installed by ``goosetown setup-hooks``): when the
    agent's loop goes idle, the hook re-checks TOWN_STATUS.md and continues the
    loop if there's something to do. The heartbeat is the floor.

    For the daemon's purposes the "wake" is best-effort: if a workspace is
    present we touch a small trigger file under ``.claude/`` so a hook (or the
    agent itself) can observe that the daemon nudged it; otherwise it's a no-op
    beyond the installed hook. We deliberately keep this simple — claude-code
    wake is *the hook*, not an RPC.
    """

    _MARKER_NAME = "goosetown-wake"

    def __init__(self, workspace_path: Path | None = None) -> None:
        self.workspace_path = workspace_path

    async def wake(self, agent_id: str, thin_context: dict) -> None:  # noqa: ARG002
        if not self.workspace_path:
            return None
        try:
            claude_dir = self.workspace_path / ".claude"
            if not claude_dir.exists():
                # Don't create .claude/ ourselves — if Claude Code isn't set up
                # here the hook can't fire anyway. The heartbeat remains the floor.
                return None
            marker = claude_dir / self._MARKER_NAME
            marker.write_text(
                json.dumps({"woke_at": time.time(), "thin_context": thin_context})
            )
        except Exception:
            _log.debug("claude-code wake touch failed", exc_info=True)
        return None


class _HttpPostAdapter(WakeAdapter):
    """Shared HTTP-POST machinery for the openclaw + webhook adapters."""

    def __init__(self, url: str, headers: dict | None = None) -> None:
        self._url = url
        self._headers = dict(headers or {})

    async def wake(self, agent_id: str, thin_context: dict) -> None:
        if not self._url:
            return None
        payload = json.dumps(
            {"agent_id": agent_id, "thin_context": thin_context}
        ).encode("utf-8")
        headers = {"Content-Type": "application/json", **self._headers}
        url = self._url
        try:
            # urlopen is blocking — run it off the event loop so a slow gateway
            # can't stall the daemon. Errors are swallowed+logged (best-effort).
            await asyncio.to_thread(self._do_post, url, payload, headers)
        except Exception:
            _log.warning("wake POST to %s failed", url, exc_info=True)
        return None

    @staticmethod
    def _do_post(url: str, payload: bytes, headers: dict) -> None:
        request = urllib.request.Request(
            url, data=payload, headers=headers, method="POST"
        )
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SEC) as resp:
            resp.read()


class OpenClawAdapter(_HttpPostAdapter):
    """HTTP model — POST the thin context to the local OpenClaw gateway.

    Prior art: Paperclip ships an ``openclaw_gateway`` adapter that wakes an
    OpenClaw agent over HTTP. The gateway URL comes from config/env
    (``openclaw_gateway_url`` / ``OPENCLAW_GATEWAY_URL``).
    """

    def __init__(self, gateway_url: str) -> None:
        super().__init__(gateway_url)

    @property
    def gateway_url(self) -> str:
        return self._url


class HttpAdapter(_HttpPostAdapter):
    """Generic webhook — POST the thin context to a configured endpoint."""

    def __init__(self, endpoint: str, headers: dict | None = None) -> None:
        super().__init__(endpoint, headers)

    @property
    def endpoint(self) -> str:
        return self._url


def get_wake_adapter(harness_type: str, config: dict) -> WakeAdapter:
    """Factory: build the WakeAdapter for ``harness_type``.

    Unknown / empty types fall back to ``NoneAdapter`` (graceful degradation —
    a typo or a newer harness name must never crash the daemon; the heartbeat
    keeps working).
    """
    config = config or {}
    if harness_type == "claude-code":
        workspace = config.get("workspace_path")
        return ClaudeCodeAdapter(workspace_path=Path(workspace) if workspace else None)
    if harness_type == "openclaw":
        url = config.get("openclaw_gateway_url") or config.get("gateway_url") or ""
        return OpenClawAdapter(url)
    if harness_type == "http":
        endpoint = config.get("wake_endpoint") or config.get("http_endpoint") or ""
        headers = config.get("wake_headers")
        return HttpAdapter(endpoint, headers=headers)
    if harness_type == "none":
        return NoneAdapter()
    # Unknown harness → compatibility floor.
    _log.debug("unknown harness type %r → NoneAdapter", harness_type)
    return NoneAdapter()


# ---------------------------------------------------------------------------
# Coordinator — per-agent coalescing / single-flight
# ---------------------------------------------------------------------------


class WakeCoordinator:
    """Per-agent wake queue with coalescing + single-flight (codex P2).

    Invariants:
      - At most ONE in-flight ``adapter.wake`` per agent at a time (no two
        overlapping turns racing the same workspace).
      - A burst of ``request_wake`` calls within the debounce window collapses
        to a single wake (files are set-state — one read sees all of it).
      - A ``request_wake`` while a wake is in-flight sets a "re-wake pending"
        flag and fires *exactly one* trailing wake on completion (collapsing the
        burst-during-flight to one follow-up).

    ``request_wake`` is synchronous and non-blocking — safe to call from the
    daemon's event handlers. The actual wake runs in a background task driven
    off the running event loop.
    """

    def __init__(
        self,
        agent_id: str,
        adapter: WakeAdapter,
        debounce_sec: float = _DEFAULT_DEBOUNCE_SEC,
    ) -> None:
        self._agent_id = agent_id
        self._adapter = adapter
        self._debounce_sec = max(0.0, debounce_sec)

        self._pending = False
        self._latest_context: dict = {}
        self._task: asyncio.Task | None = None

    def request_wake(self, thin_context: dict) -> None:
        """Request a wake. Non-blocking; coalesces with any in-flight/pending wake."""
        self._latest_context = dict(thin_context or {})
        self._pending = True
        # Start the processor if it isn't already running. An already-running
        # processor will pick up the pending flag itself (collapsing the burst).
        if self._task is None or self._task.done():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # No running loop (e.g. a sync-only test or shutdown path) —
                # the heartbeat remains the floor; nothing to do.
                _log.debug("request_wake with no running loop; skipping")
                return
            self._task = loop.create_task(self._run())

    async def _run(self) -> None:
        """Drain the pending flag: debounce → single in-flight wake → repeat."""
        # Loop as long as there's pending work. Each iteration consumes the
        # pending flag *before* the wake; anything set during the wake schedules
        # exactly one more iteration (the trailing wake), collapsing the burst.
        while self._pending:
            self._pending = False
            if self._debounce_sec > 0:
                # Coalesce a burst arriving within the window.
                await asyncio.sleep(self._debounce_sec)
                # Anything that arrived during the debounce is already folded
                # into _latest_context; clear the flag again so it doesn't
                # spuriously trigger a trailing iteration.
                self._pending = False
            context = dict(self._latest_context)
            try:
                await self._adapter.wake(self._agent_id, context)
            except Exception:
                # Adapters are supposed to swallow their own errors, but never
                # let a misbehaving adapter break the coalescing loop.
                _log.warning("wake adapter raised", exc_info=True)
            # If request_wake fired while the wake was in-flight, _pending is
            # now True → the loop runs once more (exactly one trailing wake).

    async def aclose(self) -> None:
        """Wait for any in-flight/pending wake to drain (used by tests + shutdown)."""
        task = self._task
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:  # pragma: no cover - shutdown path
                pass
