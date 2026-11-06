"""Real CLI + Moto backend routes + real Rust member; no cloud or paid calls."""

import argparse
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT / "apps/backend"),
    str(ROOT / "apps/goosetown-cli/src"),
    str(ROOT / "scripts/runtime-join-fixture"),
]
os.environ.update(
    AWS_ACCESS_KEY_ID="testing",
    AWS_SECRET_ACCESS_KEY="testing",
    AWS_SESSION_TOKEN="testing",
    AWS_DEFAULT_REGION="us-east-1",
    AWS_EC2_METADATA_DISABLED="true",
    ENVIRONMENT="dev",
    PIXELLAB_API_KEY="",
    SPRITE_S3_BUCKET="",
    SPRITE_CDN_URL="",
)
import boto3
from fastapi import FastAPI
from moto import mock_aws
import uvicorn
from tests.conftest import _create_tables
from core import dynamo
from core.auth import AuthContext, get_current_user
from core.config import settings
from core.services import sqs_drainer
from core.services.town_service import TownService
from routers.town import router
from goosetown.core import cmd_join
from goosetown.core import _cmd_join as original_join


def verified_join(args, member):
    assert member is not None
    result = member._call(
        "memory.remember", subject="synthetic town canary", fact="must stay unshared",
        kind="fact", scope="team",
    )
    assert result == {"stored": False, "refused": "team_off"}, result
    return original_join(args, member)

with mock_aws(), tempfile.TemporaryDirectory(prefix="gt-join-") as workspace:
    dynamo._client = None
    _create_tables(boto3.resource("dynamodb", region_name="us-east-1"))
    settings.RUNTIME_BINDING_ENABLED = True
    settings.RUNTIME_SESSION_ENFORCEMENT_ENABLED = True
    settings.PIXELLAB_API_KEY = ""
    sqs_drainer.create_agent_queue = lambda _: None
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/town")
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="user_fixture"
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(100)
    api = f"http://127.0.0.1:{sock.getsockname()[1]}/api/v1"
    settings.RUNTIME_BINDING_ORIGIN = api.removesuffix("/api/v1")
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, daemon=True
    )
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("backend did not start")
        req = urllib.request.Request(
            api + "/town/instance",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            onboarding = json.load(response)
        join_token = onboarding["join_token"]
        ns = argparse.Namespace(
            token=join_token,
            api_url=api,
            name="fixtureagent",
            personality="",
            appearance="fixture",
            traits="",
            harness="none",
            provider=False,
        )
        with (
            patch.dict(
                os.environ,
                {
                    "AGENT_DIR": workspace,
                    "TOWN_API_URL": api,
                    "PATH": str(ROOT / "../cli-binding-proof/target/debug")
                    + os.pathsep
                    + os.environ.get("PATH", ""),
                    "LIGHTBULB_CONNECT_DEBUG_NO_DEFAULT_RELAYS": "1",
                    "LIGHTBULB_CONNECT_DEBUG_NO_TELEMETRY": "1",
                    "LIGHTBULB_API": "http://127.0.0.1:9",
                },
            ),
            patch("goosetown.core._ensure_websockets"),
            patch("goosetown.core._require_websockets"),
            patch("goosetown.core._is_daemon_alive", return_value=False),
            patch(
                "goosetown.core._start_daemon", return_value={"connected": True}
            ) as daemon,
            patch("goosetown.core._out"),
            patch("goosetown.core._cmd_join", side_effect=verified_join),
        ):
            cmd_join(ns)
            assert daemon.call_count == 1
            ns.token = onboarding["town_token"]
            cmd_join(ns)
            assert daemon.call_count == 2
            issued = daemon.call_args.args[2]
            assert issued["runtime_session_token"].startswith("gtrs_")
            assert issued["managed"] is True
            import asyncio
            from goosetown.daemon import TownDaemon
            import websockets
            from routers.runtime_session import verify, validate_live

            async def exercise():
                effects = []
                sockets = []
                class FakeSocket:
                    def __init__(self):
                        self.queue = asyncio.Queue()
                        self.claims = None
                    async def send(self, raw):
                        msg = json.loads(raw)
                        if msg["type"] == "town_agent_connect":
                            claims = verify(msg.get("runtime_session_token"))
                            if not claims or claims["actor_id"] != issued["actor_id"]:
                                await self.queue.put(json.dumps({"event":"error"}))
                                return
                            self.claims = claims
                            await self.queue.put(json.dumps({"event":"connected", "agent":{"id":claims["actor_id"]}}))
                        elif msg["type"] == "town_agent_act":
                            if validate_live(self.claims):
                                effects.append(msg["action"])
                                result = {"event":"action_result", "request_id":msg["request_id"], "status":"accepted"}
                            else:
                                result = {"event":"action_result", "request_id":msg["request_id"], "status":"rejected", "error":"invalid_runtime_session"}
                            await self.queue.put(json.dumps(result))
                    async def recv(self):
                        item = await self.queue.get()
                        if item is None: raise ConnectionError("fixture closed")
                        return item
                    async def close(self):
                        await self.queue.put(None)
                async def connect(*args, **kwargs):
                    obj = FakeSocket(); sockets.append(obj); return obj
                state = Path(workspace) / "runtime-test"; state.mkdir()
                d = TownDaemon("ws://127.0.0.1:1", onboarding["town_token"], "fixtureagent", state, Path(workspace))
                d._runtime_session = issued
                d._on_connected = lambda data: setattr(d, "_runtime_authenticated", data["agent"]["id"] == issued["actor_id"])
                with patch.object(websockets, "connect", connect):
                    await d.connect_ws()
                    initial = json.loads(await sockets[0].recv()); d._on_connected(initial)
                    listener = asyncio.create_task(d.listen_ws())
                    ipc = asyncio.create_task(d.run_socket_server())
                    try:
                        for _ in range(100):
                            if d._sock_path.exists(): break
                            await asyncio.sleep(.01)
                        async def act():
                            reader, writer = await asyncio.open_unix_connection(str(d._sock_path))
                            writer.write(json.dumps({"action":"act", "payload":{"action":"idle"}}).encode())
                            await writer.drain(); writer.write_eof()
                            result = json.loads(await reader.read()); writer.close(); await writer.wait_closed()
                            return result
                        d._runtime_session["expires_at"] = int(time.time()) + 1
                        assert "error" not in await asyncio.wait_for(act(), 20)
                        assert len(sockets) == 2 and effects == ["idle"]
                        dynamo.agents_table().update_item(
                            Key={"id": issued["actor_id"]},
                            UpdateExpression="SET bindingRevision = :r REMOVE runtimeBinding",
                            ExpressionAttributeValues={":r": 2},
                        )
                        rejected = await act(); await asyncio.sleep(.05)
                        assert rejected["status"] == "rejected"
                        assert effects == ["idle"]
                    finally:
                        d.running = False
                        listener.cancel()
                        await asyncio.gather(listener, return_exceptions=True)
                        await asyncio.wait_for(ipc, 2)
                print("PASS enforced session: real signer renewal and real daemon IPC; stub WS validates backend before action and denies revoked binding")
            asyncio.run(exercise())
        actor = TownService().get_agent_by_name("user_fixture", "fixtureagent")
        assert actor["bindingRevision"] == 2
        assert "runtimeBinding" not in actor
        assert "runtimeJoin" not in actor and "bindingChallenge" not in actor
        cfg = (Path(workspace) / "GOOSETOWN.md").read_text()
        assert join_token not in cfg
        assert issued["runtime_session_token"] not in cfg
        for candidate in (Path(workspace) / "runtime-test").rglob("*"):
            if candidate.is_file():
                assert issued["runtime_session_token"].encode() not in candidate.read_bytes()
        assert (Path(workspace) / ".goosetown" / "member" / "state").exists()
        print(
            "PASS real CLI -> actual Moto routes -> origin-bound Rust signer -> atomic binding/reconnect; "
            "team memory write refused; daemon mocked"
        )
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        dynamo._client = None
