import argparse
import base64
import json
import os
import socketserver
import threading
import tempfile
import shutil
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest

from goosetown.core import cmd_join

KEY = "a" * 52
SESSION = KEY[:12] + "/agent"
GRANT = "g" * 43
COMPOSITE = "gtj_" + base64.urlsafe_b64encode(
    json.dumps(
        {"town_token": "synthetic-town-token", "enrollment_token": GRANT}
    ).encode()
).decode().rstrip("=")


def test_api_origin_matches_member_canonicalization():
    from goosetown.member_binding import MemberError, api_origin

    assert api_origin("https://TOWN.example:443/api/v1") == "https://town.example"
    assert api_origin("http://[0:0:0:0:0:0:0:1]:8000/api/v1") == "http://[::1]:8000"
    with pytest.raises(MemberError):
        api_origin("https://" + ".".join(["a" * 60] * 8) + "/api/v1")


@contextmanager
def local_services(tmp_path, scenario="approved", existing=False):
    events = []

    class Member(socketserver.StreamRequestHandler):
        def handle(self):
            req = json.loads(self.rfile.readline())
            assert req["token"] == "local-member-secret"
            events.append(("member", req["op"]))
            if scenario == "unsupported" and req["op"] == "goosetown.binding.info":
                self.wfile.write(
                    (
                        json.dumps(
                            {"id": req["id"], "ok": False, "error": "unknown_op"}
                        )
                        + "\n"
                    ).encode()
                )
                return
            if req["op"] == "identity.get":
                result = {"member_key": KEY}
            elif req["op"] == "goosetown.binding.info":
                result = {"version": 1, "audience": "goosetown-runtime-binding-v1:" + api.rsplit("/api/v1", 1)[0]}
            else:
                assert req["signing_bytes_b64url"] == "approved-by-server"
                result = {"signature": "synthetic-signature"}
            self.wfile.write(
                (
                    json.dumps({"id": req["id"], "ok": True, "result": result}) + "\n"
                ).encode()
            )

    class Http(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            assert self.headers["Authorization"] == "Bearer synthetic-town-token"
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            events.append(("http", self.path, data))
            code = 200
            if self.path.endswith("/agent/register"):
                if existing:
                    code, result = 400, {"detail": "Agent 'test' already registered"}
                else:
                    result = {
                        "agent_id": "actor-1",
                        "agent_name": "test",
                        "api_url": api,
                        "ws_url": "ws://127.0.0.1:1/ws",
                    }
            elif self.path.endswith("/town/runtime-join"):
                assert data == {
                    "agent_name": "test",
                    "member_key": KEY,
                    "session_id": SESSION,
                    "enrollment_token": GRANT,
                }
                if scenario == "conflict":
                    code, result = 409, {"detail": "conflict"}
                elif scenario == "ready":
                    result = {"state": "ready", "actor_id": "actor-1", "revision": 1}
                else:
                    result = {
                        "state": "approved",
                        "actor_id": "actor-1",
                        "request_id": "request-1",
                        "poll_secret": "synthetic-poll-secret",
                        "challenge": {
                            "challenge_id": "challenge-1",
                            "signing_bytes_b64url": "approved-by-server",
                        },
                    }
            elif self.path.endswith("/complete"):
                assert data == {
                    "poll_secret": "synthetic-poll-secret",
                    "challenge_id": "challenge-1",
                    "signature": "synthetic-signature",
                }
                result = {"state": "ready", "actor_id": "actor-1", "revision": 1}
            else:
                code, result = 404, {}
            body = json.dumps(result).encode()
            self.send_response(code)
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Http)
    api = f"http://127.0.0.1:{server.server_port}/api/v1"
    sockdir = tempfile.mkdtemp(prefix="gt-", dir="/tmp")
    sockpath = sockdir + "/sock"
    member = socketserver.ThreadingUnixStreamServer(sockpath, Member)
    for service in (server, member):
        threading.Thread(target=service.serve_forever, daemon=True).start()
    read_fd, write_fd = os.pipe()
    os.write(
        write_fd,
        json.dumps(
            {"socket": sockpath, "token": "local-member-secret", "session_id": SESSION}
        ).encode(),
    )
    os.close(write_fd)
    try:
        yield api, read_fd, events
    finally:
        for service in (server, member):
            service.shutdown()
            service.server_close()
        shutil.rmtree(sockdir)
        try:
            os.close(read_fd)
        except OSError:
            pass


def run_join(tmp_path, api, fd):
    ns = argparse.Namespace(
        token=COMPOSITE,
        api_url=api,
        name="test",
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
                "AGENT_DIR": str(tmp_path),
                "TOWN_API_URL": api,
                "GOOSETOWN_MEMBER_FD": str(fd),
            },
        ),
        patch("goosetown.core._ensure_websockets"),
        patch("goosetown.core._require_websockets"),
        patch("goosetown.core._is_daemon_alive", return_value=False),
        patch(
            "goosetown.core._start_daemon", return_value={"connected": True}
        ) as daemon,
        patch("goosetown.core._out") as out,
    ):
        cmd_join(ns)
        assert "GOOSETOWN_MEMBER_FD" not in os.environ
        return daemon.call_count, out.call_count


@pytest.mark.parametrize("existing", [False, True])
def test_real_join_approved_before_daemon(tmp_path, existing):
    with local_services(tmp_path, existing=existing) as (api, fd, events):
        assert run_join(tmp_path, api, fd) == (1, 1)
    assert [event[1] for event in events if event[0] == "member"] == [
        "goosetown.binding.info",
        "identity.get",
        "goosetown.binding.prove",
    ]
    assert events[-1][1].endswith("/complete")
    cfg = (tmp_path / "GOOSETOWN.md").read_text()
    assert "local-member-secret" not in cfg and "synthetic-poll-secret" not in cfg
    assert GRANT not in cfg and COMPOSITE not in cfg and "synthetic-town-token" in cfg


def test_existing_exact_binding_reconnect_skips_approval(tmp_path):
    with local_services(tmp_path, scenario="ready", existing=True) as (api, fd, events):
        assert run_join(tmp_path, api, fd) == (1, 1)
    assert [e[1] for e in events if e[0] == "member"] == [
        "goosetown.binding.info",
        "identity.get",
    ]


def test_other_binding_conflict_never_starts_daemon(tmp_path, capsys):
    with local_services(tmp_path, scenario="conflict") as (api, fd, events):
        with pytest.raises(SystemExit):
            run_join(tmp_path, api, fd)
    assert not (tmp_path / "GOOSETOWN.md").exists()
    assert "local-member-secret" not in capsys.readouterr().out


def test_composite_without_member_fails_before_registration(tmp_path):
    ns = argparse.Namespace(
        token=COMPOSITE,
        api_url="http://127.0.0.1:9/api/v1",
        name="test",
        personality="",
        appearance="fixture",
        traits="",
        harness="none",
        provider=False,
    )
    with (
        patch.dict(os.environ, {"AGENT_DIR": str(tmp_path)}, clear=True),
        patch("goosetown.core._ensure_websockets"),
        patch("goosetown.core._require_websockets"),
        patch("urllib.request.urlopen") as network,
        patch("shutil.which", return_value=None),
        patch("pathlib.Path.home", return_value=tmp_path),
    ):
        with pytest.raises(SystemExit):
            cmd_join(ns)
        network.assert_not_called()


def test_invalid_composite_rejected_without_revealing_it():
    from goosetown.runtime_join import EnrollmentError, unpack_join_token

    with pytest.raises(EnrollmentError) as error:
        unpack_join_token("gtj_invalid-private-value")
    assert "invalid-private-value" not in str(error.value)


def test_existing_agent_refreshes_stale_token_before_daemon(tmp_path):
    cfg = tmp_path / "GOOSETOWN.md"
    cfg.write_text(
        "# GooseTown Configuration\ntoken: old-token\nagent: test\nws_url: ws://127.0.0.1:1/ws\n"
    )
    with local_services(tmp_path, scenario="ready", existing=True) as (api, fd, events):
        assert run_join(tmp_path, api, fd) == (1, 1)
    assert "old-token" not in cfg.read_text()
    assert "token: synthetic-town-token" in cfg.read_text()
    assert GRANT not in cfg.read_text()


def test_different_token_with_live_daemon_refuses_before_enrollment(tmp_path):
    cfg = tmp_path / "GOOSETOWN.md"
    cfg.write_text("# GooseTown Configuration\ntoken: old-token\nagent: test\n")
    with local_services(tmp_path, scenario="ready", existing=True) as (api, fd, events):
        with patch("goosetown.core._is_daemon_alive", return_value=True):
            # run_join sets its own daemon patch, so exercise cmd_join directly here.
            ns = argparse.Namespace(
                token=COMPOSITE,
                api_url=api,
                name="test",
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
                        "AGENT_DIR": str(tmp_path),
                        "TOWN_API_URL": api,
                        "GOOSETOWN_MEMBER_FD": str(fd),
                    },
                ),
                patch("goosetown.core._ensure_websockets"),
                patch("goosetown.core._require_websockets"),
                patch("goosetown.core._start_daemon") as daemon,
            ):
                with pytest.raises(SystemExit):
                    cmd_join(ns)
                daemon.assert_not_called()
    assert cfg.read_text().find("old-token") >= 0
    assert [e[1] for e in events if e[0] == "member"] == ["goosetown.binding.info"]


def test_old_member_fails_before_registration(tmp_path, capsys):
    with local_services(tmp_path, scenario="unsupported") as (api, fd, events):
        with pytest.raises(SystemExit):
            run_join(tmp_path, api, fd)
    assert not any(event[0] == "http" for event in events)
    assert "update required" in capsys.readouterr().out


@pytest.mark.parametrize("part", [".goosetown", "member", "state"])
def test_managed_member_rejects_symlink_before_start(tmp_path, part):
    from goosetown.member_binding import ManagedMember, MemberError

    target = tmp_path / "unrelated"
    target.mkdir()
    root = tmp_path / "workspace"
    root.mkdir()
    if part == ".goosetown":
        (root / ".goosetown").symlink_to(target, target_is_directory=True)
    elif part == "member":
        (root / ".goosetown").mkdir()
        (root / ".goosetown" / "member").symlink_to(target, target_is_directory=True)
    else:
        (root / ".goosetown" / "member").mkdir(parents=True)
        (root / ".goosetown" / "member" / "state").symlink_to(
            target, target_is_directory=True
        )
    with (
        patch("shutil.which", return_value="/synthetic/lightbulb"),
        patch("subprocess.Popen") as spawn,
    ):
        with pytest.raises(MemberError, match="symlink"):
            with ManagedMember(root):
                pass
        spawn.assert_not_called()


def test_managed_member_lock_and_failure_cleanup(tmp_path):
    import fcntl
    from goosetown.member_binding import ManagedMember, MemberError

    root = tmp_path / "workspace"
    home = root / ".goosetown" / "member"
    home.mkdir(parents=True)
    lock = os.open(home / "enrollment.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (
            patch("shutil.which", return_value="/synthetic/lightbulb"),
            patch("subprocess.Popen") as spawn,
        ):
            with pytest.raises(MemberError, match="already running"):
                with ManagedMember(root):
                    pass
            spawn.assert_not_called()
        fcntl.flock(lock, fcntl.LOCK_UN)
        with (
            patch("shutil.which", return_value="/synthetic/lightbulb"),
            patch("subprocess.Popen", side_effect=OSError("fail")),
        ):
            with pytest.raises(MemberError, match="failed to start"):
                with ManagedMember(root):
                    pass
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert os.stat(home).st_mode & 0o777 == 0o700
        assert os.stat(home / "enrollment.lock").st_mode & 0o777 == 0o600
    finally:
        os.close(lock)
