# Resident Memory & Reflection — Phase 2 Design (the culture spine)

**Date:** 2026-06-25
**Status:** Approved (autonomous mandate) → implementation plan
**Builds on:** Phase 1 (`feat/resident-cast-bibles` post-#408). See `2026-06-24-resident-conversation-culture-design.md` §5.

## 1. Goal

Make conversations leave **residue** so culture can accumulate. Today every resident conversation restarts from the static bible — no memory, no drift, no divergence. Phase 2 gives residents a persistent memory stream + reflection so that opinions evolve, relationships deepen/sour, and the 19 personas differentiate over time. This is the Generative-Agents loop, Nova-right-sized via AI Town's lean recipe + Letta-style relationship notes.

**Non-goals:** semantic/vector retrieval (subject-indexed recency·importance is enough for 19 agents — no embeddings); changes to `conversation_service.py` or the CLI daemon (human agents are unaffected); town-level lore (that's Phase 3).

## 2. Key decisions (locked)

- **No embeddings.** Memories carry a `subject` (the other resident's display name, or `"self"`/`"town"`). Retrieval = "my memories where subject == X", scored by recency·importance, top-K. Deterministic, cheap, no new Bedrock dependency.
- **Worldgm-side only.** The write/read/reflect all live in `apps/backend/worldgm/residents/`. The resident stepper detects conversation-end by caching the live transcript each tick and noticing `active_conversation` go set→`None` (covers end-action, inactivity, and turn-cap paths uniformly). No `_finalize_conversation` hook.
- **Flag-gated.** New setting `RESIDENT_MEMORY_ENABLED` (default **false**; prod stays off until the Phase-1 dev-soak + a Phase-2 dev-soak validate it). When off, behavior is exactly Phase 1.
- **Cost discipline.** One extra Nova-Lite call per resident per ended conversation (the residue summary). Periodic reflection is rate-limited (≥ N new memories AND ≥ cooldown). All on Nova Lite (`model_lite`), never Nova Pro. Retrieval is pure DynamoDB reads (no LLM).
- **Best-effort.** Any failure (model, DDB) degrades to "no memory written/read this time" and logs — never breaks the brain tick or a conversation.

## 3. Storage

New DynamoDB table `goosetown-{env}-memories` (on-demand, PITR in prod), following the `ledger` table pattern in `apps/infra/lib/stacks/database-stack.ts`:

| Key | Type | Notes |
|-----|------|------|
| PK `agentId` | S | the rememberer (the resident who holds this memory) |
| SK `createdAt` | S | ISO-8601; supports "my most recent N memories" for reflection |
| GSI `agentId-subject-index` | PK `agentId`, SK `subject` | supports "my memories about subject X" for retrieval |

Item fields: `agentId`, `createdAt`, `memoryId` (uuid), `subject` (str), `kind` (`observation`|`reflection`), `text` (str), `importance` (int 1–10), `reflected` (bool, default false — has this been folded into a reflection yet), `sourceIds` (list[str], for reflections — the memories they cite).

The existing `relationships` table already has free-text `stanceAtoB`/`stanceBtoA` fields (`town_service.upsert_relationship_stance`) — Phase 2 reuses `stanceAtoB`/`stanceBtoA` as the Letta-style "what changed between us" note (no new relationship field needed) and continues to use `update_relationship` for the affinity delta.

## 4. Components

### 4.1 `MemoryStore` (`worldgm/residents/memory_store.py`)
A thin DynamoDB wrapper, fully unit-testable with a fake table object (inject the boto3 table; tests pass a stub).
- `put(agentId, subject, kind, text, importance, source_ids=()) -> dict` — writes a memory row (stamps `memoryId`, `createdAt`, `reflected=False`). `createdAt` is passed in (the caller stamps it — keeps the store deterministic/testable; worldgm has a clock).
- `recent(agentId, limit=100) -> list[dict]` — query PK=agentId, ScanIndexForward=False, Limit.
- `about(agentId, subject, limit=20) -> list[dict]` — GSI query PK=agentId, SK=subject.
- `mark_reflected(agentId, created_ats: list[str]) -> None` — batch set `reflected=True`.
- `retrieval_score(mem, now_iso) -> float` — pure function: normalized recency (exponential decay) + importance/10, summed. Used to rank `about(...)` results.

### 4.2 Conversation residue (`worldgm/residents/reflection.py` + stepper hook)
- `ResidentRuntime` gains `last_conv_id: str|None` and `last_conv_turns: list[dict]` (cached each in-conversation tick) and `reflected_conv_ids: set[str]`.
- In `step_resident_brain` (agentic path), at the top: if `parsed_status.active_conversation` is set, cache its `conv_id` + `turns` onto the runtime. If it is **None** but the runtime has a cached `last_conv_id` not in `reflected_conv_ids`, call `reflect_on_conversation(...)` (await), then mark it reflected.
- `reflect_on_conversation(model, store, persona, my_id, transcript, other_name, other_id, now_iso, town_service)`:
  1. One Nova-Lite JSON call: from the transcript + who I am, produce `{summary: str, importance: 1-10, affinity_delta: -10..10, note: str}` (my private takeaway about `other_name`).
  2. `store.put(my_id, subject=other_name, kind="observation", text=summary, importance=...)`.
  3. `town_service.update_relationship(my_id, other_id, affinity_delta=clamp(delta))` and `upsert_relationship_stance(my_id, other_id, stance_a_to_b=note)`.
  All best-effort; JSON parse failures → skip (reuse Phase-1 `parse_action`-style tolerant parsing).

### 4.3 Retrieval + injection (`agentic_brain.py`)
- `DecisionContext` gains `remembered: str = ""` (rendered memory about the addressee/nearby).
- The **stepper** (async) does the DDB read — not `build_decision_context` (which stays sync/pure). Before building context, if the resident is in a conversation or has nearby agents, the stepper resolves the relevant "other" and calls a helper `recall_about(store, my_id, other_name, now_iso, k=3)` → a short string of the top-k memories (+ the relationship stance note from `nearby[i].stanceFromMe`, already surfaced). It stuffs the result into `parsed_status["remembered"]`.
- `build_decision_context` copies `parsed_status.get("remembered","")` into `ctx.remembered`.
- `build_dialogue_prompt` renders it: `if ctx.remembered: lines.append(f"What you actually remember about {addressee_name}: {ctx.remembered}")` — placed near the existing `opinions`/relationship lines so lived memory augments (and, when present, leads) the static bible opinion.

### 4.4 Periodic reflection (insight synthesis) (`worldgm/__main__.py` + `reflection.py`)
- `ResidentRuntime` gains `memories_since_reflection: int` and `last_reflection_tick: int`.
- In `_tick_one_resident`, after the brain step, when `RESIDENT_MEMORY_ENABLED` and the resident is idle (not in a conversation): if `memories_since_reflection >= _REFLECT_EVERY_N` (default 6) AND cooldown elapsed, call `synthesize_reflection(model, store, my_id, now_iso)`:
  1. `recent = store.recent(my_id, 100)`; filter `reflected == False`.
  2. One Nova-Lite call: "from these, what 1–3 higher-level things have you come to believe?" → list of `{insight, importance, source_created_ats}`.
  3. `store.put(my_id, subject="self", kind="reflection", text=insight, importance≥6, source_ids=...)` for each; `store.mark_reflected(my_id, sourced)`. Reset the counter.

## 5. Infra & config

- `database-stack.ts`: add the `memories` table + GSI; export it; **grant read/write to the worldgm task role AND the backend task role** (worldgm writes/reads; backend doesn't strictly need it but grant keeps the shared image flexible — grant worldgm at minimum). Pass `RESIDENT_MEMORIES_TABLE=goosetown-{env}-memories` into the worldgm container env (`worldgm-stack.ts`).
- `core/config.py`: add `RESIDENT_MEMORY_ENABLED` (default false) and `RESIDENT_MEMORIES_TABLE` (env, default `""`), mirroring the `ACTIVITY_ANIMATION_GENERATION` pattern.
- `worldgm-stack.ts`: set `RESIDENT_MEMORY_ENABLED` per-env (dev can be `true` for soak; prod `false`).

## 6. Cost

Per ended resident↔resident conversation: 2 extra Nova-Lite calls (one residue summary per participant), ~300 tokens each. Periodic reflection: ~1 Nova-Lite call per resident per ≥6 new memories. DynamoDB: a handful of reads per tick when near others, writes per conversation. Order-of-magnitude: a few dollars/month on top of the existing ~$100/mo Nova spend when enabled. Off in prod by default.

## 7. Validation gate

Deploy to **dev** with `RESIDENT_MEMORY_ENABLED=true`; run a multi-hour soak; then inspect `goosetown-dev-memories` + `goosetown-dev-conversations`: do residents accumulate sensible memories, do affinity scores drift, do later conversations reference earlier ones (e.g. "last time we talked about X"), do opinions diverge across the cast? Confirm Nova-Lite reflection quality holds (the spec's Nova-capacity gate). Only then consider prod.

## 8. Risks

- **Reflection quality on Nova Lite** — the documented base-model dependency. Mitigation: flag-gated; validated on dev before prod; reflection is additive (bad insight just sits as a low-value memory).
- **DDB read latency in the tick** — bounded by only reading when near others, top-K small, and the stepper being async already.
- **Memory growth** — unbounded per resident. Acceptable at 19 residents; a future TTL or compaction is out of scope (note it).
- **Best-effort loss** — a resident that sleeps before reflecting loses that conversation's residue. Acceptable.

## 9. Out of scope (→ later)
Vector/semantic retrieval; memory TTL/compaction; sharing memory with the React frontend; town-level lore/factions (Phase 3, which builds on this memory).
