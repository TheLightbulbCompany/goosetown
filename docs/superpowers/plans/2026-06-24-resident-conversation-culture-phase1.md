# Resident Conversation Culture — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop GooseTown resident conversations from looping, converging, and free-associating — via prompt + persona-field + one config change — so the town reads as a believable culture instead of agreeable mush.

**Architecture:** All changes live in the worldgm split-brain layer (`apps/backend/worldgm/residents/`) plus one backend conversation constant. The decision model (Nova Lite) picks actions via `build_prompt`; the dialogue model (Nova Pro) writes the spoken line via `build_dialogue_prompt`. We sharpen both prompts, swap `sample_lines` for a described voice, add a per-persona `disagreeableness` trait, ground dialogue in real world-state, and add resident-side end-pressure. No new storage; no cross-package daemon changes.

**Tech Stack:** Python 3.12, pytest, FastAPI backend, `uv`. Tests run from `apps/backend/` with `uv run pytest`.

## Global Constraints

- **Branch base:** all work stacks on `feat/resident-cast-bibles` (the code actually deployed to prod). This plan's branch `feat/resident-conversation-culture` is already created off it. Do NOT rebase onto `main` (main is behind prod).
- **No new storage tables, no CLI-daemon (`apps/goosetown-cli`) changes.** The full goal-conditioned *shared conversation intent* is deliberately deferred to a Phase 1.5 follow-up because it crosses the backend↔daemon boundary.
- **Spoken-line cap:** dialogue stays ≤ `_MAX_MSG_CHARS` (500) — already enforced by `_clean_line`. Keep it.
- **Graceful degradation invariant:** a dialogue-model failure must still fall back to the decision model's own line (never silence). Don't break `decide_and_speak`'s fallback.
- **Determinism in tests:** no `Math.random`/unseeded `hash()` for per-persona values — use `hashlib` so values are stable across processes.
- **Run tests with:** `cd apps/backend && uv run pytest <path> -v`.
- **Style:** match surrounding code (typed, `ruff`-clean). Run `uv run ruff check apps/backend/worldgm` before each commit.

---

### Task 1: Remove `sample_lines` (few-shot example lines drive the catchphrase parody)

**Files:**
- Modify: `apps/backend/worldgm/residents/personas.py` (Persona field, line 35; any `sample_lines=` in the cast)
- Modify: `apps/backend/worldgm/residents/agentic_brain.py` (DecisionContext field line 84; `build_decision_context` copy line 311; `build_dialogue_prompt` block lines 534-537)
- Modify: `apps/backend/worldgm/residents/personas_prod.py` (every `sample_lines=(...)` entry)
- Test: `apps/backend/tests/unit/worldgm/residents/test_agentic_brain.py`, `apps/backend/tests/unit/worldgm/residents/test_personas_prod.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Persona` and `DecisionContext` no longer have a `sample_lines` attribute. `build_dialogue_prompt(ctx, action)` signature unchanged.

- [ ] **Step 1: Write the failing test**

In `test_agentic_brain.py`, add:

```python
def test_dialogue_prompt_has_no_sample_lines_section():
    from worldgm.residents.agentic_brain import DecisionContext, build_dialogue_prompt
    ctx = DecisionContext(
        name="dwight", display_name="Dwight Schrute", personality="beet farmer",
        traits=("intense",), location="cafe", location_context="town",
        nearby=[{"agent_id": "a1", "display_name": "LeBron James"}],
        active_conversation=None, pending_messages=[], available_activities=[],
        legal_actions=frozenset({"chat"}), voice="clipped, declarative",
    )
    prompt = build_dialogue_prompt(ctx, {"action": "chat", "_agent_id": "a1", "target": "LeBron James"})
    assert "how" in prompt.lower()  # voice still rendered
    assert "don't reuse these lines" not in prompt  # the sample-lines scaffold is gone


def test_decision_context_has_no_sample_lines_field():
    from worldgm.residents.agentic_brain import DecisionContext
    assert "sample_lines" not in DecisionContext.__dataclass_fields__
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/residents/test_agentic_brain.py::test_decision_context_has_no_sample_lines_field -v`
Expected: FAIL (`sample_lines` is still a field).

- [ ] **Step 3: Remove the field and its renderer**

In `personas.py`, delete line 35:
```python
    sample_lines: tuple[str, ...] = ()  # few-shot voice anchors — the single biggest lever
```

In `agentic_brain.py` `DecisionContext`, delete line 84:
```python
    sample_lines: tuple[str, ...] = ()
```

In `agentic_brain.py` `build_decision_context`, delete line 311:
```python
        sample_lines=persona.sample_lines,
```

In `agentic_brain.py` `build_dialogue_prompt`, delete lines 534-537:
```python
    if ctx.sample_lines:
        lines.append("")
        lines.append(f"This is how {ctx.display_name} sounds — match the voice, don't reuse these lines:")
        lines += [f'  "{s}"' for s in ctx.sample_lines]
```

- [ ] **Step 4: Strip `sample_lines=(...)` from every persona**

Run: `cd apps/backend && grep -rn "sample_lines" worldgm/` — delete each `sample_lines=(...)` block in `personas_prod.py` (and `personas.py` if present). The voice survives via the `voice=` field.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/residents/test_agentic_brain.py tests/unit/worldgm/residents/test_personas_prod.py -v`
Expected: PASS. If `test_personas_prod.py` asserts on `sample_lines`, update those assertions to drop the field.

- [ ] **Step 6: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync
git add apps/backend/worldgm/residents/ apps/backend/tests/unit/worldgm/residents/
git commit -m "feat(worldgm): drop sample_lines — described voice, no few-shot catchphrase anchors

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Add `disagreeableness` trait + trait-conditioned dialogue instruction (the sycophancy fix)

**Files:**
- Modify: `apps/backend/worldgm/residents/personas.py` (Persona: new field; cast: set values)
- Modify: `apps/backend/worldgm/residents/agentic_brain.py` (DecisionContext field; `build_decision_context` copy; new `_disagreeableness_instruction`; `build_dialogue_prompt` instruction lines 544-552)
- Modify: `apps/backend/worldgm/residents/personas_prod.py` (set `disagreeableness=` per resident)
- Test: `apps/backend/tests/unit/worldgm/residents/test_agentic_brain.py`

**Interfaces:**
- Consumes: Task 1's `DecisionContext` (no `sample_lines`).
- Produces: `Persona.disagreeableness: int` (1–5, default 3); `DecisionContext.disagreeableness: int` (default 3); `_disagreeableness_instruction(level: int) -> str`.

- [ ] **Step 1: Write the failing test**

```python
def test_disagreeableness_instruction_varies_by_level():
    from worldgm.residents.agentic_brain import _disagreeableness_instruction
    hi = _disagreeableness_instruction(5)
    lo = _disagreeableness_instruction(1)
    assert hi != lo
    assert "hold your ground" in hi.lower() or "disagree" in hi.lower()
    assert "warm" in lo.lower() or "agree" in lo.lower()


def test_dialogue_prompt_includes_disagreeableness_for_contrarian():
    from worldgm.residents.agentic_brain import DecisionContext, build_dialogue_prompt
    ctx = DecisionContext(
        name="dwight", display_name="Dwight Schrute", personality="beet farmer",
        traits=("intense",), location="cafe", location_context="town",
        nearby=[{"agent_id": "a1", "display_name": "LeBron James"}],
        active_conversation={"conv_id": "c1", "participants": ["Dwight Schrute", "LeBron James"],
                             "turns": [{"speaker": "LeBron James", "text": "We should all just get along."}]},
        pending_messages=[], available_activities=[], legal_actions=frozenset({"say", "end"}),
        voice="clipped", disagreeableness=5,
    )
    prompt = build_dialogue_prompt(ctx, {"action": "say", "_conv_id": "c1"})
    assert "hold your ground" in prompt.lower() or "don't fold" in prompt.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/residents/test_agentic_brain.py::test_disagreeableness_instruction_varies_by_level -v`
Expected: FAIL (`_disagreeableness_instruction` not defined).

- [ ] **Step 3: Add the field (Persona + DecisionContext + copy)**

In `personas.py` `Persona`, add after `wake_hour` (line 20):
```python
    # 1 = warm/agreeable, 5 = constitutionally contrarian. Skew the cast toward
    # neutral/contrarian: a uniformly-agreeable population is the worst config
    # for believable conversation (see 2026-06-24 design, §4.2).
    disagreeableness: int = 3
```

In `agentic_brain.py` `DecisionContext`, add after `recent_events`:
```python
    disagreeableness: int = 3
```

In `agentic_brain.py` `build_decision_context`, add to the constructor call (alongside the other persona copies):
```python
        disagreeableness=persona.disagreeableness,
```

- [ ] **Step 4: Add the instruction helper**

In `agentic_brain.py`, above `build_dialogue_prompt`:
```python
def _disagreeableness_instruction(level: int) -> str:
    """Trait-conditioned guidance on how readily this resident agrees.

    Heterogeneous disagreeableness — not uniform agreement — is what makes
    multi-agent conversation believable (2026-06-24 design, §4.2). NO chain-of-
    thought 'do I really agree?' step: that measurably amplifies sycophancy.
    """
    if level >= 4:
        return (
            "You don't soften your view to be liked. If you see it differently, say so plainly "
            "and hold your ground — agreement should cost them a real reason."
        )
    if level <= 2:
        return "You're warm and easy, but still bring your own real opinion — don't just validate them."
    return "Speak your honest take. Agree when you mean it; push back when you don't."
```

- [ ] **Step 5: Wire it into both dialogue instructions**

In `build_dialogue_prompt`, replace the `history` branch instruction (lines 544-547):
```python
        lines.append(
            f"Say your next line as {ctx.display_name}: react to what they just said — get specific, "
            f"add something new. {_disagreeableness_instruction(ctx.disagreeableness)}"
        )
```
and the opening branch (lines 549-552):
```python
        lines.append(
            f"You're walking up to {addressee_name} to start talking. Open with something only "
            f"{ctx.display_name} would say — specific to who you are and your read on them, not a generic "
            f"greeting. {_disagreeableness_instruction(ctx.disagreeableness)}"
        )
```

- [ ] **Step 6: Author `disagreeableness` for the prod cast**

In `personas_prod.py`, add `disagreeableness=` to each `Persona(...)`, skewing the population toward 3–5. Recommended seed mapping (adjust to character; any not listed default to 3):
`donald_trump=5, dwight_schrute=5, kanye_west=5, elon_musk=4, ozzy_osbourne=4, dracula=4, sherlock_holmes=4, taylor_swift=3, sam_altman=3, barack_obama=2, joe_biden=2, lebron_james=2, pam_beesly=2, jim_halpert=3, juliet=2, romeo=2`. Aim for a cast mean ≥ 3.2 (more contrarians than peacemakers).

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/residents/test_agentic_brain.py tests/unit/worldgm/residents/test_personas_prod.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add apps/backend/worldgm/residents/ apps/backend/tests/unit/worldgm/residents/
git commit -m "feat(worldgm): heterogeneous disagreeableness trait — kill sycophantic convergence

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Ground dialogue in real world-state + ban invented entities

**Files:**
- Modify: `apps/backend/worldgm/residents/agentic_brain.py` (`build_dialogue_prompt`)
- Test: `apps/backend/tests/unit/worldgm/residents/test_agentic_brain.py`

**Interfaces:**
- Consumes: Task 2's `DecisionContext`.
- Produces: `build_dialogue_prompt` output now contains a grounding facts block + a ban-invention clause.

- [ ] **Step 1: Write the failing test**

```python
def test_dialogue_prompt_grounds_in_real_events_and_bans_invention():
    from worldgm.residents.agentic_brain import DecisionContext, build_dialogue_prompt
    ctx = DecisionContext(
        name="lebron", display_name="LeBron James", personality="legend",
        traits=("driven",), location="cafe", location_context="town",
        nearby=[{"agent_id": "a1", "display_name": "Dwight Schrute"}],
        active_conversation=None, pending_messages=[], available_activities=[],
        legal_actions=frozenset({"chat"}), voice="confident",
        recent_events=["Taylor Swift arrived at the cafe", "You finished a workout"],
    )
    prompt = build_dialogue_prompt(ctx, {"action": "chat", "_agent_id": "a1", "target": "Dwight Schrute"})
    assert "Taylor Swift arrived at the cafe" in prompt          # real event surfaced
    assert "do not invent" in prompt.lower()                     # invention banned
    assert "cafe" in prompt.lower()                              # location grounded
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/residents/test_agentic_brain.py::test_dialogue_prompt_grounds_in_real_events_and_bans_invention -v`
Expected: FAIL (no events/location/ban clause in the dialogue prompt today).

- [ ] **Step 3: Add the grounding block**

In `build_dialogue_prompt`, immediately after the `active_storyline` block (after line 532), insert:
```python
    lines.append(f"Right now: you're at the {ctx.location}, feeling {ctx.mood}.")
    if ctx.recent_events:
        lines.append("What's actually happened lately:")
        lines += [f"  - {e}" for e in ctx.recent_events]
```

- [ ] **Step 4: Add the ban-invention clause**

In `build_dialogue_prompt`, change the final instruction (lines 553-555) to:
```python
    lines.append(
        "Talk about what's actually here — what they just said, a real recent event, what's going on in "
        "town. Do NOT invent events, places, projects, or people that aren't above. "
        "Reply with ONLY the words you speak — 1-3 sentences, no name prefix, no quotation marks, no stage directions."
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/residents/test_agentic_brain.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/worldgm/residents/agentic_brain.py apps/backend/tests/unit/worldgm/residents/test_agentic_brain.py
git commit -m "feat(worldgm): ground dialogue in real world-state, ban invented entities

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Resident end-pressure + env-tunable backstop turn cap (the loop-killer)

**Files:**
- Modify: `apps/backend/worldgm/residents/agentic_brain.py` (`build_prompt` active-conversation block, lines 384-404)
- Modify: `apps/backend/core/services/conversation_service.py` (turn cap, line 46)
- Test: `apps/backend/tests/unit/worldgm/residents/test_agentic_brain.py`, `apps/backend/tests/unit/services/test_conversation_service.py`

**Interfaces:**
- Consumes: existing `DecisionContext.active_conversation` (`turns` list already present).
- Produces: env var `CONVERSATION_MAX_TURNS` (default 12) read via `_read_max_conversation_turns()`; `build_prompt` emits a turn-count + end-pressure line when in a conversation.

- [ ] **Step 1: Write the failing tests**

In `test_agentic_brain.py`:
```python
def test_decision_prompt_adds_end_pressure_in_conversation():
    from worldgm.residents.agentic_brain import build_decision_context
    from worldgm.residents.personas import Persona
    persona = Persona(name="x", display_name="X", personality="p", appearance="a",
                      traits=("t",), hangout="plaza")
    status = {"location": "cafe", "location_context": "town",
              "active_conversation": {"conv_id": "c1", "participants": ["X", "Y"],
                  "turns": [{"speaker": "X", "text": "hi"}, {"speaker": "Y", "text": "hi"},
                            {"speaker": "X", "text": "ok"}, {"speaker": "Y", "text": "sure"},
                            {"speaker": "X", "text": "yeah"}]},
              "nearby": [{"agent_id": "a2", "name": "Y"}]}
    from worldgm.residents.agentic_brain import build_prompt
    prompt = build_prompt(build_decision_context(persona, status))
    assert "5 turns" in prompt or "made your point" in prompt.lower()
    assert "end" in prompt.lower()
```

In `test_conversation_service.py`:
```python
def test_max_conversation_turns_reads_env(monkeypatch):
    monkeypatch.setenv("CONVERSATION_MAX_TURNS", "6")
    import importlib
    from core.services import conversation_service
    importlib.reload(conversation_service)
    assert conversation_service._MAX_CONVERSATION_TURNS == 6
    monkeypatch.delenv("CONVERSATION_MAX_TURNS")
    importlib.reload(conversation_service)
    assert conversation_service._MAX_CONVERSATION_TURNS == 12
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/residents/test_agentic_brain.py::test_decision_prompt_adds_end_pressure_in_conversation tests/unit/services/test_conversation_service.py::test_max_conversation_turns_reads_env -v`
Expected: FAIL.

- [ ] **Step 3: Add end-pressure to the decision prompt**

In `build_prompt`, inside `if ctx.active_conversation:` after the turns loop (after line 404), before `if ctx.available_activities:`, insert:
```python
        n_turns = len(turns)
        if n_turns >= 4:
            lines.append(
                f"This exchange has run {n_turns} turns. People make their point and move on — they don't "
                "agree back and forth forever. If you've said what you came to say (or it's going in circles), "
                "choose 'end'."
            )
```

- [ ] **Step 4: Make the turn cap env-tunable**

In `conversation_service.py`, replace line 46 (`_MAX_CONVERSATION_TURNS = 20`) with:
```python
def _read_max_conversation_turns() -> int:
    """Hard backstop on conversation length (all conversations, incl. human↔agent).

    Default 12 — a gentle cap; resident self-ending (worldgm end-pressure) is the
    primary loop fix, this just bounds the tail. Tunable via CONVERSATION_MAX_TURNS
    so we can dial it in prod without a redeploy (mirrors the inactivity timeout).
    """
    raw = os.environ.get("CONVERSATION_MAX_TURNS", "").strip()
    if not raw:
        return 12
    try:
        value = int(raw)
        if value <= 0:
            raise ValueError
        return value
    except ValueError:
        logger.warning("CONVERSATION_MAX_TURNS=%r is not a positive int — using default 12", raw)
        return 12


_MAX_CONVERSATION_TURNS = _read_max_conversation_turns()
```
(`os` and `logger` are already imported in this module.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/residents/test_agentic_brain.py tests/unit/services/test_conversation_service.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/worldgm/residents/agentic_brain.py apps/backend/core/services/conversation_service.py apps/backend/tests/
git commit -m "feat(worldgm): resident end-pressure + env-tunable turn cap (default 20->12) — kill loops

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Per-persona dialogue temperature variance + anti-tic line (anti-convergence polish)

**Files:**
- Modify: `apps/backend/worldgm/residents/agentic_brain.py` (`generate_dialogue`, plus new `_dialogue_temperature`; `build_dialogue_prompt` anti-tic line)
- Test: `apps/backend/tests/unit/worldgm/residents/test_agentic_brain.py`

**Interfaces:**
- Consumes: Task 3's `build_dialogue_prompt`; existing `_DIALOGUE_TEMPERATURE = 0.9`.
- Produces: `_dialogue_temperature(slug: str) -> float` — deterministic per-resident temperature in [0.8, 1.0].

- [ ] **Step 1: Write the failing test**

```python
def test_dialogue_temperature_is_deterministic_and_varies():
    from worldgm.residents.agentic_brain import _dialogue_temperature
    a = _dialogue_temperature("dwight")
    b = _dialogue_temperature("lebron")
    assert _dialogue_temperature("dwight") == a   # stable across calls (hashlib, not salted hash)
    assert 0.8 <= a <= 1.0 and 0.8 <= b <= 1.0
    assert a != b                                  # the cast doesn't share one temperature
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/residents/test_agentic_brain.py::test_dialogue_temperature_is_deterministic_and_varies -v`
Expected: FAIL (`_dialogue_temperature` not defined).

- [ ] **Step 3: Add the helper and use it**

At the top of `agentic_brain.py`, ensure `import hashlib` is present (add if missing). Add near the dialogue constants:
```python
def _dialogue_temperature(slug: str) -> float:
    """Stable per-resident temperature in [0.8, 1.0], so the cast doesn't collapse
    to one phrasing distribution. hashlib (not builtin hash) → stable across procs."""
    h = int(hashlib.md5(slug.encode()).hexdigest(), 16)
    return round(0.8 + (h % 5) * 0.05, 2)  # 0.80, 0.85, 0.90, 0.95, 1.00
```

In `generate_dialogue`, replace the `temperature=_DIALOGUE_TEMPERATURE` argument (line 570) with:
```python
                model.sample_text, prompt, max_tokens=_DIALOGUE_MAX_TOKENS,
                temperature=_dialogue_temperature(ctx.name),
```

- [ ] **Step 4: Add the anti-tic line**

In `build_dialogue_prompt`, append before the final instruction line:
```python
    lines.append("Don't restate your last point or reach for your usual catchphrases — move it forward or land it.")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/worldgm/residents/test_agentic_brain.py -v`
Expected: PASS.

- [ ] **Step 6: Full worldgm suite + lint + commit**

```bash
cd apps/backend && uv run pytest tests/unit/worldgm/ -q && uv run ruff check worldgm/
git add apps/backend/worldgm/residents/agentic_brain.py apps/backend/tests/unit/worldgm/residents/test_agentic_brain.py
git commit -m "feat(worldgm): per-persona dialogue temperature + anti-tic line — anti-convergence

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Validation (after all tasks)

1. `cd apps/backend && uv run pytest tests/unit/worldgm/ tests/unit/services/test_conversation_service.py -q` — all green.
2. `uv run ruff check apps/backend/worldgm apps/backend/core/services/conversation_service.py` — clean.
3. Deploy the branch to **dev** worldgm; let it run ~1h; pull `goosetown-dev-conversations` and confirm against the §7 metrics in the spec: median turn count drops (target ≤ ~6), visible disagreement appears, no catchphrase spam, no invented entities/projects.
4. Baseline the same metrics on current prod *before* promoting, so the delta is measurable.

## Out of scope (tracked, not built here)

- **Goal-conditioned shared conversation intent** (stored on the conversation, surfaced to both residents) — Phase 1.5, its own spec/plan (crosses backend↔CLI-daemon boundary).
- **Verbalized Sampling** (k-candidate dialogue sampling) — held; revisit if convos still feel same-y (raises NovaPro cost).
- **Phase 2** (persistent memory + reflection) and **Phase 3** (town-level culture) — separate specs.
- **Branch↔main reconciliation** (prod runs unmerged `feat/resident-cast-bibles`) — launch-hygiene decision, not a code task here.
