# Game View: Real-Time Updates + Crisp Rendering — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the watchable town feel live (viewer updates 2s → up to 10 Hz, load-bounded) and render crisp on every screen (integer pixel scaling + readable bubbles).

**Architecture:** Two workstreams on the viewer path. W1 lowers the game-server viewer-broadcast interval and adds skip-when-unchanged + a 2s heartbeat so load scales with activity; the backend `ViewerRelay` coalesces bursts (latest-wins) so the higher rate can't pile up; the Godot viewer LERP window shrinks to track real-time. W2 switches Godot to integer stretch scaling and bumps the bubble/nameplate font. The daemon-facing `agent_state` feed is untouched.

**Tech Stack:** Godot 4.6 (GDScript, GdUnit4 tests), Python 3.12 FastAPI backend (pytest), Godot WebGL export.

## Global Constraints

- **Viewer path only.** NEVER modify `apps/godot/scripts/server/agent_controller.gd` `STATE_INTERVAL` (the daemon `agent_state` feed stays 2.0s). This plan only touches the *viewer* `world_state`.
- **Tunable constants:** `VIEWER_STATE_INTERVAL` (0.1), `VIEWER_HEARTBEAT_INTERVAL` (2.0), `POSITION_EPSILON` (1.0), `INTERP_DURATION_MS` (150) — defined as named constants, not magic numbers.
- **`.pck` discipline (Critical Rule):** every commit that changes a Godot source file (`.gd`, `.tscn`, `project.godot`) MUST re-export and stage the WebGL build in the same commit. Export command (run from repo root):
  ```bash
  /Applications/Godot.app/Contents/MacOS/Godot --headless --path apps/godot \
    --export-release "Web" apps/frontend/public/godot/goosetown.html
  ```
  Then `git add apps/frontend/public/godot/goosetown.{html,js,wasm,pck}` alongside the source change.
- **Godot tests:** GdUnit4, files in `apps/godot/tests/`, `extends GdUnitTestSuite`, `assert_that(x).is_true()/.is_false()/.is_equal(...)`. Run one suite locally:
  ```bash
  GODOT_BIN=/Applications/Godot.app/Contents/MacOS/Godot \
    apps/godot/addons/gdUnit4/runtest.sh -a res://tests/<file>.gd
  ```
- **Backend tests:** `cd apps/backend && uv run pytest tests/unit/<file>.py -v`; lint with `uv run ruff check <files> && uv run ruff format --check <files>`.
- **Project lives at `~/Desktop/goosetown.nosync`** (the `.nosync` suffix avoids the macOS-Tahoe iCloud FileProvider hang). Godot/export commands run fine here.
- **In-browser visual verification** (W2 + the W1 "feel") needs a running frontend. The subagent can run it via `cd apps/frontend && pnpm dev` started with `run_in_background: true` (serves `public/godot/` at localhost:5173) and drive it with the Chrome tools, OR hand the in-browser check to the user. The export succeeding + the unit tests passing are the automatable gates; the visual confirmation is explicitly called out per task.

---

### Task 1: ViewerRelay frame coalescing (backend)

Make `ViewerRelay` latest-wins so a 10 Hz inbound stream can't pile up overlapping fan-outs. Today `_run_loop` calls `await self._broadcast(data)` inline per frame; replace with a single pending slot + a worker that always fans out the newest frame, dropping superseded ones.

**Files:**
- Modify: `apps/backend/core/services/viewer_relay.py`
- Test: `apps/backend/tests/unit/test_viewer_relay_coalesce.py` (create)

**Interfaces:**
- Produces: `ViewerRelay._broadcast_worker()` (coroutine), `ViewerRelay._pending` (`dict | None`), `ViewerRelay._pending_event` (`asyncio.Event`, created in `start()`).
- Consumes: existing `ViewerRelay._broadcast(data: dict)` (unchanged) and `ViewerRelay._running`.

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/test_viewer_relay_coalesce.py`:

```python
"""ViewerRelay frame coalescing — a burst of world_states collapses to the
latest (intermediate frames dropped), so the 10Hz viewer path can't pile up
overlapping fan-outs."""

from __future__ import annotations

import asyncio

from core.services.viewer_relay import ViewerRelay


async def _drive_burst() -> list[int]:
    relay = ViewerRelay("ws://gs")
    relay._running = True
    relay._pending_event = asyncio.Event()

    broadcasted: list[int] = []
    first_started = asyncio.Event()
    release = asyncio.Event()

    async def fake_broadcast(data: dict) -> None:
        broadcasted.append(data["seq"])
        if data["seq"] == 1:
            first_started.set()
            await release.wait()  # hold frame 1 mid-fanout

    relay._broadcast = fake_broadcast  # type: ignore[method-assign]
    worker = asyncio.create_task(relay._broadcast_worker())

    relay._pending = {"seq": 1}
    relay._pending_event.set()
    await first_started.wait()  # frame 1 is now in flight, blocked

    # frames 2 and 3 arrive during frame 1's fanout -> coalesce to 3
    relay._pending = {"seq": 2}
    relay._pending_event.set()
    relay._pending = {"seq": 3}
    relay._pending_event.set()

    release.set()  # let frame 1 finish; worker loops and takes latest (3)
    await asyncio.sleep(0.05)

    relay._running = False
    relay._pending_event.set()
    worker.cancel()
    return broadcasted


def test_burst_coalesces_to_latest():
    assert asyncio.run(_drive_burst()) == [1, 3]  # frame 2 superseded by 3


async def _drive_single() -> list[int]:
    relay = ViewerRelay("ws://gs")
    relay._running = True
    relay._pending_event = asyncio.Event()
    broadcasted: list[int] = []

    async def fake_broadcast(data: dict) -> None:
        broadcasted.append(data["seq"])

    relay._broadcast = fake_broadcast  # type: ignore[method-assign]
    worker = asyncio.create_task(relay._broadcast_worker())
    relay._pending = {"seq": 7}
    relay._pending_event.set()
    await asyncio.sleep(0.02)
    relay._running = False
    relay._pending_event.set()
    worker.cancel()
    return broadcasted


def test_single_frame_delivered():
    assert asyncio.run(_drive_single()) == [7]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/test_viewer_relay_coalesce.py -v`
Expected: FAIL — `AttributeError: 'ViewerRelay' object has no attribute '_broadcast_worker'`.

- [ ] **Step 3: Implement coalescing in `viewer_relay.py`**

In `__init__`, add the pending slot (after `self._listen_task`):
```python
        self._pending: Optional[dict] = None
        self._pending_event: Optional[asyncio.Event] = None
        self._worker_task: Optional[asyncio.Task] = None
```

In `start()`, create the event + worker before starting the listen loop:
```python
    async def start(self) -> None:
        """Start the relay: connect to game server and begin listening."""
        self._running = True
        self._pending_event = asyncio.Event()
        self._worker_task = asyncio.create_task(self._broadcast_worker())
        self.seed_from_dynamodb()
        self._listen_task = asyncio.create_task(self._run_loop())
```

In `_run_loop`, replace the inline `await self._broadcast(data)` with enqueue-latest:
```python
                    if data.get("event") == "world_state":
                        self._pending = data
                        if self._pending_event is not None:
                            self._pending_event.set()
```

Add the worker (new method, place after `_run_loop`):
```python
    async def _broadcast_worker(self) -> None:
        """Fan out the most recent world_state, dropping superseded frames.

        Latest-wins: while a fan-out is in flight, newer frames overwrite the
        pending slot, so the next iteration always sends the freshest one. This
        keeps viewers real-time and stops 10Hz inbound from piling up overlapping
        fan-outs.
        """
        assert self._pending_event is not None
        while self._running:
            await self._pending_event.wait()
            self._pending_event.clear()
            data = self._pending
            self._pending = None
            if data is not None:
                await self._broadcast(data)
```

In `stop()`, cancel the worker (mirror the `_listen_task` cancel block):
```python
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/test_viewer_relay_coalesce.py -v`
Expected: PASS (2 passed).
Also run the existing prune tests to confirm no regression: `uv run pytest tests/unit/test_ws_connection_prune.py -v` → PASS.

- [ ] **Step 5: Lint + commit**

```bash
cd apps/backend && uv run ruff check core/services/viewer_relay.py tests/unit/test_viewer_relay_coalesce.py && uv run ruff format --check core/services/viewer_relay.py tests/unit/test_viewer_relay_coalesce.py
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync
git add apps/backend/core/services/viewer_relay.py apps/backend/tests/unit/test_viewer_relay_coalesce.py
git commit -m "feat(viewer): coalesce world_state fan-out (latest-wins) for the real-time rate"
```

---

### Task 2: Real-time viewer broadcast (game_server) + tighter LERP (agent)

Lower the viewer broadcast interval to 10 Hz, only broadcast when the viewer state materially changed (or a 2s heartbeat is due), and shrink the viewer interpolation window so agents track real-time. The change-detection is a pure static helper with GdUnit coverage.

**Files:**
- Modify: `apps/godot/scripts/server/game_server.gd` (interval, helper, skip/heartbeat, refactor broadcast into collect + send)
- Modify: `apps/godot/scripts/agent.gd` (`INTERP_DURATION_MS` 2000 → 150)
- Test: `apps/godot/tests/test_game_server_change_detection.gd` (create)
- Re-export: `apps/frontend/public/godot/goosetown.{html,js,wasm,pck}`

**Interfaces:**
- Produces: `static func viewer_state_changed(prev: Array, curr: Array, epsilon: float) -> bool` on `game_server.gd`.

- [ ] **Step 1: Confirm the viewer agent-dict keys**

Run: `grep -nA25 "func get_world_state_for_viewer\|func get_all_agent_states" apps/godot/scripts/server/agent_controller.gd apps/godot/scripts/*manager*.gd`
Note the exact per-agent keys (expected: `agent_id`, `x`, `y`, `facing`, `activity`, `speech`). If a key name differs (e.g. `speech_text` instead of `speech`), use the real names in Steps 2-3 below.

- [ ] **Step 2: Write the failing GdUnit test**

Create `apps/godot/tests/test_game_server_change_detection.gd`:

```gdscript
## Tests for GameServer.viewer_state_changed — the skip-when-unchanged gate
## that keeps the 10Hz viewer broadcast load-bounded to actual activity.
extends GdUnitTestSuite

const GameServer := preload("res://scripts/server/game_server.gd")


func _agent(id: String, x: float, y: float, facing := "south", activity := "idle", speech := "") -> Dictionary:
	return {"agent_id": id, "x": x, "y": y, "facing": facing, "activity": activity, "speech": speech}


func test_identical_state_is_unchanged() -> void:
	var prev := [_agent("1", 10.0, 20.0)]
	var curr := [_agent("1", 10.0, 20.0)]
	assert_that(GameServer.viewer_state_changed(prev, curr, 1.0)).is_false()


func test_subpixel_move_is_unchanged() -> void:
	var prev := [_agent("1", 10.0, 20.0)]
	var curr := [_agent("1", 10.5, 20.0)]
	assert_that(GameServer.viewer_state_changed(prev, curr, 1.0)).is_false()


func test_move_beyond_epsilon_is_changed() -> void:
	var prev := [_agent("1", 10.0, 20.0)]
	var curr := [_agent("1", 12.0, 20.0)]
	assert_that(GameServer.viewer_state_changed(prev, curr, 1.0)).is_true()


func test_facing_change_is_changed() -> void:
	var prev := [_agent("1", 10.0, 20.0, "south")]
	var curr := [_agent("1", 10.0, 20.0, "north")]
	assert_that(GameServer.viewer_state_changed(prev, curr, 1.0)).is_true()


func test_speech_change_is_changed() -> void:
	var prev := [_agent("1", 10.0, 20.0, "south", "idle", "")]
	var curr := [_agent("1", 10.0, 20.0, "south", "idle", "hi")]
	assert_that(GameServer.viewer_state_changed(prev, curr, 1.0)).is_true()


func test_roster_add_is_changed() -> void:
	var prev := [_agent("1", 10.0, 20.0)]
	var curr := [_agent("1", 10.0, 20.0), _agent("2", 5.0, 5.0)]
	assert_that(GameServer.viewer_state_changed(prev, curr, 1.0)).is_true()


func test_roster_swap_same_size_is_changed() -> void:
	var prev := [_agent("1", 10.0, 20.0)]
	var curr := [_agent("2", 10.0, 20.0)]
	assert_that(GameServer.viewer_state_changed(prev, curr, 1.0)).is_true()
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `GODOT_BIN=/Applications/Godot.app/Contents/MacOS/Godot apps/godot/addons/gdUnit4/runtest.sh -a res://tests/test_game_server_change_detection.gd`
Expected: FAIL — `viewer_state_changed` is not a function on `game_server.gd`.

- [ ] **Step 4: Implement the helper + skip/heartbeat wiring in `game_server.gd`**

Add the constants near the top (replacing the existing `VIEWER_STATE_INTERVAL`):
```gdscript
const VIEWER_STATE_INTERVAL: float = 0.1
const VIEWER_HEARTBEAT_INTERVAL: float = 2.0
const POSITION_EPSILON: float = 1.0
```

Add member state (near the other `_viewer_*` vars):
```gdscript
var _last_viewer_agents: Array = []
var _last_broadcast_sec: float = 0.0
```

Add the pure helper (top-level static func):
```gdscript
static func viewer_state_changed(prev: Array, curr: Array, epsilon: float) -> bool:
	if prev.size() != curr.size():
		return true
	var prev_by_id := {}
	for a in prev:
		prev_by_id[str(a.get("agent_id", ""))] = a
	var eps_sq := epsilon * epsilon
	for c in curr:
		var pid := str(c.get("agent_id", ""))
		if not prev_by_id.has(pid):
			return true
		var p: Dictionary = prev_by_id[pid]
		var dx := float(c.get("x", 0.0)) - float(p.get("x", 0.0))
		var dy := float(c.get("y", 0.0)) - float(p.get("y", 0.0))
		if dx * dx + dy * dy > eps_sq:
			return true
		if str(c.get("facing", "")) != str(p.get("facing", "")):
			return true
		if str(c.get("activity", "")) != str(p.get("activity", "")):
			return true
		if str(c.get("speech", "")) != str(p.get("speech", "")):
			return true
	return false
```

Refactor the existing `_broadcast_world_state()` (lines ~93-110) into a collector + sender, then add the gate. Split the current body so the agent-combining (town + apartment + library) lives in `_collect_viewer_agents() -> Array` returning the `agents` array, and the packet send lives in `_send_world_state(agents: Array)`. Then:
```gdscript
func _maybe_broadcast_world_state() -> void:
	var agents := _collect_viewer_agents()
	var now_sec := Time.get_ticks_msec() / 1000.0
	var changed := viewer_state_changed(_last_viewer_agents, agents, POSITION_EPSILON)
	var heartbeat_due := (now_sec - _last_broadcast_sec) >= VIEWER_HEARTBEAT_INTERVAL
	if changed or heartbeat_due:
		_send_world_state(agents)
		_last_viewer_agents = agents
		_last_broadcast_sec = now_sec
```

In `_process` (lines ~84-86), call the gate instead of the old direct broadcast:
```gdscript
	_viewer_timer += delta
	if _viewer_timer >= VIEWER_STATE_INTERVAL:
		_viewer_timer -= VIEWER_STATE_INTERVAL
		_maybe_broadcast_world_state()
```

- [ ] **Step 5: Run the GdUnit test to verify it passes**

Run: `GODOT_BIN=/Applications/Godot.app/Contents/MacOS/Godot apps/godot/addons/gdUnit4/runtest.sh -a res://tests/test_game_server_change_detection.gd`
Expected: PASS (7 tests).

- [ ] **Step 6: Shrink the viewer LERP in `agent.gd`**

Change `apps/godot/scripts/agent.gd:100`:
```gdscript
const INTERP_DURATION_MS: int = 150
```
(Leave `STALE_INTERP_THRESHOLD_MS` and `REFOCUS_DELTA_THRESHOLD_MS` unchanged — they remain the safety net for idle gaps and tab refocus.)

- [ ] **Step 7: Re-export the WebGL build**

Run (from repo root):
```bash
/Applications/Godot.app/Contents/MacOS/Godot --headless --path apps/godot \
  --export-release "Web" apps/frontend/public/godot/goosetown.html
```
Expected: exits 0; `apps/frontend/public/godot/goosetown.pck` regenerated.

- [ ] **Step 8: Commit (source + .pck together)**

```bash
git add apps/godot/scripts/server/game_server.gd apps/godot/scripts/agent.gd apps/godot/tests/test_game_server_change_detection.gd apps/frontend/public/godot/goosetown.html apps/frontend/public/godot/goosetown.js apps/frontend/public/godot/goosetown.wasm apps/frontend/public/godot/goosetown.pck
git commit -m "feat(town): real-time viewer updates — 10Hz cap, skip-when-unchanged + heartbeat, 150ms LERP"
```

- [ ] **Step 9: In-browser verification (feel)**

Serve the frontend (`cd apps/frontend && pnpm dev` via `run_in_background: true`) and open localhost:5173/town, or hand to the user. Confirm: agents begin moving within ~0.1–0.2s of acting (not a 2s beat), motion is smooth at true speed, idle agents are static. (If a key name was wrong in Step 1, agents won't update — fix the helper/collector keys and re-export.)

---

### Task 3: Crisp rendering — integer stretch scaling

Switch Godot to integer pixel scaling so the whole town is crisp on every screen/DPR, and style the letterbox margins so they read as intentional.

**Files:**
- Modify: `apps/godot/project.godot` (`[display]` → add `window/stretch/scale_mode="integer"`; possibly base viewport)
- Modify: `apps/frontend/src/components/GodotCanvas.tsx` (or its CSS) — letterbox background
- Re-export: `apps/frontend/public/godot/goosetown.{html,js,wasm,pck}`

**Interfaces:** none (config + visual).

- [ ] **Step 1: Add integer scale mode**

In `apps/godot/project.godot`, under `[display]` (next to `window/stretch/mode="canvas_items"`), add:
```
window/stretch/scale_mode="integer"
```

- [ ] **Step 2: Style the letterbox background**

In `apps/frontend/src/components/GodotCanvas.tsx`, ensure the canvas element (or its container) has a deliberate background so integer-scale margins look intentional rather than broken. Set the canvas background to the town's base ground tone (or a neutral dark, matching the site theme):
```tsx
// on the <canvas> style or its container:
style={{ backgroundColor: '#1a1722' }}  // match site dark theme; adjust to taste
```

- [ ] **Step 3: Re-export the WebGL build**

```bash
/Applications/Godot.app/Contents/MacOS/Godot --headless --path apps/godot \
  --export-release "Web" apps/frontend/public/godot/goosetown.html
```

- [ ] **Step 4: In-browser verification + letterbox tuning (the judgment step)**

Serve the frontend and open `/town` on at least: (a) a HiDPI/Retina display (DPR 2) and (b) a standard display (DPR 1), each at two window sizes. Confirm: sprites, tiles, and bubble text have crisp edges (no fuzz) and the pixel grid is uniform.
Decision tree on the letterbox margins:
- Margins small/acceptable → done.
- Margins too large on common screens → reduce the base viewport in `project.godot` `[display]` (`window/size/viewport_width`/`viewport_height`) toward a cleaner pixel-art resolution (try halving to `704`×`432`), re-export, re-check. NOTE: this zooms the view in (fewer tiles visible) — confirm the framing still looks right.
- If no base viewport gives acceptable fill AND crispness → fall back: set `window/stretch/mode="viewport"` (remove `scale_mode="integer"`), re-export, re-check (fills the frame; less reliably crisp but no large margins). Record which option was chosen in the commit message.

- [ ] **Step 5: Commit (source + .pck together)**

```bash
git add apps/godot/project.godot apps/frontend/src/components/GodotCanvas.tsx apps/frontend/public/godot/goosetown.html apps/frontend/public/godot/goosetown.js apps/frontend/public/godot/goosetown.wasm apps/frontend/public/godot/goosetown.pck
git commit -m "feat(godot): integer pixel scaling for crisp rendering across screens/DPR"
```

---

### Task 4: Bubble + nameplate readability

Bump the speech and nameplate font sizes so text is comfortably readable (integer scaling from Task 3 keeps it crisp).

**Files:**
- Modify: `apps/godot/scenes/agent.tscn` (`SpeechText` font size 9 → 13; `Nameplate` font size 10 → 13)
- Modify (conditional): `apps/godot/scripts/agent.gd` (`SPEECH_MAX_TEXT_WIDTH` 130 → 160, only if 13px wraps too aggressively)
- Re-export: `apps/frontend/public/godot/goosetown.{html,js,wasm,pck}`

**Interfaces:** none (config + visual).

- [ ] **Step 1: Bump the font sizes**

In `apps/godot/scenes/agent.tscn`: change the `SpeechText` Label `theme_override_font_sizes/font_size` from `9` to `13`, and the `Nameplate` label font size from `10` to `13`. (Find them via `grep -n "font_size" apps/godot/scenes/agent.tscn`.)

- [ ] **Step 2: Re-export the WebGL build**

```bash
/Applications/Godot.app/Contents/MacOS/Godot --headless --path apps/godot \
  --export-release "Web" apps/frontend/public/godot/goosetown.html
```

- [ ] **Step 3: In-browser verification**

Serve the frontend and trigger a conversation (or watch residents talk) on `/town`. Confirm: bubble text is comfortably readable and crisp; the auto-sized bubble (PR #346) hugs the larger text without clipping at the canvas edges.
- If 13px text wraps too aggressively (bubbles too tall/narrow), set `SPEECH_MAX_TEXT_WIDTH: float = 160.0` in `apps/godot/scripts/agent.gd` (line ~163), re-export, re-check.

- [ ] **Step 4: Commit (source + .pck together)**

```bash
git add apps/godot/scenes/agent.tscn apps/frontend/public/godot/goosetown.html apps/frontend/public/godot/goosetown.js apps/frontend/public/godot/goosetown.wasm apps/frontend/public/godot/goosetown.pck
# include agent.gd if SPEECH_MAX_TEXT_WIDTH was changed:
# git add apps/godot/scripts/agent.gd
git commit -m "feat(town): bump bubble + nameplate font to 13 for readability"
```

---

## Self-Review

**Spec coverage:**
- W1.1 broadcast rate → Task 2 Step 4 (`VIEWER_STATE_INTERVAL=0.1`). ✓
- W1.2 skip-when-unchanged + heartbeat → Task 2 (helper + `_maybe_broadcast_world_state`). ✓
- W1.3 ViewerRelay coalescing → Task 1. ✓
- W1.4 viewer LERP → Task 2 Step 6. ✓
- W1.5 scope guard (no `agent_controller` change) → Global Constraints + no task touches it. ✓
- W2.1 integer scaling + letterbox + fallback → Task 3. ✓
- W2.2 bubble/nameplate font + width fallback → Task 4. ✓
- `.pck` re-export discipline → Global Constraints + a re-export step in Tasks 2/3/4. ✓

**Placeholder scan:** the only deliberately-open item is the integer-scaling letterbox tuning (Task 3 Step 4), which is a judgment step with an explicit decision tree + fallback, not a vague placeholder. The agent-dict key confirmation (Task 2 Step 1) is a concrete verification step. No "TBD"/"handle edge cases"/missing code.

**Type/name consistency:** `viewer_state_changed(prev, curr, epsilon)` is defined in Task 2 Step 4 and called in `_maybe_broadcast_world_state` and the GdUnit test with the same signature. `_collect_viewer_agents()`/`_send_world_state(agents)` introduced and used within Task 2. `_broadcast_worker`/`_pending`/`_pending_event` consistent across Task 1 steps. Constants match the spec values.
