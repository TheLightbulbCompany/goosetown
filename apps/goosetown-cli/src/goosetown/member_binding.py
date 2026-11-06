"""Trusted local client for the Lightbulb member binding proof operation.

Member credentials are supplied by the trusted parent over an inherited FD,
not by an agent prompt, command-line argument or GooseTown server.
"""

import json
import os
import socket


def api_origin(api_url):
    """Derive an expected origin from the selected endpoint, never a response."""
    import ipaddress
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(api_url)
        host = parsed.hostname
        if not host or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("invalid endpoint")
        host = host.encode("idna").decode("ascii").lower()
        if ":" in host:
            host = ipaddress.IPv6Address(host).compressed
        loopback = host in ("localhost", "127.0.0.1", "::1")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise ValueError("insecure endpoint")
        port = parsed.port
        suffix = "" if port is None or (parsed.scheme, port) in (("http", 80), ("https", 443)) else f":{port}"
        canonical = f'{parsed.scheme}://{"[" + host + "]" if ":" in host else host}{suffix}'
        if len(canonical) > 480:
            raise ValueError("origin too long")
        return canonical
    except (ValueError, TypeError, UnicodeError) as exc:
        raise MemberError("invalid GooseTown API origin") from exc


class MemberError(Exception):
    pass


class Member:
    def __init__(self, path: str, token: str, session: str):
        if not path or not token or not session:
            raise MemberError("incomplete local member configuration")
        self._path = path
        self._token = token
        self.session = session

    @classmethod
    def from_fd(cls, fd: int):
        try:
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                data = stream.read(16385)
            if len(data) > 16384:
                raise ValueError("member configuration too large")
            value = json.loads(data)
            return cls(value["socket"], value["token"], value["session_id"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise MemberError("could not read local member configuration") from exc

    def _call(self, op: str, **body):
        request = {"id": 1, "token": self._token, "op": op, **body}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(10)
                conn.connect(self._path)
                conn.sendall(
                    json.dumps(request, separators=(",", ":")).encode() + b"\n"
                )
                with conn.makefile("rb") as stream:
                    line = stream.readline(65537)
            if len(line) > 65536 or not line.endswith(b"\n"):
                raise ValueError("invalid member response")
            response = json.loads(line)
            if (
                response.get("id") != 1
                or response.get("ok") is not True
                or not isinstance(response.get("result"), dict)
            ):
                raise ValueError("member rejected request")
            return response["result"]
        except (OSError, ValueError, TypeError) as exc:
            raise MemberError("local member request failed") from exc

    def check_support(self, api_url):
        try:
            result = self._call("goosetown.binding.info")
        except MemberError as exc:
            raise MemberError(
                "Lightbulb CLI update required: GooseTown signing is unavailable"
            ) from exc
        if result != {"version": 1, "audience": "goosetown-runtime-binding-v1:" + api_origin(api_url)}:
            raise MemberError(
                "Lightbulb CLI update required: incompatible GooseTown signing"
            )

    def identity(self):
        result = self._call("identity.get")
        key = result.get("member_key")
        if (
            not isinstance(key, str)
            or len(key) != 52
            or not self.session.startswith(key[:12] + "/")
        ):
            raise MemberError("local member identity does not match session")
        return key

    def prove(self, signing_bytes_b64url: str):
        try:
            result = self._call(
                "goosetown.binding.prove", signing_bytes_b64url=signing_bytes_b64url
            )
        except MemberError as exc:
            raise MemberError(
                "local member could not sign; a Lightbulb CLI with GooseTown signing support is required"
            ) from exc
        signature = result.get("signature")
        if not isinstance(signature, str) or not signature:
            raise MemberError("local member did not return a signature")
        return signature


class ManagedMember:
    """One owned enrollment helper; the persistent key stays in a town-only home."""

    def __init__(self, workspace, api_url="https://api.goosetown.thelightbulb.company/api/v1"):
        from pathlib import Path

        self.workspace = Path(workspace)
        self.origin = api_origin(api_url)
        self.home = self.workspace / ".goosetown" / "member"
        self.process = None
        self.sockdir = None
        self.lock_fd = None

    def __enter__(self):
        import secrets
        import shutil
        import subprocess
        import tempfile
        import time

        binary = shutil.which("lightbulb")
        if not binary:
            raise MemberError("Lightbulb CLI is required for automatic town enrollment")
        try:
            import fcntl

            self.workspace.mkdir(parents=True, exist_ok=True)
            for directory in (self.workspace / ".goosetown", self.home):
                if directory.is_symlink():
                    raise MemberError("town member directory cannot be a symlink")
                directory.mkdir(exist_ok=True, mode=0o700)
                if directory.is_symlink():
                    raise MemberError("town member directory cannot be a symlink")
                os.chmod(directory, 0o700)
            if (self.home / "state").is_symlink():
                raise MemberError("town member state cannot be a symlink")
            self.lock_fd = os.open(
                self.home / "enrollment.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
            )
            os.fchmod(self.lock_fd, 0o600)
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise MemberError(
                    "another town enrollment is already running in this workspace"
                ) from exc
            state = self.home / "state"
            state.mkdir(exist_ok=True, mode=0o700)
            os.chmod(state, 0o700)
            # This helper owns only the town home. Disable new team-memory
            # writes before boot; this is not a general sync/output firewall.
            config_fd = os.open(state / "config.toml", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(config_fd, "r+", encoding="utf-8") as config:
                import re

                if os.fstat(config.fileno()).st_nlink != 1:
                    raise MemberError("town member config cannot have multiple links")
                text = config.read(65537)
                if len(text) > 65536:
                    raise MemberError("town member config is too large")
                lines, replaced, section = [], False, False
                for line in text.splitlines():
                    if line.lstrip().startswith("["):
                        section = True
                    if not section and re.match(r"^\s*memory_team\s*=", line):
                        lines.append("memory_team = false")
                        replaced = True
                    else:
                        lines.append(line)
                if not replaced:
                    lines.insert(0, "memory_team = false")
                config.seek(0)
                config.write("\n".join(lines) + "\n")
                config.truncate()
                os.fchmod(config.fileno(), 0o600)
            sessions = self.home / "town-sessions.json"
            if sessions.is_symlink() or (self.home / "snapshot.db").is_symlink():
                raise MemberError("town member files cannot be symlinks")
            sessions.write_text(
                json.dumps(
                    {
                        "workspace": "GooseTown",
                        "sessions": [{"name": "goosetown", "agent": "generic"}],
                    }
                )
            )
            os.chmod(sessions, 0o600)
            token = secrets.token_urlsafe(32)
            env = {
                "HOME": str(self.home),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            }
            for name in (
                "LIGHTBULB_CONNECT_DEBUG_NO_DEFAULT_RELAYS",
                "LIGHTBULB_CONNECT_DEBUG_NO_TELEMETRY",
                "LIGHTBULB_API",
            ):
                if name in os.environ:
                    env[name] = os.environ[name]
            self.sockdir = tempfile.mkdtemp(prefix="goosetown-member-", dir="/tmp")
            socket_path = os.path.join(self.sockdir, "sock")
            self.process = subprocess.Popen(
                [
                    binary,
                    "member",
                    "--home",
                    str(self.home / "state"),
                    "--snapshot",
                    str(self.home / "snapshot.db"),
                    "--socket",
                    socket_path,
                    "--name",
                    "GooseTown",
                    "--sessions",
                    str(sessions),
                    "--goosetown-origin",
                    self.origin,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                env=env,
            )
            self.process.stdin.write(token + "\n")
            self.process.stdin.close()
            for _ in range(200):
                if os.path.exists(socket_path):
                    proxy = Member(socket_path, token, "placeholder")
                    identity = proxy._call("identity.get")
                    key = identity["member_key"]
                    return Member(socket_path, token, key[:12] + "/goosetown")
                if self.process.poll() is not None:
                    raise MemberError("local Lightbulb member failed to start")
                time.sleep(0.05)
            raise MemberError("local Lightbulb member did not become ready")
        except MemberError:
            self.__exit__(None, None, None)
            raise
        except (OSError, KeyError, TypeError, ValueError) as exc:
            self.__exit__(None, None, None)
            raise MemberError("local Lightbulb member failed to start") from exc

    def __exit__(self, *_):
        import shutil
        import subprocess

        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.sockdir:
            shutil.rmtree(self.sockdir, ignore_errors=True)
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
