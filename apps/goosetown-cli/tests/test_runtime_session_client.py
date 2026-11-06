import asyncio
import json
import time
from pathlib import Path

import pytest

from goosetown.daemon import TownDaemon
from goosetown.runtime_join import EnrollmentError, _session_if_required


def test_session_requires_fresh_challenge_and_rejects_bad_expiry(monkeypatch):
    from goosetown import runtime_join
    calls = []
    class Member:
        session = "abcdefghijkl/synthetic"
        def identity(self): return "a" * 52
        def prove(self, data):
            assert data == "encoded"
            calls.append("sign")
            return "signature"
    def post(url, token, body):
        calls.append(url.rsplit("/", 1)[-1])
        if url.endswith("challenge"):
            return {"challenge_id": "x" * 43, "expires_at": int(time.time()) + 100, "signing_bytes_b64url": "encoded"}
        return {"runtime_session_token": "gtrs_synthetic", "expires_at": int(time.time()) + 250}
    monkeypatch.setattr(runtime_join, "_post", post)
    result = _session_if_required("http://localhost:8877/api/v1", "town", Member(), "a" * 52, {"actor_id": "actor", "runtime_session_required": True})
    assert calls == ["challenge", "sign", "complete"]
    assert result["actor_id"] == "actor"
    monkeypatch.setattr(runtime_join.time, "time", lambda: 9999999999)
    with pytest.raises(EnrollmentError):
        _session_if_required("http://localhost:8877/api/v1", "town", Member(), "a" * 52, {"actor_id": "actor", "runtime_session_required": "yes"})


def test_refresh_waits_for_same_actor_before_action_and_failure_denies(monkeypatch, tmp_path):
    import websockets
    daemon = TownDaemon("ws://localhost:9000", "town", "goose", tmp_path, Path(tmp_path))
    daemon._runtime_session = {"runtime_session_token":"gtrs_old", "expires_at":int(time.time()) + 1, "actor_id":"actor", "api_url":"http://localhost:8877", "managed":True}
    daemon._runtime_authenticated = True
    events = []
    class Old:
        async def close(self): events.append("old closed")
        async def send(self, _): raise AssertionError("old socket used")
    class New:
        async def send(self, data): events.append(json.loads(data)["type"])
        async def recv(self):
            events.append("verified connected")
            return json.dumps({"event":"connected", "agent":{"id":"actor"}})
        async def close(self): events.append("new closed")
    daemon.ws = Old()
    async def refresh():
        events.append("renew")
        daemon._runtime_session = {**daemon._runtime_session, "runtime_session_token":"gtrs_new", "expires_at":int(time.time())+250}
    async def connect(*args, **kwargs): return New()
    def connected(data):
        assert data["agent"]["id"] == "actor"
        daemon._runtime_authenticated = True
    monkeypatch.setattr(daemon, "_refresh_runtime_credential", refresh)
    monkeypatch.setattr(daemon, "_on_connected", connected)
    monkeypatch.setattr(websockets, "connect", connect)
    asyncio.run(daemon._ensure_runtime_session())
    assert events == ["renew", "town_agent_connect", "verified connected", "old closed"]
    assert daemon._runtime_authenticated
    daemon._runtime_session["expires_at"] = 0
    async def fail(): raise ValueError("no secret should be logged")
    monkeypatch.setattr(daemon, "_refresh_runtime_credential", fail)
    result = asyncio.run(daemon.handle_tool_command({"action":"act", "payload":{"action":"idle"}}))
    assert "renewal failed" in result["error"]
    assert not daemon._runtime_authenticated


def test_real_ipc_survives_refresh_and_waits_for_connected(monkeypatch, tmp_path):
    import websockets
    async def scenario():
        import tempfile
        short = tempfile.TemporaryDirectory(prefix="gt-", dir="/tmp")
        daemon = TownDaemon("ws://localhost:9000", "town", "goose", Path(short.name), tmp_path)
        daemon._runtime_session = {"runtime_session_token":"gtrs_old", "expires_at":int(time.time()) + 1, "actor_id":"actor", "api_url":"http://localhost:8877", "managed":True}
        daemon._runtime_authenticated = True
        events=[]; permit=asyncio.Event()
        class Old:
            async def close(self): events.append("old-close")
            async def send(self, data): raise AssertionError("old socket used")
        class New:
            async def send(self, data):
                message = json.loads(data)
                events.append(message["type"])
                if message["type"] == "town_agent_act":
                    daemon._handle_event({"event":"action_result", "request_id":message["request_id"], "status":"accepted"})
            async def recv(self):
                await permit.wait()
                return json.dumps({"event":"connected","agent":{"id":"actor"}})
            async def close(self): pass
        async def refresh():
            daemon._runtime_session = {**daemon._runtime_session, "runtime_session_token":"gtrs_new", "expires_at":int(time.time())+250}
        async def connect(*args, **kwargs): return New()
        def connected(data):
            assert data["agent"]["id"] == "actor"
            daemon._runtime_authenticated = True
        daemon.ws = Old()
        monkeypatch.setattr(daemon, "_refresh_runtime_credential", refresh)
        monkeypatch.setattr(daemon, "_on_connected", connected)
        monkeypatch.setattr(websockets, "connect", connect)
        server=asyncio.create_task(daemon.run_socket_server())
        try:
            for _ in range(50):
                if daemon._sock_path.exists(): break
                await asyncio.sleep(.01)
            async def call():
                reader, writer = await asyncio.open_unix_connection(str(daemon._sock_path))
                writer.write(json.dumps({"action":"act", "payload":{"action":"idle"}}).encode())
                await writer.drain(); writer.write_eof()
                result=json.loads(await reader.read()); writer.close(); await writer.wait_closed()
                return result
            first=asyncio.create_task(call())
            await asyncio.sleep(.05)
            assert events == ["town_agent_connect"]
            permit.set()
            assert "error" not in await asyncio.wait_for(first, 3)
            assert events == ["town_agent_connect", "old-close", "town_agent_act"]
            assert "error" not in await asyncio.wait_for(call(), 3)
            assert events[-1] == "town_agent_act"
        finally:
            daemon.running=False
            await asyncio.wait_for(server, 2)
            short.cleanup()
    asyncio.run(scenario())


def test_failed_renewal_wakes_listener_and_reconnect_can_recover(monkeypatch, tmp_path):
    import websockets

    async def scenario():
        daemon = TownDaemon("ws://localhost:9000", "town", "goose", tmp_path, tmp_path)
        daemon._runtime_session = {"runtime_session_token": "gtrs_old", "expires_at": int(time.time()) + 1,
                                   "actor_id": "actor", "api_url": "http://localhost:8877", "managed": True}
        daemon._runtime_authenticated = True
        closed = asyncio.Event()
        sent = []

        class Old:
            async def recv(self):
                await closed.wait()
                raise ConnectionError("closed")

            async def close(self):
                closed.set()

        class New:
            async def send(self, raw):
                sent.append(json.loads(raw))

            async def close(self):
                pass

        attempts = 0

        async def refresh():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ConnectionError("temporary outage")
            daemon._runtime_session = {**daemon._runtime_session, "runtime_session_token": "gtrs_new",
                                       "expires_at": int(time.time()) + 250}

        async def connect(*args, **kwargs):
            return New()

        daemon.ws = Old()
        monkeypatch.setattr(daemon, "_refresh_runtime_credential", refresh)
        monkeypatch.setattr(websockets, "connect", connect)
        listener = asyncio.create_task(daemon.listen_ws())
        await asyncio.sleep(0)
        with pytest.raises(ConnectionError):
            await daemon._ensure_runtime_session()
        await asyncio.wait_for(listener, 1)
        assert closed.is_set() and not daemon._runtime_authenticated
        assert daemon._runtime_session["expires_at"] == 0
        # run() can now return to its existing backoff/reconnect loop.
        await daemon.connect_ws()
        assert attempts == 2
        assert sent[0]["runtime_session_token"] == "gtrs_new"
        assert not daemon._runtime_authenticated  # still waiting for connected ACK

    asyncio.run(scenario())


def test_old_socket_disconnect_during_renewal_does_not_end_listener(monkeypatch, tmp_path):
    import websockets
    async def scenario():
        d=TownDaemon("ws://localhost:9000", "town", "goose", tmp_path, tmp_path)
        d._runtime_session={"runtime_session_token":"gtrs_old", "expires_at":int(time.time())+1,"actor_id":"actor","api_url":"http://localhost:8877","managed":True}
        d._runtime_authenticated=True
        kicked=asyncio.Event(); ack=asyncio.Event(); connected=asyncio.Event()
        class Old:
            async def recv(self):
                await kicked.wait()
                raise ConnectionError("old kicked")
            async def close(self): pass
        class New:
            async def send(self, data): kicked.set()
            async def recv(self):
                await ack.wait()
                if not connected.is_set():
                    connected.set()
                    return json.dumps({"event":"connected", "agent":{"id":"actor"}})
                await asyncio.Future()
            async def close(self): pass
        async def connect(*args, **kwargs): return New()
        async def refresh(): d._runtime_session={**d._runtime_session,"expires_at":int(time.time())+250,"runtime_session_token":"gtrs_new"}
        def on_connected(data): d._runtime_authenticated=True
        d.ws=Old()
        monkeypatch.setattr(websockets,"connect",connect)
        monkeypatch.setattr(d,"_refresh_runtime_credential",refresh)
        monkeypatch.setattr(d,"_on_connected",on_connected)
        listener=asyncio.create_task(d.listen_ws())
        renewal=asyncio.create_task(d._ensure_runtime_session())
        await asyncio.wait_for(kicked.wait(), 1)
        await asyncio.sleep(.01)
        assert not listener.done()
        ack.set(); await asyncio.wait_for(renewal, 1)
        assert not listener.done() and d._runtime_authenticated
        listener.cancel()
        with pytest.raises(asyncio.CancelledError): await listener
    asyncio.run(scenario())


def test_daemon_handoff_sends_only_fd_number_in_environment(monkeypatch, tmp_path):
    import os
    import threading
    from goosetown import core
    from goosetown.config import state_dir
    name = "fixture-handoff-" + os.urandom(4).hex()
    sd = state_dir(name)
    captured = {}
    threads = []
    class Process:
        def poll(self): return None
    def spawn(argv, **kwargs):
        assert "gtrs_secret_fixture" not in repr(argv)
        assert "gtrs_secret_fixture" not in repr(kwargs["env"])
        fd = kwargs["pass_fds"][0]
        assert kwargs["env"]["GOOSETOWN_RUNTIME_SESSION_FD"] == str(fd)
        copy = os.dup(fd)
        def reader():
            with os.fdopen(copy, "rb") as stream:
                captured.update(json.load(stream))
        worker = threading.Thread(target=reader)
        worker.start(); threads.append(worker)
        (sd / "state.json").write_text('{"connected":true}')
        return Process()
    monkeypatch.setattr(core.subprocess, "Popen", spawn)
    session={"runtime_session_token":"gtrs_secret_fixture", "expires_at":int(time.time())+200, "actor_id":"actor", "api_url":"http://localhost:8877", "managed":True}
    try:
        assert core._start_daemon({"agent":name, "token":"town", "workspace_path":str(tmp_path)}, tmp_path, session)["connected"]
        threads[0].join(timeout=2)
        assert captured == session
        assert "gtrs_secret_fixture" not in (sd / "state.json").read_text()
        assert "gtrs_secret_fixture" not in (sd / "daemon.log").read_text()
    finally:
        for path in sd.iterdir(): path.unlink()
        sd.rmdir()


def test_wrong_action_ack_cannot_complete_and_timeout_is_uncertain(monkeypatch, tmp_path):
    async def scenario():
        d=TownDaemon("ws://localhost", "town", "goose", tmp_path, tmp_path)
        d._runtime_session={"runtime_session_token":"gtrs_test", "expires_at":int(time.time())+200,"actor_id":"actor","api_url":"http://localhost:8877","managed":True}
        d._runtime_authenticated=True
        class WS:
            async def send(self, raw):
                d._handle_event({"event":"action_result", "request_id":"wrong", "status":"accepted"})
        d.ws=WS()
        original=asyncio.wait_for
        async def short(awaitable, timeout): return await original(awaitable, .03 if timeout == 15 else timeout)
        monkeypatch.setattr(asyncio,"wait_for",short)
        result=await d.handle_tool_command({"action":"act", "payload":{"action":"idle"}})
        assert "uncertain" in result["error"]
        assert d._action_results == {}
    asyncio.run(scenario())


def test_dispatch_uncertainty_is_returned_without_retry(monkeypatch, tmp_path):
    async def scenario():
        daemon = TownDaemon("ws://localhost", "town", "goose", tmp_path, tmp_path)
        daemon._runtime_session = {"runtime_session_token": "gtrs_test", "expires_at": int(time.time()) + 200,
                                   "actor_id": "actor", "api_url": "http://localhost:8877", "managed": True}
        daemon._runtime_authenticated = True
        sent = []

        class WS:
            async def send(self, raw):
                message = json.loads(raw)
                sent.append(message)
                daemon._handle_event({"event": "action_result", "request_id": message["request_id"],
                                      "status": "uncertain", "error": "dispatch_failed"})

        daemon.ws = WS()
        result = await asyncio.wait_for(daemon.handle_tool_command({"action": "act", "payload": {"action": "idle"}}), 1)
        assert result["status"] == "uncertain"
        assert result["reason"] == "dispatch_failed"
        assert len(sent) == 1
        assert daemon._action_results == {}

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["chat", "move", "post_job"])
@pytest.mark.parametrize("failure", ["send", "timeout", "rejected", "uncertain"])
def test_failed_action_clears_pending_outcomes(monkeypatch, tmp_path, action, failure):
    async def scenario():
        daemon = TownDaemon("ws://localhost", "town", "goose", tmp_path, tmp_path)
        daemon._runtime_session = {"runtime_session_token": "gtrs_test", "expires_at": int(time.time()) + 200,
                                   "actor_id": "actor", "api_url": "http://localhost:8877", "managed": True}
        daemon._runtime_authenticated = True
        daemon._arrived_event = asyncio.Event()

        class WS:
            async def send(self, raw):
                message = json.loads(raw)
                if failure == "send":
                    raise ConnectionError("send failed")
                if failure != "timeout":
                    daemon._handle_event({"event": "action_result", "request_id": message["request_id"],
                                          "status": failure, "error": "fixture"})

        original_wait = asyncio.wait_for

        async def short_wait(awaitable, timeout):
            return await original_wait(awaitable, .01 if timeout == 15 else timeout)

        monkeypatch.setattr(asyncio, "wait_for", short_wait)
        daemon.ws = WS()
        result = await daemon.handle_tool_command({"action": "act", "payload": {"action": action, "destination": "cafe"}})
        assert "error" in result or result["status"] == "rejected"
        assert daemon._chat_outcome is None
        assert daemon._move_outcome is None
        assert daemon._pending_move_destination is None
        assert daemon._exchange_outcome is None
        assert daemon._pending_exchange_payload is None
        assert daemon._action_results == {}

    asyncio.run(scenario())
