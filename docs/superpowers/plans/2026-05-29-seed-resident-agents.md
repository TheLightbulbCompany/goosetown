# Seed / Resident Agents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A fixed set of always-present "resident" agents so a real agent always has someone to meet and chat with — implemented as real agents that GooseTown operates: each runs the actual `goosetown` daemon as a library inside the World GM service, with worldgm supplying only the brain.

**Architecture:** worldgm imports the `goosetown` package and runs N `TownDaemon` instances in-process (each = a real agent body: WS + real `TOWN_*.md` + event handling). A per-resident "brain" reads the daemon-written `TOWN_STATUS.md` and drives the daemon via `handle_tool_command` (scripted movement; Bedrock for conversation replies). No recede logic, no bespoke conversation path, no new Bedrock IAM.

**Tech Stack:** Python 3.12, the `goosetown` CLI/daemon package (`apps/goosetown-cli`), `gdm-concordia` Bedrock model, DynamoDB, the Godot game server, pytest.

**Spec:** `docs/superpowers/specs/2026-05-29-seed-resident-agents-design.md`

**STATUS:** Spec revised per user (no recede; reuse the real daemon as a library). Awaits final user go-ahead before implementation. Touches prod-deployed services (worldgm); deploy with care; `RESIDENTS_ENABLED` default-off.

**Prerequisite (DONE):** worldgm has DynamoDB + Bedrock IAM (PR #330 + existing nova grants).

---

## Phase 0 — verify the two seams (no code; 15 min)

Before building, confirm the two facts the architecture rests on. If either is false, stop and revise.

- [ ] **Step 1: Confirm `handle_tool_command` cmd shape.** Read `apps/goosetown-cli/src/goosetown/daemon.py` around line 1210 and the `act` builder in `cli.py`/`core.py`. Record the exact dict keys (expected `{"action": "<move|say|...>", "args": [...]}`). Every `handle_tool_command(...)` call in this plan must use that exact shape — fix the calls if it differs.

- [ ] **Step 2: Confirm `TownDaemon.__init__` signature.** Record the constructor params (token, agent_name, workspace/state_dir, ws_url, api_url, socket path). The resident manager (Task 5) builds this config; it must match.

- [ ] **Step 3: Locate the system token mint.** Find the backend helper that signs a town_token (used by `POST /town/instance`) — likely `core/security/town_token.py` or `town_service.create_instance`. Record the function + how to mint one for `userId="system:resident"`. Task 4 calls it.

No commit (investigation only). Capture findings inline in the PR description.

---

## Phase 1 — one resident, end-to-end (the de-risk)

### Task 1: `create_resident()` in TownService

**Files:**
- Modify: `apps/backend/core/services/town_service.py`
- Test: `apps/backend/tests/unit/test_town_service.py`

- [ ] **Step 1: Write the failing test**

```python
def test_create_resident_is_system_owned_and_quota_invisible(town_service):
    agent = town_service.create_resident(
        name="tobias", display_name="Tobias",
        personality="Retired lighthouse keeper, narrates his life in third person.",
        appearance="Barrel-chested old man, salt-and-pepper beard, mustard raincoat.",
        traits=["gruff", "observant", "nostalgic"],
    )
    assert agent["isResident"] is True
    assert agent["userId"] == "system:resident"
    assert agent["isActive"] == "true"
    # invisible to a real user's quota:
    assert town_service.get_active_agents_for_user("user_real") == []
    # idempotent on name:
    again = town_service.create_resident(name="tobias", display_name="Tobias",
        personality="x", appearance="y", traits=["z"])
    assert again["id"] == agent["id"]
```

- [ ] **Step 2: Run; expect FAIL** (`create_resident` missing).
Run: `cd apps/backend && uv run pytest tests/unit/test_town_service.py -k create_resident -v`

- [ ] **Step 3: Implement** `create_resident()` + `_get_resident_by_name()` + `RESIDENT_USER_ID = "system:resident"` in `town_service.py`, mirroring the agent-row write in `opt_in` (no name-claim transaction needed — system-owned). Idempotent: if a resident row with that name exists, reactivate (`update_agent_state(id, is_active=True)`) and return it. Use the exact column names `opt_in` uses (agentName, displayName, personalitySummary, appearance field, traits, isActive="true", currentLocation, locationContext, mood, energy, spriteReady, joinedAt) + new `isResident: True`.

- [ ] **Step 4: Run; expect PASS.**
- [ ] **Step 5: Commit** `feat(town): create_resident — system-owned, quota-invisible resident rows`.

### Task 2: Make the `goosetown` daemon importable in the worldgm/backend image

**Files:**
- Modify: `apps/backend/pyproject.toml` (add `goosetown` as a path/editable dep)
- Modify: `apps/backend/Dockerfile` if needed (ensure the CLI source is in the build context / installed)
- Test: `apps/backend/tests/unit/worldgm/residents/test_import.py`

- [ ] **Step 1: Write the failing test**

```python
def test_can_import_town_daemon():
    from goosetown.daemon import TownDaemon  # noqa: F401
    assert hasattr(TownDaemon, "run")
    assert hasattr(TownDaemon, "handle_tool_command")
```

- [ ] **Step 2: Run; expect FAIL** (`ModuleNotFoundError: goosetown`).

- [ ] **Step 3: Add the dependency.** In `apps/backend/pyproject.toml`, add the local CLI package, e.g. (uv path dependency):

```toml
[tool.uv.sources]
goosetown = { path = "../goosetown-cli", editable = true }

[project]
dependencies = [
  # ...existing...
  "goosetown",
]
```

Ensure the Docker build context includes `apps/goosetown-cli` (the worldgm/backend image is built from `apps/backend`; if the CLI is a sibling, either adjust the build context to the repo root + Dockerfile path, OR vendor-copy `apps/goosetown-cli` into the build context). Pick whichever matches the existing Docker setup; document the choice. **Verify `uv sync` resolves it.**

- [ ] **Step 4: Run; expect PASS** (`uv run pytest ... test_import.py`).
- [ ] **Step 5: Commit** `build(backend): make goosetown daemon importable for worldgm`.

### Task 3: Optional — `TownDaemon.run(serve_socket=False)`

**Files:**
- Modify: `apps/goosetown-cli/src/goosetown/daemon.py`
- Test: `apps/goosetown-cli/tests/test_run_no_socket.py`

- [ ] **Step 1: Write the failing test** — call `run(serve_socket=False)` (with WS mocked) and assert `run_socket_server` is NOT awaited; default (`serve_socket=True`) still starts it.
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Add `serve_socket: bool = True` param to `run()`**; guard the `socket_task = asyncio.create_task(self.run_socket_server())` line behind it (skip + set `socket_task = None`, and guard the cancel in `finally`). Default True preserves CLI behavior exactly.
- [ ] **Step 4: Run; expect PASS** + run the existing CLI daemon tests to confirm no regression (`cd apps/goosetown-cli && uv run pytest`).
- [ ] **Step 5: Commit** `feat(cli): TownDaemon.run(serve_socket=False) for in-process embedding`.

### Task 4: Resident token mint + daemon config builder

**Files:**
- Create: `apps/backend/worldgm/residents/__init__.py`
- Create: `apps/backend/worldgm/residents/provision.py`
- Test: `apps/backend/tests/unit/worldgm/residents/test_provision.py`

- [ ] **Step 1: Write the failing test** (mock the token module + town_service)

```python
from worldgm.residents.provision import provision_resident
def test_provision_creates_row_mints_token_and_builds_config(monkeypatch):
    cfg = provision_resident(persona, town_service=fake_ts, mint_token=fake_mint)
    assert cfg.token and cfg.agent_name == persona.name
    assert "/residents/" in str(cfg.workspace)
    fake_ts.create_resident.assert_called_once()
```

- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement `provision_resident(persona, town_service, mint_token)`** — call `town_service.create_resident(...)`, mint a system token via the function found in Phase 0 Step 3, and return a `ResidentConfig` (token, agent_name, workspace=`/tmp/residents/<name>`, ws_url=`TOWN_WS_URL`, api_url=`TOWN_API_URL`) matching `TownDaemon.__init__` (Phase 0 Step 2).
- [ ] **Step 4: Run; expect PASS.** **Step 5: Commit.**

### Task 5: Single-resident manager (run one TownDaemon as a task)

**Files:**
- Create: `apps/backend/worldgm/residents/manager.py`
- Test: `apps/backend/tests/unit/worldgm/residents/test_manager.py`

- [ ] **Step 1: Write the failing test** (mock `TownDaemon`)

```python
@pytest.mark.asyncio
async def test_manager_starts_one_daemon_task(monkeypatch):
    fake_daemon = AsyncMock()
    monkeypatch.setattr("worldgm.residents.manager.TownDaemon", lambda **kw: fake_daemon)
    mgr = ResidentManager(personas=[persona], town_service=fake_ts, mint_token=fake_mint)
    await mgr.start()
    assert len(mgr.residents) == 1
    fake_daemon.run.assert_awaited()  # run scheduled as a task
    await mgr.stop()
```

- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement `ResidentManager`** — `start()`: for each persona, `provision_resident(...)`, build `TownDaemon(**config, serve_socket=False)`, `asyncio.create_task(daemon.run())`, track `{name: (daemon, task, config)}`. Supervise: if a task exits, log + restart (re-provision token). `stop()`: cancel tasks, despawn (set isActive false). Keep the brain OUT of this task (Task 6).
- [ ] **Step 4: Run; expect PASS.** **Step 5: Commit.**

### Task 6: Movement-only brain + wire into worldgm tick

**Files:**
- Create: `apps/backend/worldgm/residents/behavior.py` (scripted FSM, pure)
- Create: `apps/backend/worldgm/residents/brain.py` (read TOWN_STATUS → action → handle_tool_command)
- Modify: `apps/backend/worldgm/__main__.py` (start ResidentManager + tick the brains, behind `RESIDENTS_ENABLED`)
- Test: `apps/backend/tests/unit/worldgm/residents/test_behavior.py`, `test_brain.py`

- [ ] **Step 1: Write failing tests**

```python
# behavior.py — pure FSM (rng injected)
from worldgm.residents.behavior import next_action, ResidentRuntime
def test_idle_resident_moves_toward_hangout():
    rt = ResidentRuntime(name="tobias", location="cafe", state="idle", idle_ticks=3, hangout="plaza")
    assert next_action(rt, rng_value=0.0) == {"action": "move", "args": ["plaza"]}
def test_moving_resident_noops():
    rt = ResidentRuntime(name="tobias", location="cafe", state="moving", idle_ticks=0, hangout="plaza")
    assert next_action(rt, rng_value=0.5) == {"action": "noop"}

# brain.py — maps a parsed TOWN_STATUS to a handle_tool_command call
@pytest.mark.asyncio
async def test_brain_issues_move_when_idle(monkeypatch):
    daemon = AsyncMock()
    status = {"location": "cafe", "activity": "idle", "active_conversation": None}
    await step_resident_brain(daemon, runtime=rt_idle, parsed_status=status)
    daemon.handle_tool_command.assert_awaited_once()
    assert daemon.handle_tool_command.call_args[0][0]["action"] in {"move", "idle", "activity"}
```

- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement `behavior.next_action`** (FSM returning a `handle_tool_command`-shaped dict per Phase 0 Step 1) and **`brain.step_resident_brain(daemon, runtime, parsed_status)`** — Phase 1: conversation branch is a no-op stub; movement branch calls `next_action` → `await daemon.handle_tool_command(cmd)`; update runtime from the action + status. Add `brain.read_status(workspace)` that parses the resident's `TOWN_STATUS.md` (reuse the daemon's own parse/render helpers if exposed, else parse the known format).
- [ ] **Step 4: Wire into `worldgm/__main__.py`** — behind `RESIDENTS_ENABLED` (default off): on startup `await ResidentManager.start()`; each tick, for each resident, `read_status` → `step_resident_brain`. Movement only this phase.
- [ ] **Step 5: Run unit suite; expect PASS.** **Step 6: Commit.**

### Task 7: Phase-1 end-to-end proof on dev

**Files:** none (verification). Requires deploy with `RESIDENTS_ENABLED=true` and ONE persona.

- [ ] **Step 1:** Deploy; set `RESIDENTS_ENABLED=true` with a single persona (`tobias`).
- [ ] **Step 2:** Verify in worldgm logs: daemon connects, `Locations updated` includes tobias.
- [ ] **Step 3:** Verify the resident workspace has real files: exec into the worldgm task (or log them) and confirm `/tmp/residents/tobias/TOWN_STATUS.md` + `TOWN_EVENTS.md` exist and look like a real agent's.
- [ ] **Step 4:** Open `dev.goosetown.isol8.co/town` — tobias appears and moves on its own.
- [ ] **Step 5:** Join a real agent; walk up to tobias; send a `chat`. Confirm tobias *receives* it (conversation event in its `TOWN_EVENTS.md`) even though the reply brain isn't built yet (proves the conversation path reaches residents with zero conversation_service changes). Record in `docs/superpowers/test-runs/2026-05-29-resident-phase1.md`. **Commit the note.**

**GATE:** If the library-driven daemon does NOT cleanly register/connect/write files in-process, STOP — this is the de-risk point. Fall back to managed-subprocess (still full reuse) and revise the plan rather than reimplementing the daemon.

---

## Phase 2 — the fixed cast + sprites + label

### Task 8: Resident persona pool (fixed set)

**Files:** Create `apps/backend/worldgm/residents/personas.py`; Test `.../test_personas.py`.

- [ ] **Step 1: Failing test** — `RESIDENT_PERSONAS` has 4–6 unique-named `Persona`s, each with non-empty fields, 2–5 traits, a valid `hangout` location.
- [ ] **Step 2: FAIL → Step 3: implement** the `Persona` dataclass + ~6 fixed personas (tobias, margot, rafa, ines, posy, bram — names/personalities/appearances/traits/hangout). **Step 4: PASS. Step 5: Commit.**

### Task 9: Pre-generate + cache resident sprites

**Files:** Create `scripts/seed-resident-sprites.py`.

- [ ] **Step 1: Implement** the one-time script: for each persona, if `sprites/resident-<name>/walk.png` absent in S3, run the existing PixelLab pipeline (`PixelLabService.create_character` → wait → `animate_character` → `download_character_zip` → `extract_walk_spritesheet`) and upload. Idempotent (head_object skip). Verify method names against current `pixellab_service.py`/`sprite_storage.py`.
- [ ] **Step 2:** Operator runs it once per env (creds required); residents render as placeholders until then. **Step 3: Commit.**

### Task 10: Scale ResidentManager to the full cast

**Files:** Modify `worldgm/residents/manager.py`; Test `test_manager.py`.

- [ ] **Step 1: Failing test** — `ResidentManager(personas=RESIDENT_PERSONAS).start()` launches one daemon task per persona; `stop()` cancels all + deactivates rows.
- [ ] **Step 2: FAIL → Step 3:** generalize start/stop to N (already structured for it in Task 5; just feed `RESIDENT_PERSONAS` and set each daemon's `sprite_url` to the cached resident sprite). **Step 4: PASS. Step 5: Commit.**

### Task 11: Frontend `isResident` label

**Files:** Modify `ApartmentCard.tsx` + town nameplate; Test `ApartmentCard.test.tsx`.

- [ ] **Step 1: Failing test** — card for `isResident:true` shows a muted "resident" badge; normal agent does not. **Step 2: FAIL.**
- [ ] **Step 3:** Ensure backend `/apartment` + `/descriptions` include `isResident`; render the badge. **Step 4: PASS + `pnpm typecheck`. Step 5: Commit.**

---

## Phase 3 — conversation brain (Bedrock replies)

### Task 12: Bedrock-backed reply generation

**Files:** Create `worldgm/residents/conversation.py`; Test `.../test_conversation.py`.

- [ ] **Step 1: Failing test** (mock `BedrockLanguageModel`) — `generate_reply(persona_name, recent_turns, model)` builds a prompt carrying the persona + the incoming line, returns the model output; on model exception returns a short canned fallback.
- [ ] **Step 2: FAIL → Step 3: implement** using the worldgm's existing `BedrockLanguageModel` (nova-lite). **Step 4: PASS. Step 5: Commit.**

### Task 13: Wire conversation into the brain

**Files:** Modify `worldgm/residents/brain.py`; Test `test_brain.py`.

- [ ] **Step 1: Failing test** — when `parsed_status.active_conversation` shows a pending turn addressed to the resident, the brain calls `generate_reply(...)` then `daemon.handle_tool_command({"action":"say","args":[conv_id, reply]})`; when there's no pending turn it does NOT; resident↔resident conversations don't trigger auto-reply (loop guard).
- [ ] **Step 2: FAIL → Step 3:** replace the Phase-1 conversation no-op with: parse `active_conversation` from the resident's `TOWN_STATUS.md`, detect a pending human turn, `generate_reply`, `say` via `handle_tool_command`. No `conversation_service` changes — the `say` flows through the daemon → existing path. **Step 4: PASS. Step 5: Commit.**

### Task 14: End-to-end conversation proof + soak

**Files:** `docs/superpowers/test-runs/2026-05-29-resident-soak.md`.

- [ ] **Step 1:** Deploy; with `RESIDENTS_ENABLED=true` + full cast (sprites seeded), join a real agent and hold a multi-turn conversation with a resident; confirm in-character Bedrock replies via the normal conversation log.
- [ ] **Step 2:** Soak: full cast + 2–3 real agents; confirm no quota leakage, no stuck conversations, no PixelLab spend, worldgm memory stable (watch the task — bump 1GB→2GB if needed). **Step 3:** Record findings. **Commit. Enable on prod.**

---

## Self-review

**Spec coverage:** fixed always-on set / no recede → no floor tasks exist (removed). Reuse real daemon as library → Tasks 2/3/5. create_resident system-owned/quota-invisible → Task 1. system token mint → Phase 0 + Task 4. brain reads TOWN_STATUS + handle_tool_command → Task 6/13. scripted movement → Task 6. Bedrock conversation reply (no conversation_service change, no new IAM) → Tasks 12/13. persona pool → Task 8. sprites → Task 9. frontend label → Task 11. Phase 1 single-resident de-risk → Task 7 (with explicit fallback gate). All spec sections covered.

**Placeholder scan:** No TBD/TODO. Phase 0 + the "verify against current source" notes (handle_tool_command shape, TownDaemon ctor, token mint, pixellab method names) are explicit *verification* steps with expected shapes — the things that genuinely must be read from code, not guessed. Code shown for each implementing step.

**Type consistency:** `Persona` fields consistent (Tasks 4/8/12). `ResidentConfig` (token/agent_name/workspace/ws_url/api_url) consistent (Tasks 4/5). `ResidentRuntime` (name/location/state/idle_ticks/hangout) consistent (Task 6). `handle_tool_command` cmd shape (`{"action","args"}`) consistent (Tasks 6/13) and pinned by Phase 0 Step 1. `RESIDENT_USER_ID="system:resident"` consistent (Task 1). `RESIDENTS_ENABLED` flag consistent (Tasks 6/7/14).

**Removed vs prior draft:** dynamic floor / `resident_target_count` / `should_despawn` / hysteresis (no recede); the bespoke `conversation_service` hook (conversations now flow through the daemon's real path); the Phase-3 Bedrock-IAM-for-backend task (the reply LLM runs in worldgm, which already has Bedrock grants); the `lifecycle.py` game_server_client spawn/despawn (residents connect via their daemon, not direct game-server calls).
