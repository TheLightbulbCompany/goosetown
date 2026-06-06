#!/usr/bin/env python3
"""GooseTown end-to-end test harness.

Simulates N agents exactly like OpenClaw would — by invoking the
`goosetown` CLI on a periodic heartbeat. Each agent has its own
workspace directory (`/tmp/test-harness/<name>/`) and its own daemon.

This tests the REAL path: CLI → Unix socket → daemon → WebSocket → backend.
No shortcuts. If an OpenClaw agent can run this, so can this harness.

Usage:
    cd /Users/prasiddhaparthsarthy/Desktop/goosetown
    python3 scripts/test_harness.py

Runs until Ctrl+C.
"""

import asyncio
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TOKEN = "eyJ1aWQiOiJ1c2VyXzNDR2ZRamJuOFAzbjZaekd4ejF0eGVxRUZ6QyIsImlpZCI6Ijk0ZGRkMGE1LTQyZDctNDcwOS04NjNlLThkZTI5ODcwZTZmZCJ9.AeTMBKSChpbuFtlPRyQnkGLI5OmEigq-A1Af4yVc1wU"
API_URL = "https://api-dev.goosetown.isol8.co/api/v1"
WS_URL = "wss://ws-dev.goosetown.isol8.co"

REPO_ROOT = Path("/Users/prasiddhaparthsarthy/Desktop/goosetown")
# Canonical PyPI package (apps/goosetown-cli). Invoked via the module entry
# point so the harness doesn't depend on `goosetown` being on PATH — just on
# it being importable by this interpreter (`pip install goosetown`).
CLI_MODULE = "goosetown"
WORKSPACE_ROOT = Path("/tmp/test-harness")

# OpenClaw heartbeat is 15s. We add a small jitter so agents don't synchronize.
HEARTBEAT_SEC = 15.0

COLORS = {
    "rachel":   "\033[94m",
    "ross":     "\033[92m",
    "monica":   "\033[93m",
    "chandler": "\033[95m",
    "joey":     "\033[91m",
    "phoebe":   "\033[96m",
    "test":     "\033[1;97m",
    "reset":    "\033[0m",
}

TOWN_LOCATIONS = ["plaza", "cafe", "library", "activity_center", "bank"]
ACTIVITIES_BY_LOCATION = {
    "plaza": ["people_watch", "perform", "rest"],
    "cafe": ["order_coffee", "sit_and_chat", "work_on_laptop"],
    "library": ["read", "study", "write_journal"],
    "activity_center": ["exercise", "play_game", "garden"],
    "bank": ["check_balance", "browse_listings"],
}

FRIENDS = [
    {
        "name": "rachel",
        "display": "Rachel Green",
        "personality": "Fashionable career woman, social, loves coffee",
        "appearance": "A stylish young woman with long brown hair, blue denim jacket over a floral dress, black boots, holding a coffee cup",
        "traits": "extrovert,social,energetic",
    },
    {
        "name": "ross",
        "display": "Ross Geller",
        "personality": "Paleontologist, intellectual, awkward but romantic",
        "appearance": "A tall man with short dark curly hair, brown leather jacket over button-up shirt, khaki pants",
        "traits": "introvert,studious,creative",
    },
    {
        "name": "monica",
        "display": "Monica Geller",
        "personality": "Competitive chef, organized, the mom of the group",
        "appearance": "A woman with straight black hair pulled back, chef apron over purple sweater, dark jeans",
        "traits": "extrovert,energetic,practical",
    },
    {
        "name": "chandler",
        "display": "Chandler Bing",
        "personality": "Sarcastic, witty, self-deprecating humor",
        "appearance": "A man with short brown hair, oversized sweater vest over dress shirt, loose khakis",
        "traits": "introvert,creative,social",
    },
    {
        "name": "joey",
        "display": "Joey Tribbiani",
        "personality": "Lovable actor, food-obsessed, loyal friend",
        "appearance": "A handsome man with slicked-back dark hair, leather jacket over white t-shirt, dark jeans, boots",
        "traits": "extrovert,adventurous,energetic",
    },
    {
        "name": "phoebe",
        "display": "Phoebe Buffay",
        "personality": "Free-spirited, quirky, surprisingly wise",
        "appearance": "A woman with long wavy blonde hair, bohemian patchwork skirt, knit shawl, sandals, beaded necklaces",
        "traits": "extrovert,creative,calm",
    },
]

OPENERS = {
    "rachel": ["Oh my God, hi!", "Hey sweetie!", "I love your outfit!"],
    "ross": ["Hi. So, dinosaurs...", "Hello. Fascinating day.", "Hey there."],
    "monica": ["Hey! Everything okay?", "Coffee?", "You look tense."],
    "chandler": ["Could this BE more awkward?", "Hi. I'm Chandler.", "Oh look, a person."],
    "joey": ["How YOU doin'?", "You look hungry.", "Food, anyone?"],
    "phoebe": ["Your aura is blue!", "You're a Taurus.", "La la la smelly cat."],
}

REPLIES = {
    "rachel": ["Oh totally!", "I know, right?", "That is SO true."],
    "ross": ["Actually, technically...", "We were on a break!", "Interesting."],
    "monica": ["You should organize that.", "I can fix this.", "Let me help."],
    "chandler": ["Could this BE more typical?", "That's... something.", "Sure."],
    "joey": ["How you doin'?", "I'm hungry.", "Pizza?"],
    "phoebe": ["Past life stuff.", "The universe speaks.", "Smelly cat..."],
}

START_TIME = time.time()


def log(tag: str, msg: str) -> None:
    elapsed = time.time() - START_TIME
    color = COLORS.get(tag, COLORS["test"])
    reset = COLORS["reset"]
    print(f"{color}[{elapsed:6.1f}s {tag:>8}]{reset} {msg}", flush=True)


# ---------------------------------------------------------------------------
# CLI wrapper
# ---------------------------------------------------------------------------

class AgentCLI:
    """Runs `goosetown` CLI commands for one agent."""

    def __init__(self, info: dict):
        self.name: str = info["name"]
        self.display: str = info["display"]
        self.personality: str = info["personality"]
        self.appearance: str = info["appearance"]
        self.traits: str = info["traits"]
        # AGENT_DIR: where the daemon writes TOWN_STATUS.md, TOWN_EVENTS.md,
        # GOOSETOWN.md, TOWN_LIFE.md. Each agent gets their own dir.
        self.agent_dir: Path = WORKSPACE_ROOT / self.name
        self.agent_dir.mkdir(parents=True, exist_ok=True)
        self.joined: bool = False
        self.last_state: dict = {}
        self.nearby: list = []
        self.active_conv: dict | None = None

    def log(self, msg: str) -> None:
        log(self.name, msg)

    def _env(self) -> dict:
        env = os.environ.copy()
        env.update({
            "AGENT_NAME": self.name,
            "DISPLAY_NAME": self.display,
            "PERSONALITY": self.personality,
            "APPEARANCE": self.appearance,
            "TRAITS": self.traits,
            "AGENT_DIR": str(self.agent_dir),
        })
        return env

    async def _run_cli(self, *args: str, timeout: float = 30.0) -> tuple[int, str, str]:
        """Run `goosetown <args>` asynchronously. Returns (returncode, stdout, stderr)."""
        cmd = [sys.executable, "-m", CLI_MODULE, *args]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                env=self._env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.agent_dir),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")
        except asyncio.TimeoutError:
            try:
                proc.kill()  # type: ignore
            except Exception:
                pass
            return -1, "", "TIMEOUT"

    async def join(self) -> bool:
        self.log("→ join (registers + starts daemon)")
        rc, out, err = await self._run_cli(
            "join", TOKEN,
            "--api-url", API_URL,
            timeout=60.0,
        )
        if rc == 0:
            self.joined = True
            self.log(f"✓ joined")
            return True
        self.log(f"✗ join failed rc={rc} stderr={err[:200]!r}")
        return False

    def _state_dir(self) -> Path:
        """The daemon writes state.json and TOWN_STATUS.md here."""
        # Daemon state dir: /tmp/goosetown/<name>/
        return Path("/tmp/goosetown") / self.name

    def _read_town_status(self) -> str:
        """Read TOWN_STATUS.md from the workspace — this is what OpenClaw reads.

        The daemon writes it on every world_update where `think=true`. If
        it contains 'Your turn', the agent should act this heartbeat.
        """
        status_file = self.agent_dir / "TOWN_STATUS.md"
        try:
            return status_file.read_text()
        except FileNotFoundError:
            return ""

    def _read_state_json(self) -> dict:
        """Read the daemon's structured state (for our rule-based brain).

        Real OpenClaw agents parse TOWN_STATUS.md markdown — but we're a
        simple rule-based brain, so JSON is easier. The file is written
        by the daemon on every event.
        """
        state_file = self._state_dir() / "state.json"
        try:
            return json.loads(state_file.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _it_is_my_turn(self, town_status: str) -> bool:
        """Does TOWN_STATUS.md contain a 'Your turn' prompt?

        The daemon only writes this section when world_update arrives with
        think=true. Between think prompts, TOWN_STATUS.md still exists but
        doesn't have the action menu — the agent shouldn't act.
        """
        return "Your turn" in town_status or "Active Conversation" in town_status

    async def act(self, action: str, *args: str) -> bool:
        self.log(f"→ act {action} {' '.join(str(a) for a in args)}")
        rc, out, err = await self._run_cli("act", action, *args, timeout=130.0)
        if rc == 0:
            try:
                result = json.loads(out)
                status_val = result.get("status", "")
                if status_val == "arrived":
                    self.log(f"✓ arrived at {result.get('location', '?')}")
                elif status_val in ("ok", "sleeping"):
                    pass
                else:
                    self.log(f"  act result: {status_val} {result}")
            except json.JSONDecodeError:
                pass
            return True
        self.log(f"✗ act {action} failed rc={rc} out={out[:200]!r}")
        return False

    def kill_daemon(self) -> None:
        pid_file = self.agent_dir / "daemon.pid"
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, signal.SIGTERM)
                self.log(f"killed daemon pid {pid}")
            except (ValueError, ProcessLookupError):
                pass

    # ---------- Brain ----------

    async def think(self) -> None:
        """One heartbeat tick — exactly like OpenClaw would do it.

        Reads TOWN_STATUS.md. If the daemon wrote a 'Your turn' prompt
        (meaning world_update arrived with think=true), decide and act.
        Otherwise skip — the agent isn't due.
        """
        town_status = self._read_town_status()
        if not town_status:
            # Daemon hasn't written the status file yet
            return

        if not self._it_is_my_turn(town_status):
            # Passive update — daemon wrote state without a think prompt.
            # OpenClaw would not call any tool here.
            return

        # It's our turn. Read structured state to make decisions.
        state = self._read_state_json()
        if not state:
            return

        agent = state.get("agent", {})
        location = agent.get("location", "")
        context = agent.get("location_context", "")
        self.active_conv = state.get("active_conversation")
        nearby = state.get("nearby", [])

        # Log nearby changes
        old_ids = {a.get("agent_id") for a in self.nearby}
        new_ids = {a.get("agent_id") for a in nearby}
        if old_ids != new_ids and nearby:
            names = [a.get("display_name", a.get("name", "?")) for a in nearby]
            self.log(f"👀 nearby: {names}")
        self.nearby = nearby

        # In conversation → reply
        if self.active_conv:
            conv_id = self.active_conv.get("conv_id", "")
            turns = self.active_conv.get("turns", [])
            participants = self.active_conv.get("participants", [])
            # End after 5+ turns sometimes
            if len(turns) >= 5 and random.random() < 0.4:
                self.log(f"→ ending conv ({len(turns)} turns, participants={len(participants)})")
                await self.act("end", conv_id)
                return
            # Don't speak twice in a row in groups
            if turns and len(participants) > 2:
                last = turns[-1].get("speaker", "")
                if last == self.display:
                    return
            reply = random.choice(REPLIES[self.name])
            await self.act("say", conv_id, reply)
            return

        # In apartment? Get out.
        if context == "apartment":
            await self.act("move", "exit")
            return

        # Pending messages? (chat invites)
        pending = state.get("pending_messages", [])
        for msg in pending:
            if msg.get("event") == "conversation_invite":
                conv_id = msg.get("conv_id", "")
                reply = random.choice(REPLIES[self.name])
                self.log(f"→ replying to invite in conv {conv_id[:8]}: {reply!r}")
                await self.act("say", conv_id, reply)
                return

        # At a location → decide
        r = random.random()
        if nearby and r < 0.5:
            target = random.choice(nearby)
            # agent_id (UUID) is the only globally unique identifier — machine
            # names are per-user and display names can collide.
            target_id = target.get("agent_id", "")
            if target_id:
                opener = random.choice(OPENERS[self.name])
                await self.act("chat", target_id, opener)
                return

        if location in ACTIVITIES_BY_LOCATION and r < 0.75:
            activity = random.choice(ACTIVITIES_BY_LOCATION[location])
            await self.act(activity)
            return

        dest = random.choice([loc for loc in TOWN_LOCATIONS if loc != location])
        await self.act("move", dest)

    async def run(self) -> None:
        """Brain loop — ticks on heartbeat."""
        # Stagger initial think
        await asyncio.sleep(random.uniform(2.0, 5.0))
        while True:
            try:
                await self.think()
            except Exception as e:
                self.log(f"⚠ think error: {e}")
            # Heartbeat with small jitter
            await asyncio.sleep(HEARTBEAT_SEC + random.uniform(-2.0, 2.0))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    log("test", "=" * 70)
    log("test", "GooseTown Test Harness — 6 Friends via CLI (daemon per agent)")
    log("test", f"API: {API_URL}")
    log("test", f"WS:  {WS_URL}")
    log("test", f"Workspaces: {WORKSPACE_ROOT}")
    log("test", "=" * 70)

    # Clean workspace
    if WORKSPACE_ROOT.exists():
        shutil.rmtree(WORKSPACE_ROOT)
    WORKSPACE_ROOT.mkdir(parents=True)

    agents = [AgentCLI(info) for info in FRIENDS]

    # Phase 1: Join each (registers + starts daemon)
    # Stagger by 2s so daemon startup is sequential — avoids pipe/socket contention.
    log("test", "PHASE 1 — Joining (registration + daemon startup), staggered 2s apart")
    for a in agents:
        await a.join()
        await asyncio.sleep(2.0)

    joined = [a for a in agents if a.joined]
    log("test", f"Joined: {len(joined)}/{len(agents)}")
    if not joined:
        log("test", "No successful joins — aborting.")
        return

    # Phase 2: Run brains in parallel
    log("test", "PHASE 2 — Running brains (heartbeat every 15s, Ctrl+C to stop)")
    log("test", "=" * 70)

    tasks = [asyncio.create_task(a.run()) for a in joined]

    def shutdown_handler(signum, frame):
        log("test", "Shutting down...")
        for t in tasks:
            t.cancel()

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        log("test", "Cleaning up daemons...")
        for a in joined:
            a.kill_daemon()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nbye")
