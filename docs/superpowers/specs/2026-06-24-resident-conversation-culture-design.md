# Resident Conversation Culture — Design

**Date:** 2026-06-24
**Status:** Approved design → ready for implementation plan (Phase 1)
**Author:** brainstormed with Claude
**Frame:** GooseTown is a *culture lab*. The goal is residents that remember, evolve, differentiate, form relationships/factions, and develop a shared culture over time — not merely "watchable banter."

---

## 1. Context: what is actually live

The "personality upgrade" (character bibles + split-brain dialogue + relationship surfacing) is **real and running in prod**, but it lives on the **unmerged branch `feat/resident-cast-bibles`** (= `main` #403 + 2 commits: `3bac3225` overhaul, `b8383000` bibles). Proof it's deployed: neither dev nor prod worldgm sets `WORLDGM_DIALOGUE_MODEL`, yet there is real Amazon **Nova Pro** spend — only the branch code defaults dialogue to `nova-pro`; `main` has no nova-pro path at all.

**Implication / open hygiene item:** prod runs unmerged branch code and `main` is behind it. This is consistent with the team's "build → open-PR → deploy from branch → don't merge" pattern, but `main` is not the source of truth for what's deployed. **All Phase 1 work branches off `feat/resident-cast-bibles`** (the live code). Reconciling branch↔main is out of scope here but should be decided before launch (see §8).

### Live architecture (the relevant pieces)

- **Split-brain decision/dialogue** — `apps/backend/worldgm/residents/agentic_brain.py`:
  - `build_prompt(ctx)` → decision prompt (Nova Lite) returns a JSON action (`chat`/`say`/`end`/`move`/`activity`/`idle`).
  - `build_dialogue_prompt(ctx, action)` + `generate_dialogue(...)` → the spoken line (Nova Pro by default; `WORLDGM_DIALOGUE_MODEL=off` → Nova-Lite fallback).
  - `decide_and_speak(decision_model, dialogue_model, ctx)` orchestrates both.
- **Persona bibles** — `personas.py` (`Persona` dataclass) + `personas_prod.py` (19 prod residents). Fields: `personality`, `voice`, `obsessions`, `opinions` (`(name, take)` pairs), `current_life`, `sample_lines`, `traits`, `hangout`, sleep/wake.
- **Brain tick** — `residents/brain.py::step_resident_brain` calls `decide_and_speak`; pre-gates skip the LLM when asleep / mid-move / not-my-turn.
- **Turn cap** — `core/services/conversation_service.py:46` `_MAX_CONVERSATION_TURNS = 20` (hard force-end). Inactivity auto-end at 180s (`CONVERSATION_INACTIVITY_TIMEOUT_SEC`).
- **Relationship surfacing** — `_relationship_phrase(near)` uses `affinity` + `interactionCount` (backend-injected onto nearby entries) → already fed into the dialogue prompt.
- **`stance_bias.py`** — biases *GM narrative templates* (arc kickoffs) toward "tension" for hostile pairs. Does **not** touch per-turn dialogue.

### Evidence of the problem (live prod transcripts, post-deploy)

Pulled from `goosetown-prod-conversations` (49 convos since the deploy): **median 5 turns, mean 7.7, but a fat tail — 10/49 ran 13–20 turns to the cap.** Observed failure modes:

1. **Looping** — engaged convos escalate agreeable embellishments to the 20-turn cap (Biden+Obama: ice cream → trains → murals → green energy, never resolving).
2. **Relentless agreement** — even "frosty" pairs (Dwight/LeBron) converge; no real disagreement.
3. **Verbal-tic / catchphrase overuse** — "Here's the deal, pal" / "Fact:" on nearly every line (driven by `sample_lines` + per-turn voice injection).
4. **Ungrounded** — `topicSummary=_none`; residents invent fictional initiatives (moon farming, VR courts) disconnected from real town/Exchange state.
5. **Sameness** — every resident converges to the same themes (LeBron → "legacy/community" with everyone).

The deep cause behind all five: **conversations leave no residue.** Every convo restarts from the *static* bible; nothing accumulates. The bibles are the genome; culture (what should grow on top) never grows.

## 2. Goals / non-goals

**Goals**
- Kill the mush: grounded, differentiated conversations that *resolve* and have real friction.
- Lay the substrate for *accumulating* culture (memory, evolving opinions, factions).
- Keep Phase 1 safe to ship before the provider-testing / launch window; keep cost flat or lower.

**Non-goals**
- Not rebuilding the game server or the split-brain architecture (it's sound).
- Not adding Exchange verbs to residents (separate track).
- Not authoring hand-scripted storylines (culture must emerge, not be scripted).

## 3. Research basis (external precedent)

Synthesized from: Stanford **Generative Agents** (Park et al. 2023, memory→reflection→planning, goal-conditioned dialogue); **"Peacemaker or Troublemaker"** (2025, heterogeneous disagreeableness beats uniform agreement; CoT *amplifies* sycophancy); **Letta/MemGPT** (pinned persona blocks + self-edited relationship memory; consistency without few-shot examples); **Project Sid / PIANO** (Altera 2024, social-goal generation → role specialization; location-as-cultural-island; coherence bottleneck); **Concordia** (DeepMind, Game-Master grounding); **AI Town** (a16z, lean summarize→embed→retrieve memory); **Verbalized Sampling** (2025, k-candidate sampling restores diversity); **Diversity Collapse in MAS** (ACL 2026, sparse topology + value anchors + per-agent temperature counter convergence). Full source list lives in the research synthesis attached to the 2026-06-24 session.

**Cross-cutting caveat:** nearly all results are validated on GPT-4o/Claude-tier models; Sid's authors flag base-model capacity as a hard dependency. GooseTown runs **Amazon Nova**. Phase 1 is model-agnostic prompt/config work and safe regardless. Heavy Phase 2/3 machinery is gated on a **Nova-capacity validation** (§7).

---

## 4. Phase 1 — "Stop the mush" (pre-launch; prompt + small persona-field + one backend field)

Five changes. All on top of `feat/resident-cast-bibles`. No new storage tables.

### 4.1 Conversation intent + low turn cap (the loop-killer)

The real fix for looping is a **goal-conditioned conversation** (Generative Agents), not just a cap.

- **Generate an intent at convo-start.** When a resident's decision returns a `chat` (opening) action, generate a one-line intent describing *why they walked over* (e.g., *"Dwight wants to know if LeBron will respect the permit rules"*). Cheapest implementation: have the opening-line dialogue call also emit the intent (one extra short field), or a tiny dedicated Nova-Lite call. Initiator-generated only (the responder reacts).
- **Store + surface it.** Add an `intent` field to the conversation record (backend) and echo it in the `active_conversation` payload so **both** residents see it each turn. This is the one backend-touching item in Phase 1 (`conversation_service` create path + the `active_conversation` projection read by `read_status`/`build_decision_context`).
- **End when addressed.** Feed the intent + the live turn count (`len(active_conversation["turns"])`, already available) into `build_prompt`'s say/end choice: *"You came over to: {intent}. If that's been addressed (answered or refused), choose `end`. People make their point and move on — they don't agree back and forth forever."* Bias toward `end` past ~4 turns.
- **Backstop cap.** `conversation_service.py:46`: make `_MAX_CONVERSATION_TURNS` env-tunable (`WORLDGM_MAX_CONVERSATION_TURNS`), default **6** (down from 20).

### 4.2 Heterogeneous disagreeableness (the sycophancy fix)

- **New persona field** `disagreeableness: int` on `Persona` (scale 1–5; 1 = warm/agreeable, 5 = constitutionally contrarian). Author it for all 19 prod residents, **skewing the cast toward neutral/contrarian** (a uniformly-agreeable population is empirically the worst config).
- **Trait-conditioned dialogue instruction** in `build_dialogue_prompt`, replacing the current flat *"agree, riff…"*:
  - high disagreeableness → *"You don't fold to be liked. If you see it differently, say so plainly and hold your ground."*
  - low → *"You're warm, but you still bring your own real opinion — don't just validate them."*
  - Layer the existing relationship `take`/affinity on top (frosty pair + contrarian trait → real conflict).
- **No chain-of-thought** "do I really agree?" step — measured to *amplify* sycophancy.
- *(Optional, deferred)* log stance-flips (Negative Agreement Rate) as a health metric — see §7.

### 4.3 Remove `sample_lines`; pin a described voice + value anchors (voice consistency + anti-convergence)

- **Delete `sample_lines`** from `Persona`, all 19 `personas_prod.py` bibles, and its injection in `build_dialogue_prompt` (branch lines ~534–537). Few-shot example lines are the #1 driver of catchphrase parody.
- **Keep `voice`** (described diction/cadence) pinned every turn — that carries the character without examples.
- **Add 2–3 `values`/anchors per resident** (reuse the authoring budget freed by removing `sample_lines`). Divergent value anchors are what stop everyone collapsing to the same themes. (May be folded into an expanded `voice`/`personality` rather than a separate field — implementation's call; the requirement is *divergent, described, example-free*.)

### 4.4 World-state grounding + ban invented entities

- In `build_dialogue_prompt`, inject a concrete **facts block** from data already in `DecisionContext`: location, who's actually present (`nearby`), real recent events (`recent_events` from `TOWN_EVENTS.md`), the GM `active_storyline`, real mood/energy.
- Instruction: *"Talk about what's actually here — what they just said, a real recent event, what's going on in town. Do not invent events, places, projects, or people not present above."*

### 4.5 Anti-tic / anti-convergence polish

- **Per-agent temperature variance** (free): derive each resident's dialogue temperature from a stable per-persona offset around the current 0.9, so the cast doesn't collapse to identical phrasings.
- Add: *"Don't restate your last point or reach for your usual catchphrases — move it somewhere new or land it."*

### 4.6 Explicitly held for later (not in Phase 1)

- **Verbalized Sampling** (k=3–5 candidate lines, sample) — best single diversity lever, but ~2–3× the dialogue output tokens → raises the Nova Pro bill. Revisit only if convos still feel same-y after 4.1–4.5.
- **Server-side semantic-similarity loop detection** (embed last 2 turns, cosine > ~0.85 → force end) — nice backstop, adds embedding calls. Revisit if the intent+cap mechanism under-performs.

### 4.7 Phase 1 testing

- Unit tests against existing fixtures (`tests/unit/worldgm/residents/test_agentic_brain.py`): assert `sample_lines` is gone from prompts; assert intent + turn-count appear in the decision prompt; assert end-pressure fires past the threshold; assert the disagreeableness instruction varies by trait; assert the grounding/ban-invention clause is present.
- Conversation-service test: `WORLDGM_MAX_CONVERSATION_TURNS` honored; default 6.
- Manual: deploy to dev, pull `goosetown-dev-conversations`, confirm median turns drop, disagreement appears, no catchphrase spam, no invented entities.

---

## 5. Phase 2 — "Culture accumulates" (the spine; roadmap)

Adopt the Generative Agents memory→reflection loop, Nova-right-sized via AI Town's lean recipe + Letta-style relationship memory.

- **After each convo:** summarize → score importance (Generative Agents poignancy prompt, 1–10) → embed + store in a per-resident **memory stream**.
- **Relationship residue:** alongside the existing scalar `affinityScore` on the `relationships` table, keep a self-edited **"what changed between us" narrative block** the resident updates after each encounter (Letta `core_memory` pattern).
- **On next encounter:** retrieve top-3 memories about the other (score = recency·importance·relevance, each normalized) + the relationship block → inject into `build_dialogue_prompt`. Topic now comes from *retrieval*, not imagination.
- **Reflection:** periodic (importance-sum trigger) synthesis of higher-level insights → stored as high-importance memories. This is what makes opinions *drift* and residents diverge.
- **Storage:** new memory table (or extend an existing one) + embeddings; cache by text-hash to bound cost.

## 6. Phase 3 — "Town-level culture" (roadmap)

- **Social-goal generation** per resident → emergent role specialization (Sid; measure via role-entropy).
- **Locations as cultural islands:** lean on GooseTown's 7 locations + a sparser interaction topology so *distinct* local cultures form instead of a monoculture.
- **Factions for free:** bias each resident's "who do I approach" decision by `affinityScore` → preferential-attachment clusters emerge.
- **Shared lore:** GM-seeded memes/traditions tracked as spreading culture + a town lore/history log residents can cite.

## 7. Cross-cutting: Nova-capacity gate + health metrics

- **Nova-capacity gate (before Phase 2/3):** validate on a slice that Nova sustains reflection-quality summaries, in-character disagreement, and grounded social goals. If Nova underperforms, scope Phase 2/3 down or pin a stronger dialogue model via `WORLDGM_DIALOGUE_MODEL`.
- **Health metrics (so "is culture working" is measurable):**
  - *Stance-flip rate / Negative Agreement Rate* — abandons own position mid-convo (sycophancy proxy).
  - *Self-BLEU* across a resident's recent lines — catchphrase recycling.
  - *Diversity* (pairwise cosine / Vendi) across the cast — convergence.
  - *Median turn count* — looping.
  - Baseline these on current prod, re-measure after Phase 1.

## 8. Risks / open questions

- **Branch↔main divergence.** Prod runs `feat/resident-cast-bibles`; `main` is behind. Phase 1 stacks on the branch. **Decide before launch:** merge the branch to main (make main the source of truth) or formalize the deploy-from-branch process. Out of scope for the build, but a launch blocker for hygiene.
- **Cost.** Phase 1 should be cost-neutral-to-negative (shorter convos = less Nova Pro). The conversation-intent call adds a small per-convo cost; the turn-cap reduction more than offsets it. Verbalized Sampling intentionally deferred for this reason.
- **Backend touch.** 4.1's intent field is the only non-prompt change in Phase 1; keep it minimal (one field on the conversation + its projection).
- **Model dependency.** Several research results assume GPT-4o/Claude-tier; Nova behavior on disagreement/grounding must be eyeballed on dev before prod.

## 9. Rollout

1. Branch off `feat/resident-cast-bibles`.
2. Implement 4.1–4.5 + tests; deploy to dev worldgm; eyeball `goosetown-dev-conversations` + baseline the §7 metrics.
3. Tune turn cap / disagreeableness skew / temperature spread on dev.
4. Promote to prod (same deploy-from-branch path that's live today).
5. Re-measure metrics on prod; decide on the deferred levers (4.6) and the branch↔main reconciliation (§8).
6. Phase 2 gets its own spec → plan once Phase 1 metrics confirm the substrate is clean.
