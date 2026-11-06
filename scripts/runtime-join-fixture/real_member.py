"""Disposable real Lightbulb member for the loopback enrollment proof."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from goosetown.member_binding import Member

BINARY = Path(
    "/Users/prasiddhaparthsarthy/Lightbulb/goosetown-plan/cli-binding-proof/target/debug/lightbulb"
)


class RealMember:
    def __enter__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="gt-member-", dir="/tmp"))
        self.sock = self.dir / "sock"
        self.token = "fixture-only-member-token"
        sessions = self.dir / "sessions.json"
        sessions.write_text(
            json.dumps(
                {
                    "workspace": "fixture",
                    "sessions": [{"name": "synthetic", "agent": "generic"}],
                }
            )
        )
        env = os.environ.copy()
        env.update(
            HOME=str(self.dir),
            LIGHTBULB_CONNECT_DEBUG_NO_DEFAULT_RELAYS="1",
            LIGHTBULB_CONNECT_DEBUG_NO_TELEMETRY="1",
            LIGHTBULB_API="http://127.0.0.1:9",
        )
        env.pop("LIGHTBULB_CONNECT_PEERS", None)
        self.process = subprocess.Popen(
            [
                str(BINARY),
                "member",
                "--home",
                str(self.dir / "home"),
                "--snapshot",
                str(self.dir / "snapshot.db"),
                "--socket",
                str(self.sock),
                "--name",
                "fixture",
                "--sessions",
                str(sessions),
                "--goosetown-origin",
                "https://api.goosetown.thelightbulb.company",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            text=True,
        )
        self.process.stdin.write(self.token + "\n")
        self.process.stdin.close()
        for _ in range(200):
            if self.sock.exists():
                break
            if self.process.poll() is not None:
                raise RuntimeError("member exited before ready")
            time.sleep(0.05)
        else:
            raise RuntimeError("member socket not ready")
        proxy = Member(str(self.sock), self.token, "placeholder")
        self.key = proxy._call("identity.get")["member_key"]
        self.session = self.key[:12] + "/synthetic"
        return self

    def fd(self):
        read, write = os.pipe()
        os.write(
            write,
            json.dumps(
                {
                    "socket": str(self.sock),
                    "token": self.token,
                    "session_id": self.session,
                }
            ).encode(),
        )
        os.close(write)
        return read

    def __exit__(self, *_):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        shutil.rmtree(self.dir)
