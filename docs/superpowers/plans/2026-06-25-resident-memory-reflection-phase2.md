# Resident Memory & Reflection — Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Give residents a persistent, subject-indexed memory stream + reflection so conversations leave residue and the cast differentiates over time — flag-gated off in prod until validated.

**Architecture:** All logic in `apps/backend/worldgm/residents/` + one config + one CDK table. No embeddings (memories carry a `subject`; retrieval = recency·importance over `about(subject)`). No `conversation_service`/CLI-daemon changes. Conversation-end is detected in the resident stepper by caching the transcript and noticing `active_conversation` go set→None. Everything gated by `RESIDENT_MEMORY_ENABLED` (default false).

**Tech Stack:** Python 3.12, pytest, boto3/DynamoDB, AWS CDK (TypeScript). Backend tests: `cd apps/backend && uv run pytest`. Infra: `cd apps/infra && npx jest`.

## Global Constraints

- Branch base: `feat/resident-cast-bibles` (now contains Phase 1). Do NOT rebase onto main.
- `RESIDENT_MEMORY_ENABLED` default **false**; when false, behavior is exactly Phase 1 (no memory reads/writes/reflection).
- No embeddings, no `conversation_service.py` changes, no `apps/goosetown-cli` changes.
- All memory/reflection is **best-effort**: any model or DynamoDB error logs a warning and degrades to no-op — never breaks a brain tick or a conversation.
- All resident LLM calls use Nova Lite (`model_lite`), never Nova Pro.
- Memories reuse the existing `relationships` table's `stanceAtoB`/`stanceBtoA` fields for the "what changed between us" note via `town_service.upsert_relationship_stance`; affinity via `town_service.update_relationship`.
- Commit only each task's files. The repo has unrelated uncommitted working-tree files (session-logs, video-marketing, skills-lock) — never stage them.
- Run `uv run ruff check <changed dirs>` before each backend commit.

---

### Task 1: Config flags

**Files:**
- Modify: `apps/backend/core/config.py`
- Test: `apps/backend/tests/unit/test_config.py` (create if absent)

**Interfaces — Produces:** `settings.RESIDENT_MEMORY_ENABLED: bool`, `settings.RESIDENT_MEMORIES_TABLE: str`.

- [ ] **Step 1: Write the failing test**
```python
def test_resident_memory_flags_default_off(monkeypatch):
    monkeypatch.delenv("RESIDENT_MEMORY_ENABLED", raising=False)
    monkeypatch.delenv("RESIDENT_MEMORIES_TABLE", raising=False)
    import importlib; from core import config
    importlib.reload(config)
    assert config.settings.RESIDENT_MEMORY_ENABLED is False
    assert config.settings.RESIDENT_MEMORIES_TABLE == ""

def test_resident_memory_flags_enable(monkeypatch):
    monkeypatch.setenv("RESIDENT_MEMORY_ENABLED", "true")
    monkeypatch.setenv("RESIDENT_MEMORIES_TABLE", "goosetown-dev-memories")
    import importlib; from core import config
    importlib.reload(config)
    assert config.settings.RESIDENT_MEMORY_ENABLED is True
    assert config.settings.RESIDENT_MEMORIES_TABLE == "goosetown-dev-memories"
```
- [ ] **Step 2: Run → fail.** `cd apps/backend && uv run pytest tests/unit/test_config.py -v` → FAIL (AttributeError).
- [ ] **Step 3: Implement** — in `class Settings`, alongside `ACTIVITY_ANIMATION_GENERATION`:
```python
    # Phase 2: persistent resident memory + reflection. Default false; prod stays
    # off until a dev soak validates Nova-Lite reflection quality.
    RESIDENT_MEMORY_ENABLED: bool = os.environ.get("RESIDENT_MEMORY_ENABLED", "false").lower() == "true"
    RESIDENT_MEMORIES_TABLE: str = os.environ.get("RESIDENT_MEMORIES_TABLE", "")
```
- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit** — `git add apps/backend/core/config.py apps/backend/tests/unit/test_config.py` → `feat(worldgm): RESIDENT_MEMORY_ENABLED + RESIDENT_MEMORIES_TABLE config flags` + Co-Authored-By trailer.

---

### Task 2: `MemoryStore` data layer

**Files:**
- Create: `apps/backend/worldgm/residents/memory_store.py`
- Test: `apps/backend/tests/unit/worldgm/residents/test_memory_store.py`

**Interfaces — Produces:**
- `class MemoryStore` constructed with an injected boto3 Table-like object: `MemoryStore(table)`.
  - `put(self, agent_id, subject, kind, text, importance, created_at, source_ids=()) -> dict`
  - `recent(self, agent_id, limit=100) -> list[dict]`
  - `about(self, agent_id, subject, limit=20) -> list[dict]`
  - `mark_reflected(self, agent_id, created_ats) -> None`
- module fn `retrieval_score(mem: dict, now_iso: str, half_life_hours: float = 24.0) -> float`
- factory `memory_store_from_env() -> "MemoryStore | None"` (None when `settings.RESIDENT_MEMORIES_TABLE` is empty).

**Consumes:** Task 1 settings.

- [ ] **Step 1: Write the failing tests** (use a FakeTable stub — no real AWS):
```python
import math, uuid
from worldgm.residents.memory_store import MemoryStore, retrieval_score

class FakeTable:
    def __init__(self): self.items = []; self.last_query = None
    def put_item(self, Item): self.items.append(Item)
    def query(self, **kw):
        self.last_query = kw
        # crude emulation: filter by KeyConditionExpression captured via ExpressionAttributeValues
        vals = kw.get("ExpressionAttributeValues", {})
        out = [i for i in self.items if i["agentId"] == vals.get(":a")]
        if ":s" in vals:
            out = [i for i in out if i.get("subject") == vals[":s"]]
        out.sort(key=lambda i: i["createdAt"], reverse=not kw.get("ScanIndexForward", True))
        return {"Items": out[: kw.get("Limit", len(out))]}
    def update_item(self, **kw): 
        key = kw["Key"]
        for i in self.items:
            if i["agentId"] == key["agentId"] and i["createdAt"] == key["createdAt"]:
                i["reflected"] = True

def test_put_then_about_and_recent():
    t = FakeTable(); s = MemoryStore(t)
    s.put("me", subject="LeBron", kind="observation", text="he's all hustle", importance=7, created_at="2026-06-25T10:00:00Z")
    s.put("me", subject="Dwight", kind="observation", text="beets again", importance=4, created_at="2026-06-25T11:00:00Z")
    assert len(t.items) == 2
    about = s.about("me", "LeBron")
    assert len(about) == 1 and about[0]["text"] == "he's all hustle"
    recent = s.recent("me", limit=1)
    assert recent[0]["subject"] == "Dwight"  # most recent first

def test_retrieval_score_recency_and_importance():
    fresh = {"createdAt": "2026-06-25T12:00:00Z", "importance": 5}
    old = {"createdAt": "2026-06-24T12:00:00Z", "importance": 5}
    now = "2026-06-25T12:00:00Z"
    assert retrieval_score(fresh, now) > retrieval_score(old, now)
    hi = {"createdAt": now, "importance": 10}; lo = {"createdAt": now, "importance": 1}
    assert retrieval_score(hi, now) > retrieval_score(lo, now)

def test_mark_reflected():
    t = FakeTable(); s = MemoryStore(t)
    s.put("me", "self", "observation", "x", 5, "2026-06-25T10:00:00Z")
    s.mark_reflected("me", ["2026-06-25T10:00:00Z"])
    assert t.items[0]["reflected"] is True
```
- [ ] **Step 2: Run → fail** (module missing).
- [ ] **Step 3: Implement `memory_store.py`:**
```python
"""Subject-indexed resident memory store (DynamoDB). No embeddings — retrieval is
recency·importance over a subject GSI. All methods best-effort at the call site."""
from __future__ import annotations
import datetime as _dt
import uuid
from decimal import Decimal

def _parse_iso(s: str) -> _dt.datetime:
    return _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))

def retrieval_score(mem: dict, now_iso: str, half_life_hours: float = 24.0) -> float:
    """Normalized recency (exponential decay, given half-life) + importance/10."""
    try:
        age_h = (_parse_iso(now_iso) - _parse_iso(str(mem["createdAt"]))).total_seconds() / 3600.0
    except Exception:
        age_h = 0.0
    recency = 0.5 ** (max(age_h, 0.0) / half_life_hours)
    importance = float(mem.get("importance", 1)) / 10.0
    return recency + importance

class MemoryStore:
    def __init__(self, table):
        self._t = table

    def put(self, agent_id, subject, kind, text, importance, created_at, source_ids=()):
        item = {
            "agentId": agent_id, "createdAt": created_at, "memoryId": str(uuid.uuid4()),
            "subject": subject, "kind": kind, "text": text,
            "importance": int(importance), "reflected": False,
            "sourceIds": list(source_ids),
        }
        self._t.put_item(Item=item)
        return item

    def recent(self, agent_id, limit=100):
        resp = self._t.query(
            KeyConditionExpression="agentId = :a",
            ExpressionAttributeValues={":a": agent_id},
            ScanIndexForward=False, Limit=limit,
        )
        return resp.get("Items", [])

    def about(self, agent_id, subject, limit=20):
        resp = self._t.query(
            IndexName="agentId-subject-index",
            KeyConditionExpression="agentId = :a AND subject = :s",
            ExpressionAttributeValues={":a": agent_id, ":s": subject},
            Limit=limit,
        )
        return resp.get("Items", [])

    def mark_reflected(self, agent_id, created_ats):
        for ca in created_ats:
            self._t.update_item(
                Key={"agentId": agent_id, "createdAt": ca},
                UpdateExpression="SET reflected = :t",
                ExpressionAttributeValues={":t": True},
            )

def memory_store_from_env():
    from core.config import settings
    name = settings.RESIDENT_MEMORIES_TABLE
    if not name:
        return None
    import boto3
    return MemoryStore(boto3.resource("dynamodb").Table(name))
```
Note: real DynamoDB needs `Decimal` for numbers; the FakeTable uses ints. Keep `importance` an `int` in items — boto3 resource accepts int for a Number attribute (matches `town_service` convention). Do not wrap in Decimal.
- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit** — `git add apps/backend/worldgm/residents/memory_store.py apps/backend/tests/unit/worldgm/residents/test_memory_store.py` → `feat(worldgm): MemoryStore — subject-indexed recency·importance memory layer` + trailer.

---

### Task 3: Reflection module (residue, recall, insight synthesis)

**Files:**
- Create: `apps/backend/worldgm/residents/reflection.py`
- Test: `apps/backend/tests/unit/worldgm/residents/test_reflection.py`

**Interfaces — Produces:**
- `recall_about(store, my_id, other_name, now_iso, k=3) -> str` — short rendered memory string ("" if none).
- `async reflect_on_conversation(model, store, town_service, my_id, other_id, other_name, transcript, now_iso) -> bool` — writes one observation memory + relationship affinity/stance; returns True if a memory was written.
- `async synthesize_reflection(model, store, my_id, now_iso, min_new=6) -> int` — returns number of insights stored.
- helper `_extract_json(raw: str) -> dict | None`.

**Consumes:** Task 2 `MemoryStore`/`retrieval_score`; `town_service.update_relationship` + `upsert_relationship_stance`; an injected `model` with `.sample_text(prompt, max_tokens=, temperature=)`.

- [ ] **Step 1: Write failing tests** (fake model + fake store + fake town_service):
```python
import asyncio, json
from worldgm.residents import reflection
from worldgm.residents.memory_store import MemoryStore

class FakeModel:
    def __init__(self, reply): self.reply = reply; self.calls = []
    def sample_text(self, prompt, **kw): self.calls.append(prompt); return self.reply

class FakeTownService:
    def __init__(self): self.affinity = []; self.stance = []
    def update_relationship(self, a, b, affinity_delta=0, new_type=None): self.affinity.append((a, b, affinity_delta))
    def upsert_relationship_stance(self, a, b, stance_a_to_b=None, stance_b_to_a=None, stance_updated_at=None): self.stance.append((a, b, stance_a_to_b))

class FakeTable:  # minimal, see test_memory_store for the fuller version
    def __init__(self): self.items=[]
    def put_item(self, Item): self.items.append(Item)
    def query(self, **kw):
        v=kw.get("ExpressionAttributeValues",{}); out=[i for i in self.items if i["agentId"]==v.get(":a")]
        if ":s" in v: out=[i for i in out if i.get("subject")==v[":s"]]
        out.sort(key=lambda i:i["createdAt"], reverse=not kw.get("ScanIndexForward",True)); return {"Items":out[:kw.get("Limit",len(out))]}
    def update_item(self, **kw):
        for i in self.items:
            if i["agentId"]==kw["Key"]["agentId"] and i["createdAt"]==kw["Key"]["createdAt"]: i["reflected"]=True

def test_reflect_on_conversation_writes_memory_and_relationship():
    store = MemoryStore(FakeTable()); ts = FakeTownService()
    model = FakeModel(json.dumps({"summary":"LeBron only talks legacy","importance":6,"affinity_delta":-3,"note":"exhausting"}))
    wrote = asyncio.run(reflection.reflect_on_conversation(model, store, ts, "me","lebron-id","LeBron",
        transcript=[{"speaker":"LeBron","text":"legacy"},{"speaker":"Me","text":"ok"}], now_iso="2026-06-25T10:00:00Z"))
    assert wrote is True
    mems = store.about("me","LeBron"); assert mems and mems[0]["text"]=="LeBron only talks legacy" and mems[0]["importance"]==6
    assert ts.affinity == [("me","lebron-id",-3)]
    assert ts.stance and ts.stance[0][2]=="exhausting"

def test_reflect_on_conversation_tolerates_bad_json():
    store = MemoryStore(FakeTable()); ts = FakeTownService()
    wrote = asyncio.run(reflection.reflect_on_conversation(FakeModel("not json"), store, ts, "me","x","X",
        transcript=[{"speaker":"X","text":"hi"}], now_iso="2026-06-25T10:00:00Z"))
    assert wrote is False and store.about("me","X") == [] and ts.affinity == []

def test_recall_about_renders_top_k():
    store = MemoryStore(FakeTable())
    store.put("me","X","observation","first thing",3,"2026-06-25T09:00:00Z")
    store.put("me","X","observation","big thing",9,"2026-06-25T08:00:00Z")
    out = reflection.recall_about(store, "me", "X", "2026-06-25T10:00:00Z", k=2)
    assert "big thing" in out and "first thing" in out
    assert reflection.recall_about(store, "me", "NoOne", "2026-06-25T10:00:00Z") == ""

def test_synthesize_reflection_stores_insights_and_marks_sources():
    store = MemoryStore(FakeTable())
    for i in range(6): store.put("me","X","observation",f"obs{i}",5,f"2026-06-25T0{i}:00:00Z")
    model = FakeModel(json.dumps({"insights":[{"insight":"X is exhausting","importance":7}]}))
    n = asyncio.run(reflection.synthesize_reflection(model, store, "me", "2026-06-25T10:00:00Z", min_new=6))
    assert n == 1
    refl = [m for m in store.recent("me") if m["kind"]=="reflection"]
    assert refl and refl[0]["text"]=="X is exhausting" and refl[0]["importance"]>=6
    assert all(m["reflected"] for m in store.recent("me") if m["kind"]=="observation")

def test_synthesize_reflection_noop_below_threshold():
    store = MemoryStore(FakeTable()); store.put("me","X","observation","only one",5,"2026-06-25T01:00:00Z")
    assert asyncio.run(reflection.synthesize_reflection(FakeModel("{}"), store, "me","2026-06-25T10:00:00Z", min_new=6)) == 0
```
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement `reflection.py`** — model calls run in a thread (`asyncio.to_thread`) like `agentic_brain.decide`. Use a tolerant `_extract_json` (first `{` … last `}`). `recall_about` ranks via `retrieval_score`. `reflect_on_conversation` clamps `affinity_delta` to [-10,10] and `importance` to [1,10]; writes via `store.put` then `town_service.update_relationship` + `upsert_relationship_stance`; returns False on parse failure or empty summary. `synthesize_reflection` filters `recent()` for `reflected==False` observations, returns 0 if fewer than `min_new`, else one model call → store each insight as `kind="reflection"`, `importance=max(6, given)`, `source_ids=[m["memoryId"]...]`, then `mark_reflected` the source `createdAt`s. All model calls wrapped in try/except → degrade to no-op. (Full code is straightforward from the tests; build prompts that hand the model the transcript / recent memories and ask for the exact JSON shape the tests assert.)
- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit** — `git add apps/backend/worldgm/residents/reflection.py apps/backend/tests/unit/worldgm/residents/test_reflection.py` → `feat(worldgm): reflection — conversation residue, recall, insight synthesis (Nova-Lite, best-effort)` + trailer.

---

### Task 4: Stepper integration (conversation-end residue, retrieval injection, periodic reflection)

**Files:**
- Modify: `apps/backend/worldgm/residents/behavior.py` (ResidentRuntime fields) — confirm where `ResidentRuntime` is defined; if it's a dataclass there, add fields.
- Modify: `apps/backend/worldgm/residents/brain.py` (`step_resident_brain`: cache transcript, detect end→residue, inject recall)
- Modify: `apps/backend/worldgm/__main__.py` (`_tick_one_resident`: periodic `synthesize_reflection`)
- Test: `apps/backend/tests/unit/worldgm/residents/test_brain_memory.py` (new)

**Interfaces — Consumes:** Tasks 1-3. **Produces:** wired live behavior, all gated by `settings.RESIDENT_MEMORY_ENABLED`.

- [ ] **Step 1: Write failing tests** — drive `step_resident_brain` with a fake daemon whose `state` transitions from an active conversation to none across two calls; inject a fake model/store/town_service (monkeypatch `memory_store_from_env` to return the fake store, and patch `settings.RESIDENT_MEMORY_ENABLED=True`). Assert: (a) while in conversation, no residue written; (b) on the tick where `active_conversation` becomes None, `reflect_on_conversation` is invoked once (a memory appears for the other participant); (c) the same ended conv is not reflected twice; (d) with the flag False, nothing is written. Mirror the existing `test_brain_agentic.py` fake-daemon setup. Also assert `recall_about` output is injected: when the resident has a nearby/addressee with prior memories, `step_resident_brain` populates `parsed_status["remembered"]` (assert the decision prompt — via the model's captured prompt — contains the remembered text, OR assert the helper was called).
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement:**
  - `ResidentRuntime`: add `last_conv_id: str|None = None`, `last_conv_turns: list = field(default_factory=list)`, `reflected_conv_ids: set = field(default_factory=set)`, `memories_since_reflection: int = 0`.
  - `step_resident_brain` (agentic path only, and only when `settings.RESIDENT_MEMORY_ENABLED` and a `store = memory_store_from_env()` is available — pass `store`/`town_service`/`model` through, or fetch inside; keep the FSM-fallback path untouched):
    - On each tick: if `active_conversation`, cache `runtime.last_conv_id`/`last_conv_turns` from it.
    - If `active_conversation` is None and `runtime.last_conv_id` set and not in `reflected_conv_ids`: resolve the other participant (from cached turns / participants), `await reflect_on_conversation(...)`; on success `runtime.memories_since_reflection += 1`; add to `reflected_conv_ids`; clear the cache.
    - Before building context (when there's an addressee/nearby): `parsed_status["remembered"] = recall_about(store, my_id, other_name, now_iso)`.
  - `_tick_one_resident` (`__main__.py`): after the brain step, if flag on and not in conversation and `runtime.memories_since_reflection >= _REFLECT_EVERY_N` (module const, default 6): `await synthesize_reflection(model_lite, store, agent_id, now_iso, min_new=_REFLECT_EVERY_N)`; reset `runtime.memories_since_reflection = 0` on completion. Best-effort try/except.
  - Thread `now_iso` from a `datetime.now(UTC).isoformat()` helper (mirror brain.py's `_now_utc_hour`).
- [ ] **Step 4: Run → pass** (`uv run pytest tests/unit/worldgm/ -q`).
- [ ] **Step 5: Commit** — scoped `git add` of the 3 files + test → `feat(worldgm): wire memory residue + recall + periodic reflection into the resident stepper (flag-gated)` + trailer.

---

### Task 5: Inject recalled memory into the dialogue prompt

**Files:**
- Modify: `apps/backend/worldgm/residents/agentic_brain.py` (`DecisionContext.remembered`; `build_decision_context` copy from `parsed_status`; `build_dialogue_prompt` render)
- Test: `apps/backend/tests/unit/worldgm/residents/test_agentic_brain.py`

**Interfaces — Produces:** `DecisionContext.remembered: str = ""`, rendered in the dialogue prompt.

- [ ] **Step 1: Write failing test** — build a `DecisionContext` (via `build_decision_context` with `parsed_status={"...","remembered":"Last time LeBron only talked legacy."}`) and assert `build_dialogue_prompt(ctx, action)` contains "Last time LeBron only talked legacy." Also assert empty `remembered` renders nothing.
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** — add `remembered: str = ""` to `DecisionContext` (after `disagreeableness`); in `build_decision_context` add `remembered=parsed_status.get("remembered", "")`; in `build_dialogue_prompt`, after the `take`/relationship lines, add:
```python
    if ctx.remembered:
        lines.append(f"What you actually remember about {addressee_name}: {ctx.remembered}")
```
- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit** — `git add` the 2 files → `feat(worldgm): render recalled memory in the dialogue prompt` + trailer.

---

### Task 6: CDK — memories table + worldgm env/grant

**Files:**
- Modify: `apps/infra/lib/stacks/database-stack.ts` (table + GSI + export + grant)
- Modify: `apps/infra/lib/stacks/worldgm-stack.ts` (env `RESIDENT_MEMORIES_TABLE`, `RESIDENT_MEMORY_ENABLED` per-env, grant)
- Test: `apps/infra/test/database-stack.test.ts` (assert table + GSI)

**Interfaces — Produces:** `goosetown-{env}-memories` table; worldgm container env `RESIDENT_MEMORIES_TABLE` + `RESIDENT_MEMORY_ENABLED` (dev `true`, prod `false`).

- [ ] **Step 1: Write failing test** — in `database-stack.test.ts`, assert a `dynamodb.Table` with `TableName` `goosetown-test-memories` (or whatever env the test stack uses) and a GSI named `agentId-subject-index` exist (mirror an existing table assertion in that file).
- [ ] **Step 2: Run → fail.** `cd apps/infra && npx jest database-stack`
- [ ] **Step 3: Implement** — add the `memories` table (PK `agentId` S, SK `createdAt` S, PAY_PER_REQUEST, PITR in prod, `removalPolicy`) + GSI `agentId-subject-index` (PK `agentId`, SK `subject`, projection ALL), mirroring the `ledger` table block. Add `memories: dynamodb.Table` to the `DatabaseTables` interface/exports. In `worldgm-stack.ts`: pass container env `RESIDENT_MEMORIES_TABLE: \`goosetown-${env}-memories\``, `RESIDENT_MEMORY_ENABLED: env === "prod" ? "false" : "true"`, and grant the worldgm task role `memories.grantReadWriteData(taskRole)` (wire the table ref through to worldgm-stack the same way other tables reach it).
- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Synth check** — `cd apps/infra && rm -rf ../backend/.venv 2>/dev/null; npx cdk synth GooseTownDev/DatabaseStack >/dev/null` (per CLAUDE.md: synth hangs if `apps/backend/.venv` exists). Confirm no error.
- [ ] **Step 6: Commit** — `git add apps/infra/lib/stacks/database-stack.ts apps/infra/lib/stacks/worldgm-stack.ts apps/infra/test/database-stack.test.ts` → `feat(infra): memories DynamoDB table + worldgm RESIDENT_MEMORY_ENABLED env (dev on, prod off)` + trailer.

---

## Validation (after all tasks)
1. `cd apps/backend && uv run pytest tests/unit/worldgm/ tests/unit/test_config.py -q` — green; `uv run ruff check worldgm/ core/config.py` clean.
2. `cd apps/infra && npx jest database-stack` — green; `npx cdk synth GooseTownDev/DatabaseStack` succeeds (no `.venv`).
3. After deploy to dev (`RESIDENT_MEMORY_ENABLED=true`): multi-hour soak → inspect `goosetown-dev-memories` (sensible observations + reflections accumulating), `goosetown-dev-relationships` (affinity drifting, stance notes set), and dev conversations (later convos reference earlier ones). Confirm Nova-Lite reflection quality (the Nova-capacity gate). Prod stays off until this passes.

## Out of scope (tracked)
Embeddings/semantic retrieval; memory TTL/compaction; frontend surfacing; Phase 3 (town lore/factions, social goals).
