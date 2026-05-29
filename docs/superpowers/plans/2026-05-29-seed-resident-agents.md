# Seed / Resident Agents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep GooseTown populated above a density floor with server-driven "resident" agents that recede as real agents arrive, so a newly-joined real agent always has someone to meet and chat with.

**Architecture:** Extend the worldgm service (single-replica, tick loop, Bedrock, DDB). Each tick: reconcile resident population to a dynamic floor, then scripted-move active residents. Residents connect to the Godot game server server-side via the existing `game_server_client`. Conversations with residents are LLM-backed (Bedrock) and event-driven on the backend. Residents are `agents` rows flagged `isResident=true` with a system `userId`; sprites are pre-generated once and cached.

**Tech Stack:** Python 3.12, FastAPI backend, `gdm-concordia` Bedrock model, DynamoDB (boto3), the Godot headless game server, pytest.

**Spec:** `docs/superpowers/specs/2026-05-29-seed-resident-agents-design.md`

**STATUS NOTE (read before executing):** This plan was written autonomously overnight and **awaits the author's review of the spec**. Do not begin implementation until the user has reviewed `docs/superpowers/specs/2026-05-29-seed-resident-agents-design.md` and confirmed. It also touches prod-deployed services (worldgm, game server) — treat deploys with the usual care.

**Prerequisite (DONE):** worldgm task role now has DynamoDB access (PR #330, verified 2026-05-29). The floor controller's `get_active_agents()` query works.

---

## File structure

- `apps/backend/worldgm/residents/__init__.py` — package
- `apps/backend/worldgm/residents/personas.py` — the resident persona pool (data + loader)
- `apps/backend/worldgm/residents/floor.py` — pure floor-target math + reconcile orchestration
- `apps/backend/worldgm/residents/behavior.py` — scripted movement FSM
- `apps/backend/worldgm/residents/conversation.py` — Bedrock-backed reply generation
- `apps/backend/core/services/town_service.py` — add `create_resident()` + `isResident` handling (MODIFY)
- `apps/backend/worldgm/__main__.py` — wire reconcile + step into `_tick` (MODIFY)
- `apps/backend/core/services/conversation_service.py` — route resident replies to the conversation handler (MODIFY)
- `apps/frontend/src/components/ApartmentCard.tsx` + town nameplate — "resident" label (MODIFY)
- `scripts/seed-resident-sprites.py` — one-time sprite pre-generation (CREATE)
- Tests under `apps/backend/tests/unit/worldgm/residents/`

---

## Phase 1 — Static residents (always-on, connected, scripted movement)

### Task 1: `isResident` flag + `create_resident()` in TownService

**Files:**
- Modify: `apps/backend/core/services/town_service.py`
- Test: `apps/backend/tests/unit/test_town_service.py`

- [ ] **Step 1: Write the failing test**

```python
def test_create_resident_sets_resident_flag_and_system_user(town_service, agents_table):
    agent = town_service.create_resident(
        name="tobias",
        display_name="Tobias",
        personality="Retired lighthouse keeper who narrates his life in third person.",
        appearance="Barrel-chested, salt-and-pepper beard, mustard raincoat.",
        traits=["gruff", "observant", "nostalgic"],
    )
    assert agent["isResident"] is True
    assert agent["userId"] == "system:resident"
    assert agent["isActive"] == "true"
    # Resident must NOT count against any real user's quota:
    actives = town_service.get_active_agents_for_user("system:resident")
    assert any(a["agentName"] == "tobias" for a in actives)
```

- [ ] **Step 2: Run it; expect FAIL** (`AttributeError: 'TownService' object has no attribute 'create_resident'`).
Run: `cd apps/backend && uv run pytest tests/unit/test_town_service.py::test_create_resident_sets_resident_flag_and_system_user -v`

- [ ] **Step 3: Implement `create_resident`**

In `town_service.py`, add (mirrors the agent-row creation in `opt_in`, but with a fixed system user and no name-claim transaction since residents are system-owned):

```python
RESIDENT_USER_ID = "system:resident"

def create_resident(
    self,
    name: str,
    display_name: str,
    personality: str,
    appearance: str,
    traits: list[str],
) -> dict:
    """Create (or reactivate) a system-owned resident agent row.

    Residents have a fixed system userId so they're invisible to per-user
    quota checks. Idempotent on agentName: re-creating an existing resident
    flips it back to active rather than duplicating.
    """
    existing = self._get_resident_by_name(name)
    if existing:
        self.update_agent_state(existing["id"], is_active=True)
        return {**existing, "isActive": "true", "isResident": True}

    import uuid
    item = {
        "id": str(uuid.uuid4()),
        "userId": RESIDENT_USER_ID,
        "agentName": name,
        "displayName": display_name,
        "personalitySummary": personality,
        "appearanceDescription": appearance,
        "traits": traits,
        "isActive": "true",
        "isResident": True,
        "currentLocation": "plaza",
        "locationContext": "town",
        "mood": 0,
        "energy": 100,
        "spriteReady": False,
        "joinedAt": _now_iso(),
    }
    self.agents_table().put_item(Item=item)
    return item

def _get_resident_by_name(self, name: str) -> dict | None:
    resp = self.agents_table().query(
        IndexName="userId-agentName-index",
        KeyConditionExpression=Key("userId").eq(RESIDENT_USER_ID) & Key("agentName").eq(name),
    )
    items = resp.get("Items", [])
    return _decimal_to_native(items[0]) if items else None
```

(Confirm `_now_iso()` / the timestamp helper name used elsewhere in the file; reuse it. If `appearanceDescription` isn't an existing column, match the column name `opt_in` uses for appearance.)

- [ ] **Step 4: Run the test; expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/town_service.py apps/backend/tests/unit/test_town_service.py
git commit -m "feat(town): create_resident — system-owned resident agent rows"
```

### Task 2: Resident persona pool

**Files:**
- Create: `apps/backend/worldgm/residents/__init__.py` (empty)
- Create: `apps/backend/worldgm/residents/personas.py`
- Test: `apps/backend/tests/unit/worldgm/residents/test_personas.py`

- [ ] **Step 1: Write the failing test**

```python
from worldgm.residents.personas import RESIDENT_PERSONAS, Persona

def test_persona_pool_is_valid_and_larger_than_floor():
    assert len(RESIDENT_PERSONAS) >= 8  # more than RESIDENT_FLOOR(4) so runs vary
    names = [p.name for p in RESIDENT_PERSONAS]
    assert len(names) == len(set(names)), "persona names must be unique"
    for p in RESIDENT_PERSONAS:
        assert isinstance(p, Persona)
        assert p.name and p.display_name and p.personality and p.appearance
        assert 2 <= len(p.traits) <= 5
        assert p.hangout in {"plaza", "library", "cafe", "activity_center", "bank", "residence"}
```

- [ ] **Step 2: Run it; expect FAIL** (module not found).

- [ ] **Step 3: Implement `personas.py`**

```python
"""Curated resident persona pool. More personas than RESIDENT_FLOOR so the
town's resident cast varies. Each maps to a pre-generated sprite cached under
sprites/resident-<name>/walk.png (see scripts/seed-resident-sprites.py)."""
from __future__ import annotations
from dataclasses import dataclass, field

@dataclass(frozen=True)
class Persona:
    name: str            # machine name, lowercase, used as agentName + sprite key
    display_name: str
    personality: str
    appearance: str
    traits: tuple[str, ...]
    hangout: str         # weighted destination location id

RESIDENT_PERSONAS: tuple[Persona, ...] = (
    Persona("tobias", "Tobias", "Retired lighthouse keeper who narrates his own life in the third person and distrusts anyone who walks too fast.", "Barrel-chested old man, salt-and-pepper beard, mustard-yellow raincoat, slow gait.", ("gruff", "observant", "nostalgic", "stubborn"), "plaza"),
    Persona("margot", "Margot", "Sharp-eyed editor who reads everything twice and quotes the style guide unprompted.", "Tall woman, charcoal blazer, red pen behind one ear, half-moon glasses.", ("meticulous", "dry", "principled"), "library"),
    Persona("rafa", "Rafa", "Cafe regular who has opinions about everyone's coffee order and offers unsolicited life advice.", "Wiry man, espresso-stained apron, perpetual five-o'clock shadow, quick hands.", ("warm", "nosy", "generous"), "cafe"),
    Persona("ines", "Ines", "Botanist who treats the gardens like patients and talks to the plants.", "Short woman, dirt-smudged overalls, wide straw hat, gardening gloves.", ("gentle", "patient", "eccentric"), "activity_center"),
    Persona("dmitri", "Dmitri", "Ex-banker turned philosopher who counts everything and trusts no one's arithmetic.", "Lean man, threadbare three-piece suit, pocket watch, suspicious eyebrows.", ("precise", "cynical", "lonely"), "bank"),
    Persona("posy", "Posy", "Relentlessly cheerful busker who remembers every face and every name.", "Young person, patchwork coat, fingerless gloves, a battered ukulele.", ("extroverted", "scattered", "kind"), "plaza"),
    Persona("hale", "Hale", "Night-shift type who only feels awake after dark and collects rumors.", "Pale figure, long dark coat, headphones around the neck, restless.", ("aloof", "curious", "wry"), "cafe"),
    Persona("verena", "Verena", "Retired schoolteacher who corrects grammar mid-conversation and means well.", "Stately older woman, cardigan with brooches, reading chain on her glasses.", ("exacting", "maternal", "proud"), "library"),
    Persona("cleo", "Cleo", "Tinkerer who is always mid-project and explains things you didn't ask about.", "Compact person, tool belt, goggles pushed up on forehead, grease on cheek.", ("inventive", "distractible", "earnest"), "activity_center"),
    Persona("bram", "Bram", "Quiet bench-sitter who watches the town and occasionally drops a perfect one-liner.", "Heavyset man, flat cap, newspaper under arm, unhurried.", ("taciturn", "shrewd", "patient"), "plaza"),
)
```

- [ ] **Step 4: Run the test; expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add apps/backend/worldgm/residents/__init__.py apps/backend/worldgm/residents/personas.py apps/backend/tests/unit/worldgm/residents/test_personas.py
git commit -m "feat(worldgm): resident persona pool"
```

### Task 3: Pre-generate + cache resident sprites (one-time seed script)

**Files:**
- Create: `scripts/seed-resident-sprites.py`

- [ ] **Step 1: Implement the seed script** (no unit test — it's a one-shot operational script; it calls the real PixelLab pipeline and uploads to S3).

```python
"""One-time: generate a sprite per resident persona via the existing PixelLab
pipeline and cache it at sprites/resident-<name>/walk.png in the sprite bucket.
Idempotent: skips personas whose sprite already exists in S3.

Usage: cd apps/backend && uv run python ../../scripts/seed-resident-sprites.py [--env dev|prod]
"""
import argparse, asyncio, boto3
from worldgm.residents.personas import RESIDENT_PERSONAS
from core.services.pixellab_service import PixelLabService
from core.services.sprite_storage import extract_walk_spritesheet
from core import config

async def main(env: str) -> None:
    s3 = boto3.client("s3")
    bucket = config.SPRITE_S3_BUCKET
    pxl = PixelLabService(api_key=config.PIXELLAB_API_KEY)
    for p in RESIDENT_PERSONAS:
        key = f"sprites/resident-{p.name}/walk.png"
        try:
            s3.head_object(Bucket=bucket, Key=key)
            print(f"skip {p.name} (exists)")
            continue
        except s3.exceptions.ClientError:
            pass
        print(f"generating {p.name} ...")
        char_id, job_id = await pxl.create_character(p.appearance)
        await pxl.wait_for_jobs([job_id])
        await pxl.animate_character(char_id, "walk")
        zip_bytes = await pxl.download_character_zip(char_id)
        png = extract_walk_spritesheet(zip_bytes)
        s3.put_object(Bucket=bucket, Key=key, Body=png, ContentType="image/png")
        print(f"  uploaded {key}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="dev")
    args = ap.parse_args()
    asyncio.run(main(args.env))
```

(Verify the exact `PixelLabService` / `sprite_storage` method names against the current source; adjust to match. This mirrors the agent sprite pipeline described in CLAUDE.md.)

- [ ] **Step 2: Manual run note** — this is run once per env by an operator with PixelLab creds + S3 access; not part of CI. Document in the PR that residents render as shadow placeholders until it's run.

- [ ] **Step 3: Commit**

```bash
git add scripts/seed-resident-sprites.py
git commit -m "feat(worldgm): one-time resident sprite seed script"
```

### Task 4: Scripted behavior FSM

**Files:**
- Create: `apps/backend/worldgm/residents/behavior.py`
- Test: `apps/backend/tests/unit/worldgm/residents/test_behavior.py`

- [ ] **Step 1: Write the failing test**

```python
from worldgm.residents.behavior import next_action, ResidentRuntime

def test_idle_resident_picks_a_move_toward_hangout_or_neighbor():
    rt = ResidentRuntime(name="margot", location="plaza", state="idle", idle_ticks=0, hangout="library")
    act = next_action(rt, rng_value=0.0)  # rng injected for determinism
    assert act["type"] == "move"
    assert act["destination"] in {"library", "plaza", "cafe", "activity_center", "bank", "residence"}

def test_resident_idles_for_a_few_ticks_after_arriving():
    rt = ResidentRuntime(name="margot", location="library", state="arrived", idle_ticks=0, hangout="library")
    act = next_action(rt, rng_value=0.9)
    assert act["type"] in {"activity", "idle"}

def test_moving_resident_emits_no_action_until_arrival():
    rt = ResidentRuntime(name="margot", location="plaza", state="moving", idle_ticks=0, hangout="library")
    act = next_action(rt, rng_value=0.5)
    assert act["type"] == "noop"
```

- [ ] **Step 2: Run; expect FAIL.**

- [ ] **Step 3: Implement `behavior.py`** — a pure function `next_action(runtime, rng_value) -> dict` (no LLM, no I/O; rng injected so it's testable). FSM: `idle` → `move` (weighted to hangout, else a random location); `moving` → `noop`; `arrived` → `activity` or short `idle`. Keep it ~40 lines. The worldgm step layer (Task 6) maps the returned action dict to `town_agent_act` calls and updates `ResidentRuntime`.

```python
from __future__ import annotations
from dataclasses import dataclass

LOCATIONS = ("plaza", "library", "cafe", "activity_center", "bank", "residence")
_IDLE_TICKS_AFTER_ARRIVE = 3

@dataclass
class ResidentRuntime:
    name: str
    location: str
    state: str          # "idle" | "moving" | "arrived"
    idle_ticks: int
    hangout: str

def next_action(rt: ResidentRuntime, rng_value: float) -> dict:
    if rt.state == "moving":
        return {"type": "noop"}
    if rt.state == "arrived" and rt.idle_ticks < _IDLE_TICKS_AFTER_ARRIVE:
        # mostly idle; occasionally do a location activity
        return {"type": "activity"} if rng_value > 0.7 else {"type": "idle"}
    # idle and rested → move: 60% to hangout, else a random location
    if rng_value < 0.6:
        dest = rt.hangout
    else:
        idx = int(rng_value * len(LOCATIONS)) % len(LOCATIONS)
        dest = LOCATIONS[idx]
    if dest == rt.location:
        return {"type": "idle"}
    return {"type": "move", "destination": dest}
```

- [ ] **Step 4: Run; expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add apps/backend/worldgm/residents/behavior.py apps/backend/tests/unit/worldgm/residents/test_behavior.py
git commit -m "feat(worldgm): scripted resident behavior FSM"
```

### Task 5: Server-side connect/despawn helper

**Files:**
- Create: `apps/backend/worldgm/residents/lifecycle.py`
- Test: `apps/backend/tests/unit/worldgm/residents/test_lifecycle.py`

- [ ] **Step 1: Write the failing test** (mock `game_server_client` + `town_service`)

```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from worldgm.residents import lifecycle

@pytest.mark.asyncio
async def test_spawn_resident_creates_row_then_connects_game_server():
    ts = MagicMock()
    ts.create_resident.return_value = {"id": "r1", "agentName": "tobias", "currentLocation": "plaza"}
    gs = AsyncMock()
    persona = MagicMock(name="tobias", display_name="Tobias", personality="p", appearance="a", traits=("gruff",))
    persona.name = "tobias"
    await lifecycle.spawn_resident(persona, town_service=ts, game_server=gs)
    ts.create_resident.assert_called_once()
    gs.connect_agent.assert_awaited_once()

@pytest.mark.asyncio
async def test_despawn_resident_disconnects_and_deactivates():
    ts = MagicMock(); gs = AsyncMock()
    await lifecycle.despawn_resident("r1", town_service=ts, game_server=gs)
    gs.disconnect_agent.assert_awaited_once_with("r1")
    ts.update_agent_state.assert_called_once()
```

- [ ] **Step 2: Run; expect FAIL.**

- [ ] **Step 3: Implement `lifecycle.py`** — `spawn_resident(persona, town_service, game_server)`: call `town_service.create_resident(...)`, then `await game_server.connect_agent(agent_id, ...)` with the resident's spawn location + cached sprite URL (`sprites/resident-<name>/walk.png` via the CDN base). `despawn_resident(agent_id, town_service, game_server)`: `await game_server.disconnect_agent(agent_id)` then `town_service.update_agent_state(agent_id, is_active=False)`. Match `connect_agent`'s real signature (check `game_server_client.py:49`).

- [ ] **Step 4: Run; expect PASS.**

- [ ] **Step 5: Commit.**

### Task 6: Phase-1 wiring — always-on residents in the tick

**Files:**
- Modify: `apps/backend/worldgm/__main__.py`
- Create: `apps/backend/worldgm/residents/floor.py` (Phase-1 version: just "ensure all-personas-up-to-N are spawned", dynamic logic in Phase 2)
- Test: `apps/backend/tests/unit/worldgm/residents/test_floor.py`

- [ ] **Step 1: Write the failing test for the Phase-1 static reconcile**

```python
from worldgm.residents.floor import resident_target_count

def test_phase1_static_target_is_constant_floor():
    # Phase 1: ignore real-agent count, always target the floor.
    assert resident_target_count(live_real_agents=0, floor=4, dynamic=False) == 4
    assert resident_target_count(live_real_agents=10, floor=4, dynamic=False) == 4
```

- [ ] **Step 2: Run; expect FAIL.**

- [ ] **Step 3: Implement `resident_target_count`** (the dynamic branch lands in Phase 2):

```python
def resident_target_count(live_real_agents: int, floor: int, dynamic: bool = True) -> int:
    if not dynamic:
        return floor
    return max(0, min(floor, floor - live_real_agents))
```

- [ ] **Step 4: Run; expect PASS.**

- [ ] **Step 5: Wire reconcile + step into `_tick`** in `__main__.py`. After `sync_locations`, add (behind a `RESIDENTS_ENABLED` env flag, default off so deploy is safe until Phase 1 is validated):

```python
if state.residents_enabled:
    await reconcile_residents(state, live_real_agents=_count_real(state.last_known_locations))
    await step_residents(state)  # maps next_action() → town_agent_act per active resident
```

Define `reconcile_residents` and `step_residents` in `worldgm/residents/floor.py` (orchestration) using `spawn_resident`/`despawn_resident` and `next_action`. `_count_real` counts non-resident agents in the synced locations.

- [ ] **Step 6: Run full worldgm unit suite; expect PASS.**
Run: `cd apps/backend && uv run pytest tests/unit/worldgm/ -v`

- [ ] **Step 7: Commit.**

### Task 7: Frontend "resident" label

**Files:**
- Modify: `apps/frontend/src/components/ApartmentCard.tsx` (+ the town nameplate component)
- Test: `apps/frontend/src/components/ApartmentCard.test.tsx`

- [ ] **Step 1: Write the failing test** — render an `ApartmentCard` for an agent with `isResident: true`; assert a "resident" badge renders; for a normal agent it does not.
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Add the badge** — read `isResident` off the agent payload (ensure the backend `/apartment` + `/descriptions` responses include `isResident`); render a small muted "resident" chip.
- [ ] **Step 4: Run; expect PASS.** Also `pnpm typecheck`.
- [ ] **Step 5: Commit.**

---

## Phase 2 — Dynamic floor (recede as reals arrive)

### Task 8: Dynamic target + hysteresis

**Files:**
- Modify: `apps/backend/worldgm/residents/floor.py`
- Test: `apps/backend/tests/unit/worldgm/residents/test_floor.py`

- [ ] **Step 1: Write failing tests**

```python
from worldgm.residents.floor import resident_target_count, should_despawn

def test_dynamic_target_recedes_as_reals_arrive():
    assert resident_target_count(0, floor=4) == 4
    assert resident_target_count(1, floor=4) == 3
    assert resident_target_count(4, floor=4) == 0
    assert resident_target_count(9, floor=4) == 0

def test_despawn_requires_grace_period():
    # above-floor for fewer than grace ticks → don't despawn yet
    assert should_despawn(above_floor_ticks=2, grace=4) is False
    assert should_despawn(above_floor_ticks=4, grace=4) is True
```

- [ ] **Step 2: Run; expect FAIL** (`should_despawn` missing; dynamic already implemented in Task 6).
- [ ] **Step 3: Implement `should_despawn`** + thread an `above_floor_ticks` counter through the reconcile orchestration so despawns only fire after `DESPAWN_GRACE_TICKS` (default 4 ≈ 2 min) above floor — prevents flapping as real agents connect/disconnect.
- [ ] **Step 4: Run; expect PASS.**
- [ ] **Step 5: Flip `reconcile_residents` to dynamic** (`dynamic=True`) and use `should_despawn` before despawning surplus. Spawn-up is immediate (cold start matters); despawn is graced.
- [ ] **Step 6: Commit.**

### Task 9: Integration test — floor behavior end-to-end (mocked game server)

**Files:**
- Test: `apps/backend/tests/integration/test_resident_floor.py`

- [ ] **Step 1: Write integration test** — with a mocked `game_server_client` and a real (moto) DDB: empty town → reconcile spawns 4 residents; inject 2 real active agents → after grace ticks, residents recede to 2; inject 4 reals → recede to 0. Assert `isActive` transitions and no `system:resident` rows count in a real user's quota query.
- [ ] **Step 2: Run; expect FAIL then implement any gaps; PASS.**
- [ ] **Step 3: Commit.**

---

## Phase 3 — Conversation LLM

### Task 10: Bedrock-backed resident replies

**Files:**
- Create: `apps/backend/worldgm/residents/conversation.py`
- Test: `apps/backend/tests/unit/worldgm/residents/test_conversation.py`

- [ ] **Step 1: Write failing test** (mock `BedrockLanguageModel`)

```python
import pytest
from unittest.mock import AsyncMock
from worldgm.residents.conversation import generate_reply

@pytest.mark.asyncio
async def test_generate_reply_uses_persona_and_recent_turns():
    model = AsyncMock()
    model.generate.return_value = "The lighthouse keeps better hours than you, friend."
    reply = await generate_reply(
        persona_name="tobias",
        recent_turns=[{"speaker": "nyx", "text": "what do you do around here?"}],
        model=model,
    )
    assert reply
    model.generate.assert_awaited_once()
    # prompt must carry the persona + the incoming line
    prompt = model.generate.call_args[0][0]
    assert "Tobias" in prompt or "tobias" in prompt
    assert "what do you do around here" in prompt
```

- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement `generate_reply`** — build a compact prompt from the persona (look up in `RESIDENT_PERSONAS`) + recent turns + a hard instruction to reply in-character in 1-2 sentences; call the injected Bedrock model; on exception, return a short canned persona fallback line (so the real agent isn't left hanging).
- [ ] **Step 4: Run; expect PASS.**
- [ ] **Step 5: Commit.**

### Task 11: Route resident conversation turns through the handler

**Files:**
- Modify: `apps/backend/core/services/conversation_service.py`
- Test: `apps/backend/tests/unit/services/test_conversation_service.py`

- [ ] **Step 1: Write failing test** — when a real agent `say`s in a conversation whose other participant is a resident (`isResident=true`), the service generates a resident reply (mock `generate_reply`) and posts it as the resident's turn; when both participants are real, it does NOT auto-generate.
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement the hook** — in the say/chat path, after recording a real agent's turn, if a participant is a resident and it's the resident's turn, call `generate_reply(...)` and dispatch it as the resident's `say` (reusing the existing turn-recording + broadcast path). Guard against resident↔resident loops (residents don't auto-reply to other residents). Respect the existing 180s inactivity auto-end.
- [ ] **Step 4: Run; expect PASS.**
- [ ] **Step 5: Commit.**

### Task 12: Bedrock IAM for resident conversation model

**Files:**
- Modify: `apps/infra/lib/stacks/worldgm-stack.ts` (if a different model than nova-lite/micro is used) OR `compute-stack.ts` (if replies are generated in the backend, not worldgm)

- [ ] **Step 1: Decide which service generates replies.** Conversations are event-driven on the **backend** (the `/ws-message` → `town_agent_act` → conversation_service path), not the worldgm tick. So the **backend (compute-stack)** task role needs `bedrock:InvokeModel`/`Converse` on the resident reply model. Check whether compute-stack already grants Bedrock; if not, add it scoped to the resident model ARN (e.g. `amazon.nova-lite-v1:0`).
- [ ] **Step 2: `cdk synth`**; verify the backend role gains `bedrock:InvokeModel` on the model ARN.
- [ ] **Step 3: Commit** (this is the infra change that lets Phase 3 actually work in deployed envs).

---

## Phase validation + rollout

### Task 13: Soak + enablement

- [ ] **Step 1:** Deploy with `RESIDENTS_ENABLED=false`; confirm no behavior change (safety: residents off by default until validated).
- [ ] **Step 2:** Run `scripts/seed-resident-sprites.py --env dev` once (operator with creds).
- [ ] **Step 3:** Flip `RESIDENTS_ENABLED=true` on dev; watch worldgm logs: residents spawn to floor, appear on `dev.goosetown.isol8.co/town`, move around, render with their sprites.
- [ ] **Step 4:** Join a real agent; confirm it can walk up to a resident and have an LLM-backed conversation; confirm residents recede after the grace period as more real agents join.
- [ ] **Step 5:** Reuse the 5-agent soak harness with residents enabled; check no quota leakage, no PixelLab credit spend on resident cycling, no stuck conversations.
- [ ] **Step 6:** Record findings in `docs/superpowers/test-runs/2026-05-29-resident-soak.md`. Then enable on prod.

---

## Self-review

**Spec coverage:** dynamic floor → Tasks 6/8/9; hybrid behavior (scripted move + LLM conversation) → Tasks 4/10/11; persona pool → Task 2; pre-generated sprites → Task 3; server-side connection → Task 5; data model `isResident`/system-user/no-quota → Task 1; honesty label → Task 7; phase-out (recede + `RESIDENTS_ENABLED`) → Tasks 8/13; cost (scripted $0, conversation-only LLM) → Tasks 4/10; testing (unit/integration/soak) → throughout + Tasks 9/13. Bedrock IAM for the conversation model → Task 12 (a real seam the spec implied but didn't call out — added). All spec sections covered.

**Placeholder scan:** No TBD/TODO. Code shown for each implementing step. Two explicit "verify the exact method name against current source" notes (sprite pipeline, connect_agent signature) — these are *verification* instructions with the expected shape given, not placeholders.

**Type consistency:** `Persona` fields (name/display_name/personality/appearance/traits/hangout) consistent across Tasks 2/4/5/10. `ResidentRuntime` fields consistent Tasks 4/6. `create_resident` signature consistent Tasks 1/5. `resident_target_count`/`should_despawn` consistent Tasks 6/8/9. `RESIDENT_USER_ID = "system:resident"` consistent Tasks 1/9.

**Staging note:** Phases 1→2→3 each leave the system working (Phase 1: residents exist + move; Phase 2: they recede; Phase 3: they converse). `RESIDENTS_ENABLED` default-off makes every deploy safe until a phase is validated.
