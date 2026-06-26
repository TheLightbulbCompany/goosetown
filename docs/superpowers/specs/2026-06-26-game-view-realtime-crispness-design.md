# Game View: Real-Time Updates + Crisp Rendering — Design

**Date:** 2026-06-26
**Status:** Approved design, pre-implementation
**Author:** brainstorming session (prez2307 + Claude)

## Goal

Make the GooseTown game view (the watchable town) feel **live** and look **crisp**:
1. Cut the perceived update lag from ~2s to near-real-time.
2. Fix cross-screen pixel-art blur and small-text readability.

## Background — current state (with evidence)

**Update cadence (the lag):**
- The headless game server broadcasts viewer `world_state` every **2.0s** — `apps/godot/scripts/server/game_server.gd:10` `const VIEWER_STATE_INTERVAL: float = 2.0`, applied in `_process` at lines 84-86.
- The browser Godot viewer interpolates each agent over a **2000ms** window — `apps/godot/scripts/agent.gd:100` `INTERP_DURATION_MS = 2000` (snapshot LERP in `_physics_process`, lines ~200-248).
- Net: an action takes up to 2s to appear, then renders as a slow 2s glide. The user's complaint is the **lag** (time-to-first-change), not the smoothness.

**Delivery path:** game server → backend `ViewerRelay` (a single game-server viewer peer) → API Gateway management-API fan-out → browser WS → React `useTownState` → JS bridge → Godot `agent_manager`. This fan-out was just hardened in #416 (off-loop `to_thread`, dead-connection pruning).

**Daemon feed is separate:** the resident-perception `agent_state` feed is emitted every 2.0s by `apps/godot/scripts/server/agent_controller.gd` (`STATE_INTERVAL = 2.0`). **Out of scope** — speeding it up would multiply resident-facing load and the synchronous enrichment DB reads in `daemon_bridge._send_to_daemon`.

**Cross-screen blur:** texture filtering is already NEAREST (`apps/godot/project.godot:34` `default_texture_filter=0`) and the WebGL canvas is DPR-aware (`apps/frontend/src/components/GodotCanvas.tsx:191` `canvas.width = rect.width * devicePixelRatio`). The blur comes from `window/stretch/mode="canvas_items"` + `aspect="expand"` (`project.godot:25-26`) with **no integer scale snapping** — the 2D view scales by a fractional factor that varies per screen/window/DPR, so pixels render at uneven sizes (worst on size-9 text).

## Non-goals

- **Approach B (direct game-server→browser streaming):** rejected. The game server is private (Cloud Map, `assignPublicIp: false` in `gameserver-stack.ts:89`), thin (no auth), and single-replica/can't-scale (in-memory world state). Viewer fan-out belongs on the autoscaling backend. Revisit only as a future scaling investment.
- **Daemon `agent_state` cadence** — stays 2s.
- **Bubble restyle** (shape/asset/colors), show/hide animation, speech duration, truncation — the user's gripes were readability + crispness only.

---

## Workstream 1 — Real-time viewer updates (Approach A)

### W1.1 — Higher broadcast rate
- `game_server.gd`: `VIEWER_STATE_INTERVAL` 2.0 → **0.1** (10 Hz cap). Single tunable constant.

### W1.2 — Skip-when-unchanged + heartbeat (load safety)
Each viewer tick (now 0.1s), build the viewer `world_state` and decide whether to actually broadcast:
- **Broadcast** if the state *materially changed* vs the last broadcast: roster changed (agent added/removed), OR any agent moved > `POSITION_EPSILON` (~1.0 px), OR any agent's `facing` / `activity` / `speech` text changed.
- **Heartbeat:** also broadcast if `>= VIEWER_HEARTBEAT_INTERVAL` (2.0s) since the last broadcast regardless of change — so newly-connected viewers and any drift reconcile to a full snapshot within 2s.
- Otherwise **skip** (reset the cadence timer, no fan-out).

Result: idle town ≈ today's 0.5 Hz heartbeat load; active town up to 10 Hz. **Load scales with activity, not wall-clock** — the key property that keeps this off the path that just flooded.

Implementation: store the last-broadcast snapshot (per-agent `position`/`facing`/`activity`/`speech`, keyed by `agent_id`) and the last-broadcast timestamp on `game_server.gd`. **The message shape is unchanged** (always a full `world_state`), so there is no viewer protocol change and no despawn-on-partial risk — `agent_manager` keeps treating each message as the full roster.

### W1.3 — ViewerRelay frame coalescing (backend)
At 10 Hz, a new `world_state` can arrive while the previous fan-out is still in flight, piling up overlapping fan-outs. Make `ViewerRelay` **latest-wins**: keep a single pending slot for the most recent `world_state` and a single consumer that fans out the latest, dropping superseded intermediate frames. A burst coalesces to the newest frame; viewers never fall behind real-time, and the fan-out can't pile up.
- File: `apps/backend/core/services/viewer_relay.py` (`_run_loop`/`_broadcast` → single-slot pending + worker).

### W1.4 — Viewer interpolation
- `agent.gd`: `INTERP_DURATION_MS` 2000 → **150** (slightly above the 100ms cap for continuous smoothness). Agents track their live position; during a 2s idle heartbeat the position is unchanged, so no visible motion. Keep the existing safety: `STALE_INTERP_THRESHOLD_MS` snap and the tab-refocus snap (`REFOCUS_DELTA_THRESHOLD_MS`).

### W1.5 — Scope guard
- Do **NOT** touch `agent_controller.gd: STATE_INTERVAL` (daemon `agent_state` stays 2.0s).

### Files
- `apps/godot/scripts/server/game_server.gd` (interval, change-detection, heartbeat)
- `apps/godot/scripts/agent.gd` (`INTERP_DURATION_MS`)
- `apps/backend/core/services/viewer_relay.py` (coalescing)
- Re-export `apps/frontend/public/godot/goosetown.{html,js,wasm,pck}` (headless export, commit the `.pck`)

### Verification
- In-browser: agents start moving within ~0.1–0.2s of acting, smoothly, at true speed; idle agents stay put.
- Backend: fan-out rate tracks town activity (near-zero idle, up to 10 Hz active); **no `/health` latency regression** (re-check the metric from today's incident).

---

## Workstream 2 — Crisp rendering + readable bubbles

### W2.1 — Integer scaling (with letterbox tuning)
- `apps/godot/project.godot`: add `window/stretch/scale_mode="integer"` under `[display]`. Snaps the 2D scale to whole numbers → uniform crisp pixels on every screen/DPR, game-wide.
- **Known interaction to tune:** integer fill quality depends on the base viewport (currently `viewport_width=1408`, `viewport_height=864`). On a high-DPI canvas, `floor(canvas_px / 1408)` can land on scale 1 with large letterbox margins. Implementation must **verify fill across DPR=1 and DPR=2 and several window sizes in-browser**, and tune one of:
  - keep 1408 base and accept the margins (style them — see below), or
  - reduce the base viewport to a cleaner pixel-art resolution (e.g. 704×432) so integer steps (2×, 3×) fill more — note this zooms the view in (fewer tiles visible), a visible change to confirm.
- **Letterbox styling:** set the canvas/container background so margins read as intentional (town base background or neutral dark) — `apps/frontend/src/components/GodotCanvas.tsx` / its CSS container.
- **Fallback:** if integer letterbox is unacceptably large on common screens after tuning, fall back to `window/stretch/mode="viewport"` (fills the frame; consistent though less reliably crisp than integer). Decide via the in-browser check, not blind.

### W2.2 — Bubble + nameplate readability
- `apps/godot/scenes/agent.tscn`: `SpeechText` font size **9 → 13**; `Nameplate` font size **10 → 13** (consistency). Keep `upheaval_pro.ttf` (style is fine per the user).
- The bubble auto-sizes to text (PR #346), so larger text → proportionally larger bubble. Verify wrapping: if 13px wraps too aggressively at `SPEECH_MAX_TEXT_WIDTH=130` (`agent.gd:163`), bump it to ~160.

### Files
- `apps/godot/project.godot` (`scale_mode`, possibly base viewport)
- `apps/godot/scenes/agent.tscn` (font sizes), possibly `apps/godot/scripts/agent.gd` (`SPEECH_MAX_TEXT_WIDTH`)
- `apps/frontend/src/components/GodotCanvas.tsx` / CSS (letterbox background)
- Re-export `apps/frontend/public/godot/goosetown.{html,js,wasm,pck}`

### Verification
- In-browser on a Retina/HiDPI screen and a standard screen, at several window sizes: sprites, tiles, and bubble text are crisp (no fuzzy edges); letterbox margins look intentional; bubble text is comfortably readable.

---

## Risks & trade-offs

- **Load (W1):** higher active-period fan-out. Mitigated by skip-when-unchanged + heartbeat + coalescing + the hardened delivery path + a tunable rate. Watch `/health` latency and fan-out rate after the dev deploy; dial `VIEWER_STATE_INTERVAL` up if needed.
- **Letterbox (W2):** integer scaling trades exact fill for crispness (user accepted). Tuned in-browser; `viewport` fallback if margins are too large.
- **`.pck` discipline:** every Godot change must re-export and commit the `.pck` alongside the `.gd`/`.tscn`/`project.godot` changes (Critical Rule).
- **Interp gaps (W1):** sparse updates from skip-when-unchanged + 150ms LERP — covered by the existing stale/refocus snap logic.

## Rollout

Single PR, two clearly-separated workstreams. Dev-verify in-browser before prod (the deploy auto-promotes dev→prod). Tune `VIEWER_STATE_INTERVAL` and the integer-scaling fill based on dev observation.
