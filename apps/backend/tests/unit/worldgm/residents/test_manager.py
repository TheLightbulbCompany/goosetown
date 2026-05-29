"""Tests for ResidentManager — launch, supervise, and stop resident daemons."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import worldgm.residents.manager as manager_mod
from worldgm.residents.manager import ResidentManager
from worldgm.residents.personas import Persona


def _persona(name="tobias"):
    return Persona(
        name=name,
        display_name=name.title(),
        personality="p",
        appearance="a",
        traits=("x",),
        hangout="plaza",
    )


def _fake_town_service(agent_id="agent-1"):
    fake_ts = MagicMock()
    fake_ts.create_resident.return_value = {"id": agent_id}
    return fake_ts


# ---------------------------------------------------------------------------
# Test 1: start() creates exactly one supervisor task and run() is awaited
# ---------------------------------------------------------------------------

async def test_manager_starts_one_daemon_task(monkeypatch, tmp_path):
    fake_daemon = AsyncMock()
    monkeypatch.setattr("worldgm.residents.manager.TownDaemon", lambda **kw: fake_daemon)

    fake_ts = _fake_town_service("agent-1")
    fake_mint = MagicMock(return_value="tok")

    mgr = ResidentManager(
        personas=[_persona()],
        town_service=fake_ts,
        mint_token=fake_mint,
        ws_url="wss://x",
        base_dir=tmp_path,
    )
    await mgr.start()

    assert len(mgr.residents) == 1
    fake_daemon.run.assert_awaited()  # run scheduled + awaited as a task

    await mgr.stop()
    # stop() deactivates the row:
    fake_ts.update_agent_state.assert_called_with("agent-1", is_active="false")


# ---------------------------------------------------------------------------
# Test 2: stop() cancels the supervisor task and it finishes cleanly
# ---------------------------------------------------------------------------

async def test_stop_cancels_supervisor_task(monkeypatch, tmp_path):
    """After stop(), the supervisor task is done (cancelled or finished)."""
    fake_daemon = AsyncMock()
    monkeypatch.setattr("worldgm.residents.manager.TownDaemon", lambda **kw: fake_daemon)

    fake_ts = _fake_town_service("agent-2")
    fake_mint = MagicMock(return_value="tok")

    mgr = ResidentManager(
        personas=[_persona()],
        town_service=fake_ts,
        mint_token=fake_mint,
        ws_url="wss://x",
        base_dir=tmp_path,
    )
    await mgr.start()

    handle = mgr.residents["tobias"]
    assert handle.task is not None

    await mgr.stop()

    # Task must be finished (done includes both completed and cancelled)
    assert handle.task.done()


# ---------------------------------------------------------------------------
# Test 3: workspace and state_dir are created on disk
# ---------------------------------------------------------------------------

async def test_workspace_and_state_dir_created(monkeypatch, tmp_path):
    fake_daemon = AsyncMock()
    monkeypatch.setattr("worldgm.residents.manager.TownDaemon", lambda **kw: fake_daemon)

    fake_ts = _fake_town_service("agent-3")
    fake_mint = MagicMock(return_value="tok")

    mgr = ResidentManager(
        personas=[_persona("tobias")],
        town_service=fake_ts,
        mint_token=fake_mint,
        ws_url="wss://x",
        base_dir=tmp_path,
    )
    await mgr.start()
    await mgr.stop()

    # provision_resident builds workspace = base_dir / persona.name
    workspace = tmp_path / "tobias"
    assert workspace.exists(), "workspace dir should be created"
    assert (workspace / ".state").exists(), "state_dir should be created"


# ---------------------------------------------------------------------------
# Test 4: daemon is constructed with correct kwargs
# ---------------------------------------------------------------------------

async def test_daemon_constructed_with_correct_kwargs(monkeypatch, tmp_path):
    received_kwargs: dict = {}

    def fake_daemon_factory(**kw):
        received_kwargs.update(kw)
        return AsyncMock()

    monkeypatch.setattr("worldgm.residents.manager.TownDaemon", fake_daemon_factory)

    fake_ts = _fake_town_service("agent-4")
    fake_mint = MagicMock(return_value="my-token")

    mgr = ResidentManager(
        personas=[_persona("tobias")],
        town_service=fake_ts,
        mint_token=fake_mint,
        ws_url="wss://game",
        base_dir=tmp_path,
    )
    await mgr.start()
    await mgr.stop()

    assert received_kwargs["ws_url"] == "wss://game"
    assert received_kwargs["token"] == "my-token"
    assert received_kwargs["agent_name"] == "tobias"
    assert isinstance(received_kwargs["state_dir"], Path)
    assert isinstance(received_kwargs["workspace_path"], Path)


# ---------------------------------------------------------------------------
# Test 5: daemon.run() is called with serve_socket=False
# ---------------------------------------------------------------------------

async def test_daemon_run_called_with_serve_socket_false(monkeypatch, tmp_path):
    fake_daemon = AsyncMock()
    monkeypatch.setattr("worldgm.residents.manager.TownDaemon", lambda **kw: fake_daemon)

    fake_ts = _fake_town_service("agent-5")
    fake_mint = MagicMock(return_value="tok")

    mgr = ResidentManager(
        personas=[_persona()],
        town_service=fake_ts,
        mint_token=fake_mint,
        ws_url="wss://x",
        base_dir=tmp_path,
    )
    await mgr.start()
    await mgr.stop()

    fake_daemon.run.assert_awaited_with(serve_socket=False)


# ---------------------------------------------------------------------------
# Test 6: multiple personas each get their own supervisor task and handle
# ---------------------------------------------------------------------------

async def test_multiple_personas_each_get_a_handle(monkeypatch, tmp_path):
    fake_daemon_a = AsyncMock()
    fake_daemon_b = AsyncMock()
    call_count = 0

    def fake_daemon_factory(**kw):
        nonlocal call_count
        call_count += 1
        return fake_daemon_a if call_count == 1 else fake_daemon_b

    monkeypatch.setattr("worldgm.residents.manager.TownDaemon", fake_daemon_factory)

    fake_ts = MagicMock()
    fake_ts.create_resident.side_effect = [
        {"id": "agent-a"},
        {"id": "agent-b"},
    ]
    fake_mint = MagicMock(return_value="tok")

    mgr = ResidentManager(
        personas=[_persona("alice"), _persona("bob")],
        town_service=fake_ts,
        mint_token=fake_mint,
        ws_url="wss://x",
        base_dir=tmp_path,
    )
    await mgr.start()

    assert len(mgr.residents) == 2
    assert "alice" in mgr.residents
    assert "bob" in mgr.residents
    # Each has its own distinct task
    task_alice = mgr.residents["alice"].task
    task_bob = mgr.residents["bob"].task
    assert task_alice is not task_bob

    await mgr.stop()


# ---------------------------------------------------------------------------
# Test 7: stop() is idempotent (safe to call on a not-yet-started manager)
# ---------------------------------------------------------------------------

async def test_stop_before_start_is_safe(tmp_path):
    fake_ts = _fake_town_service()
    fake_mint = MagicMock(return_value="tok")
    mgr = ResidentManager(
        personas=[_persona()],
        town_service=fake_ts,
        mint_token=fake_mint,
        ws_url="wss://x",
        base_dir=tmp_path,
    )
    # Should not raise
    await mgr.stop()


# ---------------------------------------------------------------------------
# Test 8: deactivation failure in stop() does not raise
# ---------------------------------------------------------------------------

async def test_stop_deactivation_error_is_best_effort(monkeypatch, tmp_path):
    fake_daemon = AsyncMock()
    monkeypatch.setattr("worldgm.residents.manager.TownDaemon", lambda **kw: fake_daemon)

    fake_ts = _fake_town_service("agent-8")
    fake_ts.update_agent_state.side_effect = RuntimeError("DDB unavailable")
    fake_mint = MagicMock(return_value="tok")

    mgr = ResidentManager(
        personas=[_persona()],
        town_service=fake_ts,
        mint_token=fake_mint,
        ws_url="wss://x",
        base_dir=tmp_path,
    )
    await mgr.start()

    # Must not raise even though update_agent_state blows up
    await mgr.stop()


# ---------------------------------------------------------------------------
# Test 9: a daemon that crashes is re-provisioned and restarted after backoff
# ---------------------------------------------------------------------------

async def test_supervisor_restarts_daemon_after_crash(monkeypatch, tmp_path):
    """If daemon.run() raises, the supervisor re-provisions + constructs a new
    daemon after the backoff (proving the restart loop, not just clean exit)."""
    # Collapse the backoff so the test doesn't wait 5s.
    monkeypatch.setattr(manager_mod, "_RESTART_BACKOFF_SECONDS", 0.0)

    construct_count = 0
    second_daemon_running = asyncio.Event()

    async def run_side_effect(*args, **kwargs):
        # First daemon crashes; second one "stays up" until cancelled.
        if construct_count == 1:
            raise RuntimeError("ws drop")
        second_daemon_running.set()
        await asyncio.sleep(3600)

    def fake_daemon_factory(**kw):
        nonlocal construct_count
        construct_count += 1
        d = AsyncMock()
        d.run = AsyncMock(side_effect=run_side_effect)
        return d

    monkeypatch.setattr(manager_mod, "TownDaemon", fake_daemon_factory)

    fake_ts = _fake_town_service("agent-9")
    fake_mint = MagicMock(return_value="tok")

    mgr = ResidentManager(
        personas=[_persona()],
        town_service=fake_ts,
        mint_token=fake_mint,
        ws_url="wss://x",
        base_dir=tmp_path,
    )
    await mgr.start()

    # The restart path must construct a SECOND daemon after the first crashes.
    await asyncio.wait_for(second_daemon_running.wait(), timeout=2.0)
    assert construct_count >= 2
    # Re-provisioning mints a fresh token / row each restart.
    assert fake_ts.create_resident.call_count >= 2

    await mgr.stop()
