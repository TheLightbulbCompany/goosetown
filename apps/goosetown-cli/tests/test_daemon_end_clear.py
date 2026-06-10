"""#80 — `act end` must let an immediate `act move` through.

`active_conversation` was only cleared by the server's `conversation_ended`
event, so `act end` returned ok while the local guard still saw the agent as
"in a conversation" — an immediate `act move` got a spurious `in_conversation`
rejection until the round-trip landed. The daemon now optimistically clears the
local conversation state when the agent itself ends the (matching) conversation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from goosetown.daemon import TownDaemon


class _FakeWS:
    async def send(self, _msg: str) -> None:  # noqa: D401
        return None


@pytest.fixture
def daemon(tmp_path: Path) -> TownDaemon:
    state_dir = tmp_path / "state"
    workspace = tmp_path / "workspace"
    state_dir.mkdir()
    workspace.mkdir()
    d = TownDaemon(
        ws_url="wss://example.invalid",
        token="t",
        agent_name="testagent",
        state_dir=state_dir,
        workspace_path=workspace,
    )
    d.ws = _FakeWS()
    return d


@pytest.mark.asyncio
async def test_end_clears_matching_active_conversation(daemon: TownDaemon):
    daemon.state["active_conversation"] = {"conv_id": "c1", "participants": ["X"]}
    daemon.state["pending_messages"] = [{"from": "X", "text": "hi"}]
    await daemon.handle_tool_command(
        {"action": "act", "payload": {"action": "end_conversation", "conv_id": "c1"}}
    )
    assert daemon.state.get("active_conversation") is None
    assert daemon.state["pending_messages"] == []


@pytest.mark.asyncio
async def test_end_does_not_clear_a_different_conversation(daemon: TownDaemon):
    daemon.state["active_conversation"] = {"conv_id": "c2"}
    await daemon.handle_tool_command(
        {"action": "act", "payload": {"action": "end_conversation", "conv_id": "c1"}}
    )
    assert daemon.state.get("active_conversation") == {"conv_id": "c2"}
