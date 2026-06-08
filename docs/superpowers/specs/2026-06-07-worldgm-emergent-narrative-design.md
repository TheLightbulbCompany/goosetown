# World GM: Emergent, Data-Grounded Narrative — Design Spec

**Date:** 2026-06-07
**Status:** Design approved; pending implementation plan.

## Goal

Rework the World GM so every storyline it injects is grounded entirely in the real
world state — resident positions, personalities, moods, relationships, recent
conversations, activities — instead of inventing fiction. The GM stays a
*generative game-master* (it makes the town feel lively by injecting interesting
happenings), but those happenings always feature real residents, are delivered to
those residents so they respond, and react to how they responded. One shared
world, no fiction layer floating above it.

## Background / problem

The WORLD panel is driven by `worldgm/arc_orchestrator.py`. Today it picks a static
seed from `worldgm/arc_templates.py` (e.g. *"an overheard rumor spreads through the
plaza…"*) and feeds it **raw** to Bedrock with **no world context**. The model fills
the vacuum by inventing its own cast — "Anna the librarian", a newcomer "Clara", a
group conversation that never happened — completely decoupled from the actual
residents and the real conversations in the sidebar. Observed live 2026-06-07: the
WORLD panel narrated characters and a conversation that exist nowhere in the town.

PR #358 (`fix/ground-gm-narrative-in-real-residents`) is an interim grounding patch
that injects real witness names into the existing template prompts. This spec
**supersedes** that approach with an emergent, data-driven generator. #358 may merge
as a stop-gap; its `_witness_names` helper is reused here.

## Purpose of the World GM (the reframing)

The World GM is a **director / game-master**, not a chronicler. Its value is
*generative*: it injects storylines and events so the town has happenings — so
residents have something to react to and user-owned agents have something to walk
into. "Culture for the culture lab." A pure passive recap of real events would gut
the point. The fix is not to stop authoring storylines; it is to make every
storyline **grounded** in the real cast and **looped back** to them.

"Unified" means: the GM authors **from** the real world (who's here, how they feel,
who likes/dislikes whom, what they've discussed), the storyline features **those**
residents, it is **delivered** to them, and they respond in real conversations — and
the next beat reads that response.

## Architecture

Three units with clean, one-directional dependencies:

```
world_snapshot (reads real data, no LLM, no writes)
      ▲
narrative_director (wraps the LLM; snapshot+signals → injection | nothing)
      ▲
arc_orchestrator (lifecycle: persist / kickoff / beat / resolve + cast-targeted delivery)
```

Each is independently testable. Delivery/feedback reuse the existing arc rails
(`worldgm-arcs` DDB table, `emit_world_event` → WORLD panel, resident perception).

---

## Component 1 — `worldgm/world_snapshot.py` (new)

One job: turn live data into one structured `WorldSnapshot` per turn. No LLM, no
writes.

### Dataclasses

```python
@dataclass
class ResidentView:
    agent_id: str
    name: str            # displayName (fallback agentName)
    location: str        # currentLocation
    context: str         # "town" | "apartment"
    mood: int            # -50..50
    mood_label: str      # miserable..elated
    energy: int          # 0..100
    traits: list[str]    # personality traits
    persona: str         # one-line personalitySummary
    activity: str        # current activity or "idle"
    awake: bool

@dataclass
class RelationshipView:
    a: str               # resident name
    b: str               # resident name
    affinity: int        # -100..100
    rel_type: str        # acquaintance / friend / rival / ...

@dataclass
class ConversationView:
    participants: list[str]  # names
    location: str
    topic: str               # topicSummary or first-line snippet
    minutes_ago: int

@dataclass
class WorldSnapshot:
    residents: list[ResidentView]
    relationships: list[RelationshipView]   # RELEVANT pairs only (see below)
    recent_conversations: list[ConversationView]
    clusters: dict[str, list[str]]          # location -> co-located resident names
    hour_utc: int
```

### `build_snapshot(state) -> WorldSnapshot`

- **residents:** `state.town_service.get_active_agents()` → online residents only;
  derive `mood_label` via the existing mood-engine label helper.
- **relationships:** fetch only the **relevant pairs** — pairs that are co-located
  *or* have a recent conversation together — via `get_or_create_relationship`. This
  bounds the fetch (no N² table sweep) and keeps the prompt small.
- **recent_conversations:** the last ~8 conversations (existing recent-conversations
  query) → participants, location, topic snippet, minutes-ago.
- **clusters:** invert resident locations → `{location: [names]}` for shared town
  locations (apartments are private/per-user, so each apartment is its own cluster
  of that user's agents — not merged).
- **hour_utc:** current UTC hour.

Best-effort per source: a failed relationships query still yields residents +
conversations. Never raises; partial snapshot is acceptable.

### `compute_signals(snapshot) -> list[Signal]`

Deterministic, ranked hooks the LLM may riff on (free to use, combine, or ignore).

```python
@dataclass
class Signal:
    kind: str            # see taxonomy
    score: float         # interestingness rank
    cast: list[str]      # involved resident names
    location: str | None
    note: str            # human-readable hook for the prompt
```

| `kind` | Fires when | Notes |
|---|---|---|
| `rivalry` | affinity ≤ −30 | score boosted if the pair is co-located now |
| `bond` | affinity ≥ +30 | score boosted if co-located now |
| `cluster` | ≥3 residents at one non-home location | score scales with count |
| `mood_extreme` | a resident at mood ≤ −25 or ≥ +25 | score scales with magnitude |
| `topic_echo` | same topic across ≥2 recent conversations | emergent town theme |
| `loner` | an extrovert idle/alone | nudge opportunity |
| `reunion` | two residents with history (strong affinity / prior convo) now co-located | callback |

Rank by `score` desc; the orchestrator passes the **top ~5** into the prompt.

**v1 simplification (deliberate):** signals use *absolute thresholds + co-location*,
**not** hour-over-hour deltas. Deltas ("affinity fell 15 this hour") would require
persisting prior scores; deferred. v1 stays stateless.

`rivalry`/`bond`/`reunion` signals fire on a pair's affinity **regardless of
location** — so two non-co-located residents (e.g. each in their own apartment) can
still seed a storyline. Co-location only *boosts* score; it is never required.

---

## Component 2 — `worldgm/narrative_director.py` (new)

Wraps the LLM. Snapshot + signals → a structured injection, or nothing. Uses the
lighter Bedrock model (`model_lite`).

### Output type

```python
@dataclass
class Injection:
    narrative: str           # 2-3 vivid sentences
    cast: list[str]          # REAL resident names (validated)
    location: str | None     # the storyline's nominal location (for WORLD framing)
```

### `author_kickoff(snapshot, signals) -> Injection | None`

Prompt frames the model as the town's unseen game-master. It is handed the
serialized snapshot (residents + state, relevant relationships, recent
conversations, time) and the top ~5 signals as *optional hooks*. Hard rules in the
prompt:

- Only the listed residents exist. Never invent people, narrators, or visitors.
- Author one short, vivid happening (2–3 sentences) featuring specific real
  residents by name. It may build on a hook or simply suit the scene.
- A storyline cast may be **co-located or apart** — if apart, frame the thread as a
  letter, a rumor reaching both, a memory surfacing, etc.
- If nothing interesting fits right now, reply with the literal token `NOTHING`.

Output is **structured**, parsed into `LOCATION`, `CAST` (names), `NARRATIVE`. The
structured `CAST`/`LOCATION` is what makes delivery precise.

**Hallucination guard (centerpiece):** the parsed `CAST` is validated against the
snapshot's real resident names. Invented names are dropped. If the cast ends up
empty/invalid, the result is treated as `None` (no storyline). **A storyline cannot
exist with a non-real cast.**

### `author_beat(arc, snapshot) -> str | None`

Continues an active thread from the **fresh** snapshot focused on the arc's cast +
their current locations/state/new conversations. Returns the next beat text, or the
literal token `DONE` (→ `None`) when the thread has run its course. Same
"only real residents" rule.

---

## Component 3 — `worldgm/arc_orchestrator.py` (modified)

Keeps the arc DDB rows, kickoff→beat→resolve lifecycle, and `emit_world_event` →
WORLD panel. Changes:

### Storyline = cast-based (not location-based)

An arc row carries an explicit **`cast`** (list of agent IDs), `location` (nominal,
for framing), `narrativeKickoff`, beat fields, `status`, timestamps. Delivery is by
cast, not by location match (see Component 4).

### Generation source

Replace `_pick_template` + raw `template["kickoff_prompt"]` with:
`build_snapshot` → `compute_signals` → `narrative_director.author_kickoff`.
`_fire_beat` calls `author_beat` with a fresh snapshot instead of the generic
"continue this arc" prompt. `arc_templates.py` is removed.

### Trigger model — heartbeat + hooks

- **Active storyline present** → advance via beats on a **beat interval**
  (`WORLDGM_BEAT_INTERVAL_MIN`, default ~3–5 min — far faster than today's hourly
  beats so threads visibly develop). Durations shorten to ~15–45 min
  (`WORLDGM_STORYLINE_DURATION_MIN`) then resolve.
- **No storyline for an eligible cast** → each tick, build snapshot + signals; if a
  signal clears an "interesting-enough" bar (or a small random roll on a quiet
  town), author a kickoff. A **cooldown** (`WORLDGM_KICKOFF_COOLDOWN_MIN`, ~5–10
  min) throttles new kickoffs.

### Concurrency — per-agent + global rate cap (scales with population)

- **Each agent is in ≤1 active storyline at a time** (never recruited into a
  second). Enforced by reading active arcs' casts before kickoff.
- **Total concurrent storylines scale with population** — many small 2–4-person
  threads run in parallel, bounded only by agents not already cast. At 100 agents,
  potentially ~20–30 parallel micro-threads; at 20 agents, a handful.
- A **global rate cap** (`WORLDGM_MAX_KICKOFFS_PER_MIN`) limits new-storyline churn
  and LLM spend without limiting steady-state richness.
- Today's "only one active arc" kickoff guard is replaced by these rules.

**Deferred:** multiple *parallel* scenes at a single crowded location (split a busy
location into sub-clusters) — v1 lets one storyline span a cluster.

---

## Component 4 — Cast-targeted delivery + resident perception

### Emission

A kickoff/beat emits a world event with `witnesses = cast` (agent IDs). The
`emit_world_event` per-witness `personal.<agent_id>.observed` path already exists —
each cast member gets the beat regardless of location, including separate
apartments. The town-wide event (WORLD panel) also fires for the public feed.

### Resident perception (the `_tick_one_resident` change)

Today `_tick_one_resident` sets
`status["active_storyline"] = _arc_text_for_location(active_arcs, location)` — a
location match. Replace with `_storyline_text_for_agent(active_arcs, agent_id)`:
the resident perceives the storyline whose **cast contains their agent_id**,
regardless of where they are. This is what lets two apartment-bound residents share
a thread and both respond. The resident brain already reads `active_storyline` from
its status (no brain-logic change needed).

---

## Data flow — the unified loop

```
GM tick
  → build_snapshot(real data)        # positions, mood, relationships, conversations, activities
  → compute_signals                  # ranked hooks
  → if active arc for cast: author_beat(fresh snapshot)   # reacts to how cast responded
    else if hook clears bar (+cooldown, +per-agent free, +rate cap): author_kickoff
  → validate cast against real residents (drop hallucinations)
  → persist arc {cast, location, narrative, beats}
  → emit_world_event(witnesses=cast) → WORLD panel + per-cast delivery
  → each cast resident perceives via _storyline_text_for_agent → responds in real conversation
  → next tick: snapshot reflects the response (new conversation, affinity/mood shift) → beat reacts
```

## Cadence & tuning (env vars, all with sane defaults)

| Var | Default | Meaning |
|---|---|---|
| `WORLDGM_BEAT_INTERVAL_MIN` | ~4 | minutes between beats of an active storyline |
| `WORLDGM_STORYLINE_DURATION_MIN` | ~30 | storyline lifetime before forced resolve |
| `WORLDGM_KICKOFF_COOLDOWN_MIN` | ~7 | min gap before a new kickoff for a freed cast |
| `WORLDGM_MAX_KICKOFFS_PER_MIN` | ~2 | global new-storyline rate cap |
| `WORLDGM_SIGNAL_MIN_SCORE` | 1.5 | bar a signal must clear to seed a kickoff |

Signal scores are on an arbitrary positive scale fixed in `compute_signals`: base
1.0 per signal, +0.5–1.0 for co-location/magnitude boosts. A default
`WORLDGM_SIGNAL_MIN_SCORE = 1.5` admits a co-located rivalry/bond or a 3+ cluster
(boosted above base) while a lone low-magnitude signal stays below the bar. The
quiet-town random roll is the only path to a kickoff when no signal clears it.

## Error handling

- **Snapshot:** best-effort per source; partial snapshot acceptable; never raises.
- **LLM failure/timeout:** skip that kickoff/beat; log + metric; existing storylines
  continue; town gets no new injection that tick.
- **Malformed / hallucinated output:** cast validated against real names; invented
  names dropped; empty/invalid cast → treated as `NOTHING`/`DONE`. Structural fix
  for the "Anna the librarian" bug.
- **Delivery (per-witness emit):** best-effort, logged; residents reconcile next beat.

## Cost

- Per-tick *scan* is deterministic (`compute_signals`) — cheap. The LLM fires only
  on a cleared hook (or rare quiet-town roll) + one call per active beat.
- Rate cap + beat interval bound spend. Beats use `model_lite`. Snapshot serialized
  compactly: only relevant relationships, top ~5 signals, focal cast — not the whole
  town verbatim.

## Testing

- **`world_snapshot`:** assembles correctly from a mocked `town_service`
  (residents/relationships/conversations); `compute_signals` detects and ranks each
  signal kind; relationships limited to relevant pairs.
- **`narrative_director`:** stub LLM — prompt carries real names + signals + the
  "only these residents exist" rule; `NOTHING` → no injection; **hallucinated cast
  names rejected**; `LOCATION/CAST/NARRATIVE` parsed; `DONE` → resolve.
- **`arc_orchestrator`:** per-agent concurrency (agent already cast isn't recruited
  again); global rate cap; cast-targeted per-witness delivery; cadence/cooldown;
  existing lifecycle tests adapted to the new generation source.
- **Perception:** `_storyline_text_for_agent` returns the storyline whose cast
  contains the agent, regardless of location (incl. separate apartments).
- **Feedback:** a beat re-reads a fresh snapshot reflecting a new conversation.

## Scope

**Ships in v1:** `world_snapshot` + signals (absolute thresholds); `narrative_director`
(kickoff/beat + hallucination guard); cast-based storylines; per-agent concurrency +
global rate cap; cast-targeted delivery + `_storyline_text_for_agent`; faster tunable
cadence; WORLD panel via existing `emit_world_event`. Removes `arc_templates.py`;
supersedes/folds in PR #358's `_witness_names`.

**Defers:** affinity *deltas* (needs prior-score state); multiple parallel scenes at
one crowded location; rich artifact delivery (real letters/objects residents can
act on); goal-advanceability biasing (the old "Phase C5" note).
