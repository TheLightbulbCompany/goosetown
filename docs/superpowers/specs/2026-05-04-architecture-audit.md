# 2026-05-04 — GooseTown Architecture Audit

**Lenses:** mattpocock's *improve-codebase-architecture* (depth / seams / adapters /
deletion-test) + Pragmatic Programmer (DRY / orthogonality / DBC / tell-don't-ask /
no programming-by-coincidence).

**Scope:** the post-sub-project-#3 backend (`apps/backend/core/services/` +
`apps/backend/routers/town.py`), the GooseTown CLI daemon
(`apps/goosetown-cli/src/goosetown/daemon.py` + the bundled twin under
`apps/backend/data/goosetown-skill/`), the Godot game-server scripts
(`apps/godot/scripts/server/`), and the React frontend.

**Method:** read the project glossary in `CLAUDE.md` first (no formal
`CONTEXT.md`/`docs/adr/` exists — see §3). Walked the modules above looking for
shallow-interface friction, repeated-knowledge surfaces, and silent-coincidence
seams. Code references are absolute paths.

This is **audit only** — no interfaces are proposed and nothing is fixed. The
purpose is to surface candidates and rank them.

---

## Section 1 — Deepening candidates

Twelve candidates. Each names files + line ranges, identifies what is shallow or
duplicated, sketches a deepening direction in plain English, and explains the
locality + leverage win.

### 1. `TownDaemon` — 30+ event handlers writing the same dict

**Files**
- `apps/goosetown-cli/src/goosetown/daemon.py:131-1097` (the class)
- duplicated in `apps/backend/data/goosetown-skill/skills/goosetown/goosetown_cli.py:916-2480`

**Problem**

`TownDaemon` is a 1,300-line class with **17 distinct `_on_*` handlers** plus
`_handle_event`, `_handle_world_update`, `_handle_agent_state`,
`_flush_owner_messages`, `_check_alarm`, `_keepalive_loop`, `connect_ws`,
`listen_ws`, `_handle_socket_client`, `run_socket_server`, `handle_tool_command`
and `run`. Each `_on_*` handler does a small, similar thing: pluck fields from
`data: dict`, mutate `self.state`, append to `self._events`, call `_write_state`
or `_write_status`.

Apply the **deletion test**: deleting any single handler doesn't move
complexity — it would just re-appear inline at the dispatch site
(`_handle_event:232-303`). The handlers don't *hide* anything from the dispatcher;
they're just where the dispatcher's `elif` body got moved to. That is the
canonical signature of a shallow surface.

The interface this module presents to `daemon_main` is conceptually small:
*"connect to a WebSocket, render `TOWN_STATUS.md` reflecting the latest world
state, accept tool commands via Unix socket, settle move/chat futures."* The
implementation is huge. The gap between the two is shallowness.

The shallowness is also leaking through the interface: the recently-added
`_chat_outcome` future, `_move_outcome` future, `_pending_move_destination`,
`_arrived_event`, `_initial_state_event`, `_owner_messages_buffer`,
`_pending_owner_messages`, `_owner_flush_handle`, `_prev_mood`, `_prev_energy`,
`_prev_nearby`, `_last_think_time`, `_events`, `_max_events`, `self.running`,
`self.state`, `self.ws` (instance fields, `__init__:147-190`) are *all* visible
to every handler because they all live on `self`. There is no internal seam.

Specific knot: `_handle_world_update:831-1097` is **267 lines** and switches on
`data["think"]`, conversation state, location_context, owner messages, nearby
agents, nearby conversations, available activities, and writes one of four
different `TOWN_STATUS.md` markdown templates. That's not a function, that's a
view layer with a fistful of state.

**Solution**

Two seams worth considering, both inside the daemon module:

1. **A pure state-folder** that takes `(prev_state, event) -> next_state`, with
   no I/O. The 17 handlers become one match statement returning a new state. The
   instance fields shrink to `state` plus a small "outcome-future registry"
   (chat/move/arrived).

2. **A `TownStatusRenderer`** that takes `(state, recent_events)` and returns
   the `TOWN_STATUS.md` string. Today's rendering logic is duplicated across
   `_on_connected:305-351`, `_on_sprite_ready:352-366`,
   `_on_context_changed:582-634`, `_on_wake:710-725`, and the giant
   `_handle_world_update`. Each of those builds a fresh markdown blob with very
   similar fragments: location, activity, mood, energy, "Where you can go",
   "What you can do here". A renderer with a small set of inputs would
   concentrate that knowledge.

**Benefits**

- *Locality:* the rules of "when does the daemon set
  `state['agent']['activity'] = 'idle'`" today live in 6 separate handlers
  (`_on_activity_complete`, `_on_activity_interrupted`, `_on_context_changed`,
  `_on_arrived`, `_handle_agent_state`, `_handle_world_update`). They'd live in
  one place.
- *Leverage:* the marker-normalization quirk for `player_spawn`/`exit` is
  applied in **three different handlers** (`_on_arrived:672-708`,
  `_on_move_rejected:636-670`, `_handle_world_update:850-859`) with the same
  3-clause condition. A pure folder applies it once.
- *Tests:* today's tests in `tests/unit/test_goosetown_cli.py` instantiate the
  full class and assert on file contents, scrubbing for substrings. With a pure
  folder, tests become `assert reduce(events, initial_state) == expected_state`
  and the renderer becomes `assert render(state) == "...md..."` — both pure,
  fast, no asyncio, no monkeypatching of `Path.write_text`.

**Effort:** large. ~1,300 lines and the live PyPI package shape.

---

### 2. `town_service.update_agent_state(**kwargs)` — the `field_map` dict is the smell

**Files**
- `apps/backend/core/services/town_service.py:345-406` (the method)
- `field_map`: `town_service.py:358-375` (16 entries today, growing)
- Call sites scattered across `sprite_pipeline.py`, `agent_connect_service.py`,
  `daemon_bridge.py`, `context_transition_service.py`, `activity_service.py`,
  `routers/town.py`.

**Problem**

`update_agent_state(agent_id, **kwargs)` is a keyword-bag CRUD that does
snake_case → camelCase translation through a hand-maintained `field_map` dict.
Run the **deletion test**: deleting it would push the same `update_item` SQL +
the same case translation out to 30+ call sites. So it's earning its keep — but
its interface is a string dict with no schema, and the implementation is a
miniature ORM written by hand.

The smell is the field_map itself. There are **two parallel naming systems**:

- snake_case Python kwargs (`sprite_url`, `current_location`, `onboarding_phase`)
- camelCase DynamoDB attributes (`spriteUrl`, `currentLocation`, `onboardingPhase`)

Two places must change together when a new field is added: the dataclass-y
domain field, and `field_map`. That's a textbook DRY (knowledge) violation.

Worse: the special case at `town_service.py:352-355` auto-bumps
`spriteUpdatedAt` whenever `sprite_url` changes — a write-side invariant that
exists nowhere in the *interface* of the method. A caller who passes
`sprite_url=...` has no way to know they're also writing `spriteUpdatedAt`. That
is a Design-by-Contract gap encoded in the implementation.

The kwargs surface also hides which fields are *legal* for an agent record. New
callers can pass `update_agent_state(agent_id, foo=42)` and it'll happily
`SET foo = :v0` into DynamoDB. The interface is more permissive than the actual
schema.

**Solution**

The sub-fields cluster naturally. *Sprite progress* (`sprite_status`,
`sprite_ready`, `sprite_url`, `sprite_updated_at`, `sprite_animations`,
`pixellab_character_id`, `sprite_current_template`, `onboarding_phase`) is one
cluster. *Connection state* (`daemon_connection_id`, `last_heartbeat_at`) is
another. *Context* (`location_context`, `current_location`, already extracted
into `update_agent_context`) is a third. *Mood/energy* (already extracted into
`update_agent_mood_energy`).

A deeper interface is one method per cluster, each taking a typed value object,
not `**kwargs`. The case-translation stops being interface — it's a private
detail of how the agents table happens to be modelled.

**Benefits**

- *Locality:* the "when sprite_url changes, also bump spriteUpdatedAt" rule
  becomes a private detail of the sprite-progress writer; callers can't break
  it by forgetting.
- *Leverage:* a typed `SpriteProgress` value object documents what callers can
  set, and the camelCase mapping becomes implementation, not interface.
- *Tests:* tests stop asserting `field_map["onboarding_phase"] == "onboardingPhase"`
  (`tests/unit/services/test_town_service.py:200`) — that test is testing the
  implementation. Tests on the typed-cluster interfaces describe behaviour
  ("after `mark_sprite_ready(...)`, `agent.spriteReady` is `True` and
  `spriteUpdatedAt` is fresh"), which survives any internal renaming.

**Effort:** medium. The seams already cluster cleanly; the change is mostly
mechanical extraction of named writers + their tests.

---

### 3. `onboardingPhase` writes — five sites, one truth

**Files**
- `apps/backend/core/services/sprite_pipeline.py:144-149, 264-266, 310-312, 446-447`
- `apps/backend/core/services/agent_connect_service.py:215`
- `apps/backend/main.py:115`

**Problem**

`onboardingPhase` is set in **six places** across two files, with a parallel
in-memory cache (`AgentConnection.onboarding_phase`,
`town_agent_ws.py:38, 129-136`) that *also* must be updated alongside every DB
write. The current pattern at every write site is:

```python
service.update_agent_state(agent_id, onboarding_phase="alive")
get_town_agent_ws_manager().update_onboarding_phase(str(agent_id), "alive")
```

(`sprite_pipeline.py:264-266, 310-312`)

This is the pattern the producer's launch-readiness plan flagged as the "phase
race." It's two places that must change together, with no compiler help — if
someone adds a new transition and forgets the cache mirror, the
`agent_state` broadcast to the daemon will report a stale phase for one full
broadcast cycle.

The legal phase transitions are also implicit. The valid lattice is:

```
(none) -> creating -> static -> alive
                            \-> alive  (skip path when no rotations)
```

…but nothing in the code says so. Any string can be written; downstream code
reads the string and dispatches off it (`daemon_bridge.py:122`).

**Deletion test:** delete the `update_onboarding_phase` cache mirror. The
hot-path `_send_to_daemon:117-122` still works — it just does a
`get_town_agent_by_id` lookup per `agent_state` event (the original behaviour
before PR #181). So the mirror is purely a perf cache. That's fine, but it's
*coupled to every phase write* by convention, not by code structure. That's
incidental coupling.

**Solution**

A small **OnboardingPhase module** owning the lattice. Interface is the legal
transitions: `start_creating(agent_id)`, `mark_static_ready(agent_id, ...)`,
`mark_alive(agent_id)`. The module is responsible for both the DB write and
the cache mirror; callers can't forget either.

A neat side effect: the lattice becomes the test surface. Today there's no
single test for "you can't go back from `alive` to `creating`," because there's
no module to test it on.

**Benefits**

- *Locality:* the cache invariant ("if DB says X, cache says X") concentrates
  in one module.
- *Leverage:* a single phase change is one call instead of two; the phase
  lattice is enforceable.
- *Tests:* a tiny state-machine test covers transitions; existing tests can
  continue to assert observable side effects.

**Effort:** small. ~50 lines of new module, ~6 mechanical call-site
substitutions.

---

### 4. The two CLIs (`apps/goosetown-cli/` and the bundled `goosetown_cli.py`)

**Files**
- `apps/goosetown-cli/src/goosetown/` (package, ~1500 lines daemon)
- `apps/backend/data/goosetown-skill/skills/goosetown/goosetown_cli.py` (single file, 2533 lines)

**Problem**

These two implementations of the same daemon **drift continuously**. The
package version is the modular one (`daemon.py`, `core.py`, `cli.py`,
`activities.py`, `config.py`, `endpoints.py`, `ipc.py`); the bundle is a
single-file monolith. `diff` between just `daemon.py` and the daemon part of
the bundle is 1979 lines (basically every line differs, mostly because the
bundle's still has imports inlined). They're "kept in sync manually."

This is the Pragmatic Programmer DRY rule applied to the *knowledge of
"what the daemon does."* Two artifacts encode it. Add a feature
(`_chat_outcome` future, marker-normalisation, owner-message
buffering, `render_mood`/`render_activity` helpers — see daemon.py:64-101)
and you must update both. CLAUDE.md and the comment at `daemon.py:65-70`
explicitly admit this:

> Mirrors `apps/backend/core/services/town_mood_engine.mood_label`. Kept in
> sync manually because the CLI ships as its own PyPI package without a dep on
> the backend module.

Three independent strands of duplicated knowledge (mood_label thresholds,
location/activity registry, daemon protocol).

**Deletion test:** delete the bundle. Complexity vanishes — the legacy
`/town/skill/bundle` endpoint becomes stub-or-redirect; the canonical install
paths (`pip install goosetown` and the plugin marketplace) remain. CLAUDE.md
already says the legacy bundle "sunsets in sub 4."

**Solution**

This is barely a deepening question — it's an "execute the existing plan"
question. The bundle exists for one reason (legacy install path during the
public-launch transition), and the plan is to delete it. Until then, generate
the bundle from the package at build time so the duplication can't drift.

**Benefits**

- *Locality:* one canonical TownDaemon, one set of mood thresholds, one set of
  activity definitions.
- *Leverage:* a fix like "marker normalization" in PR #N hits both CLIs the
  same day.
- *Tests:* `tests/unit/test_goosetown_cli.py` today imports from a copy under
  `apps/backend/test-workspace-phoebe/goosetown_cli.py` (`grep` shows references
  at `:1287-1340`). Removing the bundle removes that test workspace too.

**Effort:** medium. The PyPI package already exists. Either generate the bundle
or sunset it.

---

### 5. `ManagementApiClient()` instantiation + `mgmt.send_message(connection_id, {payload...})` — repeated 30+ times

**Files**
- `apps/backend/core/services/management_api_client.py` (the client)
- 19 instantiation sites + 32 method calls across:
  `conversation_service.py`, `agent_connect_service.py`, `daemon_bridge.py`,
  `context_transition_service.py`, `activity_service.py`, `routers/town.py`

**Problem**

Almost every handler in the codebase does:

```python
mgmt = ManagementApiClient()
try:
    mgmt.send_message(conn_id, {"event": "...", ...})
except Exception as exc:
    logger.warning("Failed to send X to Y: %s", ..., exc)
```

19 places repeat this exact envelope. Add the `_reject` closures inside
each handler in `conversation_service.py:176-184, 414-421, 539-546, 652-659`,
`activity_service.py:78-85`, `context_transition_service.py:151-159` and you
have ~25 more boilerplate copies of "instantiate, send, log-warn-on-fail."

Run the deletion test on `ManagementApiClient`: deleting it pushes
`boto3.client("apigatewaymanagementapi", ...)` into 19 places. So the client
is earning its keep. But the *use site* is shallow — the
client is a thin transport, not a vocabulary.

The vocabulary the codebase actually uses is **events to specific recipients**:
"send `chat_rejected` to the initiator", "send `conversation_invite` to the
target", "broadcast `agent_context_changed` to all viewers." Today every
caller has to know:

1. Look up the connection_id (from `agent_id` via `ws_manager` — sometimes —
   or from the request).
2. Construct the envelope.
3. Catch and log the failure.
4. Decide whether to keep going on failure (cosmetic) or bail.

That's the same 3-4-step protocol stamped into every handler.

`_send_to_daemon` (`daemon_bridge.py:68-147`) is the closest existing answer —
it takes `(agent_id, payload)` and does the lookup + send + log. But it's only
used for the few hot-path events; the conversation/activity handlers all still
call `mgmt.send_message` directly. That's a hypothetical seam (one adapter)
that should be a real one (drive every event-to-agent through it).

**Solution**

A small **DaemonMailbox** (or whatever the team chooses to call it) at the
seam *agent_id → daemon WebSocket*. Interface is event-shaped:
`mailbox.notify(agent_id, event)`. The client + connection lookup + log-warn
becomes implementation. The shallow envelope-stamping disappears.

For viewer broadcasts, a `ViewerBroadcast` peer module already exists in spirit
(`viewer_relay.py`, 157 lines) but `context_transition_service.py:266-292`
still iterates `relay._viewer_connections` directly and calls `mgmt.send_message`
in the loop. That's accessing private state and re-implementing the broadcast
loop — Law-of-Demeter violation; see §2.

**Benefits**

- *Locality:* "what happens when an API Gateway connection is gone" lives in
  one place.
- *Leverage:* every new event handler gets the gone-connection / log-warn
  behaviour for free.
- *Tests:* the per-handler tests today patch `MGMT_PATH = "core.services.management_api_client.ManagementApiClient"`
  (`tests/unit/test_agent_activity_handler.py:27`) — replacing those patches
  with an in-memory `DaemonMailbox` adapter is the textbook port-and-adapter
  win.

**Effort:** medium. ~30 call sites is mechanical; the design choice is whether
events get typed or stay as dicts (probably stay-as-dicts for the first cut).

---

### 6. The three Godot agent controllers (town / apartment / library)

**Files**
- `apps/godot/scripts/server/agent_controller.gd` (893 lines)
- `apps/godot/scripts/server/apartment_agent_controller.gd` (342 lines)
- `apps/godot/scripts/server/library_agent_controller.gd` (352 lines)

**Problem**

Three GDScript classes. Each has its own copy of:

- `spawn_agent` (with the same dedupe-by-agent_id, the same TILE_SIZE math)
- `remove_agent`
- `handle_command` (move/idle/get_locations dispatch)
- `_find_location`
- `_get_agent_location`
- `_check_arrivals`
- `_broadcast_agent_states`
- `get_world_state_for_viewer`
- the same constants (`TILE_SIZE = 32.0`, `STATE_INTERVAL = 2.0`,
  `DESTINATION_JITTER = 6.0`, `SPEECH_DURATION = 5.0`)
- the same per-peer maps (`_agents`, `_peer_to_id`, `_id_to_peer`,
  `_arrived_at`, `_agent_speech`, `_speech_timers`, `_state_timer`)

`agent_controller.gd` adds proximity detection + conversation layout (the
public-space features); `apartment_agent_controller.gd` adds a `user_id` field
(per-tenant isolation); `library_agent_controller.gd` adds `_peer_to_user`
(shared interior with mixed ownership). Beyond that, the cores are 80%
identical.

Compare the file headers (`apartment_agent_controller.gd:1-4`):
> Same interface as AgentController but for apartment locations.

That comment says it out loud: *same interface, three implementations*. That's
the Pragmatic-Programmer Orthogonality break — every change to a per-peer map
or a TILE_SIZE-related calculation has to be mirrored across three files.

**Deletion test:** delete `apartment_agent_controller.gd` or
`library_agent_controller.gd`. Complexity reappears in two files (the other
two), not concentrated. That's the signature of "we have one concept here, we
just spelled it three times."

**Solution**

A base `AgentControllerBase` class with the common per-peer bookkeeping +
move/idle/spawn/remove/state-broadcast loop, plus three thin subclasses that
override:

- whether proximity is checked (town: yes; apartment, library: no)
- whether `user_id` is per-instance (apartment: yes) or per-peer (library: yes)
  or absent (town: not used)
- the spawn fallback position when no marker is found

GDScript supports inheritance and the existing `class_name`/`extends`
keywords already lay the seam.

**Benefits**

- *Locality:* changes to the per-peer state-broadcast cadence, dedupe rule,
  facing logic, etc. happen once.
- *Leverage:* adding a fourth interior (e.g. cafe basement, marketplace) is
  ~50 lines of subclass instead of ~350 lines of copy-paste.
- *Tests:* GDUnit tests on the base class cover the shared behaviour; the
  subclass tests cover the differences.

**Effort:** medium-to-large for the GDScript refactor itself. Critical Rules
in CLAUDE.md require shipping the regenerated `.pck` with any GDScript
change, so the merge has to be coordinated. But the leverage compounds.

---

### 7. Three "scheduled fire-once with cancel" registries

**Files**
- `apps/backend/core/services/agent_connect_service.py:48-102`
  (`_pending_sleep_tasks` / `_delayed_sleep` / `_schedule_sleep_grace` / `_cancel_sleep_grace`)
- `apps/backend/core/services/conversation_service.py:93-153`
  (`_inactivity_tasks` / `_inactivity_timeout` / `_schedule_inactivity_timeout` / `_cancel_inactivity_timeout`)
- `apps/backend/core/services/activity_service.py:47-190`
  (`_activity_tasks` / `_activity_timer` / set-membership cleanup)

**Problem**

Three module-level dicts (or sets) hold strong refs to in-flight `asyncio.Task`
objects. Each implements the same protocol:

1. Schedule: cancel any existing task for this key, create a new task, store
   in registry.
2. The task body sleeps for a duration, then does some work, then pops itself
   from the registry in `finally`.
3. Cancel from outside: pop, cancel, idempotent.

The exact implementations differ slightly — `_pending_sleep_tasks` is keyed by
`agent_id`, `_inactivity_tasks` by `conv_id`, `_activity_tasks` is just a set
(no key, used purely for GC anchor). All three independently re-discovered
that you must pop in the task's own `finally` block (commented at
`agent_connect_service.py:77-79` and `conversation_service.py:108-112`).

That's three solutions to the same problem, with the *same gotchas* solved
slightly differently. New scheduled tasks that get added (e.g. quota-cooldown,
custom-animation-warmup) will re-discover the same gotchas.

**Solution**

A small `KeyedTaskRegistry` module: takes a key, a coro factory, and a
duration; provides `schedule`, `cancel`, and a clean shutdown. Internal seam,
not exposed at the service interfaces. The "task pops itself in finally"
detail becomes a property of the module.

**Benefits**

- *Locality:* the asyncio gotcha lives in one place.
- *Leverage:* future timers (and there will be more — see CLAUDE.md activity
  roadmap) get the discipline for free.
- *Tests:* a single test for the registry covers cancel-mid-sleep, race-on-
  reschedule, finally-pop semantics. The service-level tests stop monkeypatching
  the registries directly (see `conversation_service.py:1282-1320` test
  patterns).

**Effort:** small. ~80 lines, three call-site rewrites.

---

### 8. Conversation lifecycle is split between five places that all keep state

**Files**
- `apps/backend/core/services/conversation_service.py` (handlers + finalize)
- `apps/backend/core/services/town_service.py:744-1085` (DynamoDB CRUD)
- `apps/backend/core/services/town_agent_ws.py:138-146` (`update_conversation` cache)
- `apps/backend/core/services/daemon_bridge.py:118-132` (reads
  `agent_conn.current_conversation_id`)
- the daemon: `daemon.py:375-547` (`_on_conversation_*` handlers)

**Problem**

The lifecycle of a conversation has **five storages** that must agree:

1. The `conversations` DynamoDB table (`status`, `participants`, `publicLog`,
   `lastSpeakerId`, `turnCount`, `endedAt`).
2. The `agents` DynamoDB table (`currentConversationId`,
   `lastConversationAt`).
3. The in-memory `AgentConnection.current_conversation_id` cache.
4. The in-memory `_inactivity_tasks` registry.
5. The daemon's local `state["active_conversation"]` and `pending_messages`.

A new turn (`say`) writes to (1) and triggers a re-schedule of (4) and
broadcasts to (5). A `chat` writes to (1) and (2), updates (3) for both
participants, schedules (4), and broadcasts to (5). An `end_conversation`
updates (1), (2), (3), cancels (4), broadcasts to (5). A turn-limit auto-end
or silence auto-end goes through `_finalize_conversation` which also writes to
(1), (2), (3) and broadcasts to (5). Each handler also does the
"clear talking activity / clear speech bubble" cosmetic cleanup against the
game server (`conversation_service.py:838-851`).

That is **five storages × four handlers (chat / say / end / finalize) =
twenty pairings** all of which must stay in sync. The current code does sync
them — but the synchronization rule is "the handler that originated the
mutation knows to update all five." Adding a new handler (e.g. `pause`,
`whisper`, `thread`) means re-discovering this rule.

The **interface is the test surface** rule fails here. Tests at
`tests/unit/test_conversation_handlers.py` check that "after `_handle_chat`,
the DB record looks like X *and* the ws_manager mirror has Y *and* the
inactivity registry has a task with Z." The test surface is the *implementation
surface*.

**Solution**

A single `ConversationLifecycle` module that owns *all five storages* behind a
narrow interface: `start(initiator, target, opening_msg, location) -> Conv`,
`turn(conv_id, speaker, message) -> Conv`,
`join(conv_id, joiner) -> Conv`, `leave(conv_id, leaver) -> Conv`,
`finalize(conv_id, reason) -> Conv`. Internally, this module is the only thing
that touches the four backend storages; the daemon's local state is reconciled
by listening to broadcast events the module emits.

The big change is *not* combining handlers — it's making the storages the
private detail of one module, instead of shared writeable state across many.

**Benefits**

- *Locality:* the "five storages" rule becomes "the lifecycle module's
  invariants."
- *Leverage:* a new lifecycle event (`pause`, `whisper`, group rename) is one
  method on one module.
- *Tests:* tests assert observable lifecycle outcomes through the interface
  ("after `start`, both participants' next `agent_state` shows them in this
  conv"), not the storage layout.

**Effort:** large. This is the most consequential candidate (high leverage,
deep interface) but also the most invasive — it consolidates work that just
got extracted into `conversation_service.py` and would touch
`town_service.py`'s conversation methods + `town_agent_ws.py`'s cache fields.

---

### 9. `routers/town.py` — re-export aliases hide imports tests still rely on

**Files**
- `apps/backend/routers/town.py:55-60, 1456-1501` (the alias-import blocks)
- `tests/unit/test_*` (callers of `routers.town._send_to_daemon`,
  `routers.town._handle_chat`, `routers.town._handle_context_transition`, …)

**Problem**

The router file ends with three blocks of the form:

```python
# These imports keep the dispatch ladder above calling its old names AND
# preserve `from routers.town import _handle_chat` for tests and any external
# caller. Sub-project #3 extraction, 2026-04-22.
from core.services.activity_service import (...)
from core.services.context_transition_service import (...)
from core.services.conversation_service import (...)
```

The sub-project #3 decomposition moved logic into services but kept the
alias-import block to avoid breaking tests that patch
`routers.town._handle_chat` and friends.

That is **programming by coincidence**: the test infrastructure pins to the
import location (`routers.town._handle_chat`) but the implementation lives in
`core.services.conversation_service`. The function object is the same; the
patch path is incidental. If a test patches `routers.town._handle_chat`, the
production caller (which now imports from `core.services.conversation_service`
directly) is unaffected by the patch — the test would silently no-op.

This is exactly what the mattpocock skill calls "tests pinned to the
implementation location, not the interface." It's a tax on every future
extraction.

**Solution**

Migrate the tests off `routers.town` patch paths to the
`core.services.<module>` patch paths (or, better, off `patch(<string-path>)`
entirely — see candidate 11 below). Once nothing imports the legacy names from
`routers.town`, delete the alias-import block.

This isn't a "deepening" in the strict sense — it's debt cleanup from the
extraction. But it's blocking honest tests of the new modules.

**Benefits**

- *Locality:* the function lives in one place; tests target that place.
- *Leverage:* future extractions don't need a parallel re-export block.
- *Tests:* tests describe the interface (e.g. via in-memory adapters from
  candidate 5 / 11), not the import graph.

**Effort:** medium. ~50 patch-path constants across the unit-test suite.

---

### 10. `_handle_world_update` reaches into too many state corners

**Files**
- `apps/goosetown-cli/src/goosetown/daemon.py:831-1097`

**Problem**

This is the daemon's most-called function (every WebSocket
`world_state`/`agent_state` tick) and it currently does:

- `_MARKER_NORMALIZE` correction on `data["you"]["location"]` (lines 850-859)
- merge new agent into `self.state["agent"]`
- merge `nearby_agents` into `self.state["nearby"]`
- write `state.json`
- compute formatted `nearby_text`
- format `pending_messages` into markdown
- branch on `data.get("think")` — write think-prompt or static-status
- inside think branch, ask `state` whether there's an `active_conversation`
- inside think branch, format `pending_owner_messages` (mutating
  `self._pending_owner_messages` — a side effect inside a render path)
- inside think branch, use `LOCATION_ACTIVITIES.get(canonical_location(location))`
  to render available activities
- inside think branch, format nearby_chat_options
- inside think branch, format nearby_conv_block
- after the branch, compare `nearby_set` against `self._prev_nearby` and
  *append events* if it changed
- compare `mood`/`energy` against `self._prev_mood`/`self._prev_energy` and
  append events if changed

The function reads + writes `self.state`, `self._prev_nearby`, `self._prev_mood`,
`self._prev_energy`, `self._pending_owner_messages`, and writes
`TOWN_STATUS.md`, `TOWN_EVENTS.md`, and `state.json`. It's a mix of
*reconciliation* (apply the world update to local state), *event detection*
(diff against previous to emit "NEARBY changed"), and *rendering* (format the
markdown).

The `think` decision is itself a moving target: the function trusts what the
backend put in `data["think"]`, but `_handle_agent_state:773-825` *also*
computes its own `think` value from time-since-last-think + nearby-changed +
has-nearby-convs. Two independent thinking decisions converge here.

**Solution**

Three pure functions:

- `reconcile(state, world_update) -> next_state`
- `diff_events(prev_state, next_state) -> [event_string]`
- `render(state) -> markdown_str`

Plus a tiny driver that sequences them. The interface to the rest of the
daemon is one entry point per kind of incoming event.

**Benefits**

- *Locality:* the marker-normalisation rule is one function (today: 3 copies).
- *Leverage:* tests on `reconcile` and `render` are pure-function tests.
- *Tests:* `tests/unit/test_goosetown_cli.py` would shrink dramatically.

**Effort:** medium. Sub-candidate of #1 but worth listing separately because
it's the highest-traffic codepath in the daemon.

---

### 11. Tests pinned to string import paths

**Files**
- `apps/backend/tests/unit/test_agent_activity_handler.py:27-39`
- 52 `patch("core.services....")` and `patch("routers.town....")` call sites

**Problem**

The test file at `test_agent_activity_handler.py` defines:

```python
MGMT_PATH = "core.services.management_api_client.ManagementApiClient"
SERVICE_PATH = "core.services.activity_service.TownService"
GS_CLIENT_PATH = "core.services.activity_service.get_game_server_client"
GET_ACTIVITY_PATH = "core.services.activity_service.get_activity"
CONTEXT_MSG_PATH = "core.services.activity_registry.get_context_mismatch_message"
CANONICAL_LOCATION_PATH = "core.services.activity_registry.canonical_location"
TIMER_PATH = "core.services.activity_service._activity_timer"
```

The fact that the file *needs constants for the patch paths* is the smell. The
comment at lines 35-38 admits it:

> Both of the below are imported DEFERRED inside `_handle_agent_activity`,
> so we patch them at the source (activity_registry) rather than at the
> activity_service namespace. Keep this in mind when adding new deferred
> imports: the patch target follows the import style.

Every developer touching this file must know that "deferred import here, patch
at source" but "top-level import there, patch at the consumer namespace."
That's knowledge encoded in *test conventions*, not in the production
interface.

The deeper diagnosis: the production handlers reach for
`ManagementApiClient`, `TownService`, `get_game_server_client`, `get_activity`,
etc. via concrete imports. There is no seam at which a test could swap them
all wholesale. The tests cope by string-patching, and the path strings are the
only "interface" the test surface has.

**Solution**

This is the natural follow-on to candidates 5, 7, and 8. Each module that
today is `patch`-ed should instead be reachable through a port: a small
interface the handler accepts (constructor injection or argument). Production
wires the real adapter; tests wire an in-memory adapter.

For `ManagementApiClient`: the `DaemonMailbox` from candidate 5 already
naturally takes a port shape.

For `get_game_server_client` / `get_activity`: a small "GameServerPort" /
"ActivityRegistryPort" with two adapters (real, in-memory) does the same job.

The mattpocock skill's *one adapter = hypothetical seam, two adapters = real
seam* applies cleanly: the test adapter exists today (in the form of
monkeypatched mock objects), it's just not formalised. Formalising it deletes
the string-path constants.

**Benefits**

- *Locality:* tests describe interface contracts, not import graphs.
- *Leverage:* an extraction (move `_handle_agent_activity` to a different
  module) doesn't break tests.
- *Tests:* `tests/unit/test_agent_activity_handler.py:27-39`'s constant block
  goes away; assertions become "after I dispatched X to the handler with the
  in-memory registry, the registry saw Y," readable end-to-end.

**Effort:** medium. Drives candidates 5/7/8 too.

---

### 12. `_handle_context_transition` reaches into `viewer_relay`'s privates

**Files**
- `apps/backend/core/services/context_transition_service.py:266-292`
- `apps/backend/core/services/viewer_relay.py`

**Problem**

```python
from core.services.viewer_relay import get_viewer_relay
relay = get_viewer_relay()
for viewer_conn_id in list(relay._viewer_connections):
    try:
        mgmt.send_message(
            viewer_conn_id,
            {"event": "agent_context_changed", ...},
        )
```

The transition handler reaches into `relay._viewer_connections` (a private set)
and re-implements the broadcast loop. That's a Law-of-Demeter violation: ask
the relay to broadcast, don't ask the relay for its connection set and then do
the broadcast yourself.

The deletion test on `viewer_relay`: if you delete it, the broadcast loop
appears in three places (here, plus wherever speech-bubble broadcasts go,
plus wherever world-state broadcasts go). So it earns its keep — but its
interface today is *too narrow*: it owns the connection set but not the
broadcast.

**Solution**

Move the broadcast-to-all-viewers protocol into `viewer_relay.py`. Callers
say `relay.broadcast_to_viewers({"event": "...", ...})` and don't iterate.

Tell-don't-ask in its purest form.

**Benefits**

- *Locality:* "what to do when one viewer connection has dropped" is in one
  place.
- *Leverage:* future broadcast events (e.g. economy ticker, day-night change)
  call one method.
- *Tests:* relay-level tests cover the broadcast; transition-level tests stop
  mocking iterating-over-private-set.

**Effort:** small. ~20 lines.

---

## Section 2 — Pragmatic Programmer findings

Specific, file-anchored. Each finding is one sentence of "fix" — not a design
proposal.

### DRY (knowledge duplication)

- **`mood_label` thresholds duplicated.** Backend at
  `apps/backend/core/services/town_mood_engine.py` and CLI at
  `apps/goosetown-cli/src/goosetown/daemon.py:64-79`. The doc-comment admits the
  duplication. *Fix:* publish the thresholds from one source (the backend) via
  the `/town/skill` endpoint or a small shared package.

- **Daemon CLI duplicated** (candidate 4). Two ~1500-line implementations.
  *Fix:* generate the bundle from the package or sunset it.

- **`field_map` snake_case ↔ camelCase translation** at
  `apps/backend/core/services/town_service.py:358-375`. Two places must change
  when adding a field (the agent record schema in `opt_in:247-271` and the
  field_map). *Fix:* per-cluster typed writers (candidate 2).

- **`_MARKER_NORMALIZE` logic** in `daemon.py:636-708, 850-859` — three copies.
  *Fix:* one helper or one reconcile function (candidate 10).

- **`_reject(...)` closure** in
  `conversation_service.py:176-184, 414-421, 539-546, 652-659`,
  `activity_service.py:78-85`, `context_transition_service.py:151-159`. Six
  copies of "construct an event dict, send via mgmt, log on failure." *Fix:*
  a `DaemonMailbox.reject(connection_id, kind, reason)` (candidate 5).

- **DB write + ws_manager cache mirror** at six call sites for
  `update_conversation` (`conversation_service.py:284-285, 568, 688, 804`) and
  six for `update_onboarding_phase` (`sprite_pipeline.py:149, 266, 312`,
  `agent_connect_service.py:215`, `main.py:115`). *Fix:* the typed writers in
  candidate 2 own both, or extract the lattice helper (candidate 3).

- **Three KeyedTaskRegistry implementations** (candidate 7). *Fix:* one shared
  registry.

- **Three Godot agent controllers** (candidate 6). *Fix:* base class +
  thin subclasses.

### Orthogonality breaks

- **`AgentConnection` ws_manager cache fields** at
  `town_agent_ws.py:30-39` (`onboarding_phase`, `current_conversation_id`).
  These mirror DynamoDB fields and must be kept in lock-step. Every site that
  writes the DB also has to remember the cache write. *Fix:* candidates 2/3/8.

- **Conversation lifecycle's five storages** (candidate 8). A change to one
  storage's shape ripples to every handler.

- **`viewer_relay._viewer_connections` accessed from
  `context_transition_service`** (candidate 12). Coupling between modules
  that should be peer-level.

- **Bundled CLI's `apps/backend/test-workspace-phoebe/goosetown_cli.py` is a
  test-time copy of the bundled CLI** (`tests/unit/test_goosetown_cli.py:1287-1340`
  imports from it). A change to the daemon's `_handle_world_update` must
  ripple to *three* places (PyPI package, bundle, test workspace). *Fix:*
  candidate 4.

### Design-by-contract gaps

- **`update_agent_state` silently writes `spriteUpdatedAt` when caller passes
  `sprite_url`.** `town_service.py:352-355`. The interface doesn't say so. A
  caller intending only to test the URL value gets a clock-bump. *Fix:* the
  typed sprite-progress writer in candidate 2 makes this explicit.

- **`update_agent_state` accepts arbitrary keys.** `town_service.py:381` —
  unmapped keys fall through to `dynamo_key = field_map.get(key, key)`. A typo
  (`update_agent_state(agent_id, sprite_redy=True)`) writes a stray attribute
  to DynamoDB silently. *Fix:* validate kwargs against a known set, or replace
  with typed writers.

- **`onboardingPhase` is a free-form string with an implicit lattice**
  (candidate 3). `daemon_bridge.py:122` reads
  `agent_conn.onboarding_phase or "alive"` — anything goes. *Fix:* the phase
  module's `Literal["creating", "static", "alive"]` enforcement.

- **`get_nearby_conversations` `exclude_conv_id` field is None-tolerated.**
  `town_service.py:858`. Caller's intent ("don't include MY conv") is
  expressed via Optional, but the only doc is the parameter name. *Fix:*
  rename to `current_agent_conv_id` or split into two methods.

- **`endedAt = "_active"` sentinel.** `town_service.py:837`. A magic string
  used to satisfy a GSI key constraint. Leaks into every consumer that reads
  the field (e.g. `get_recent_conversations:776-795`). *Fix:* the lifecycle
  module hides it; consumers ask `conv.is_active()` instead.

- **`participants` vs `[participantAId, participantBId]` legacy fallback.**
  `town_service.py:873-877, 1008-1012, 1068-1072` and
  `conversation_service.py:440-444, 671-675, 798-802, 806-810`. Every reader
  has to know to fall back to the two-participant case. *Fix:* migrate the
  data once and remove the fallbacks; or hide behind a typed getter.

### Tell-don't-ask violations

- **`_handle_context_transition` reads
  `relay._viewer_connections`** (candidate 12). Tells the relay nothing; asks
  for a private set; performs the broadcast itself.

- **`_send_to_daemon` reads `agent_conn.onboarding_phase` and
  `agent_conn.current_conversation_id`** (`daemon_bridge.py:118-130`). The
  consumer enriches the payload itself — it's reaching into the connection's
  fields. Less egregious than the above (the cache exists *for* this caller)
  but still a coupling: any new field the daemon needs forces both a cache
  field on `AgentConnection` and a peek in `_send_to_daemon`. *Fix:* an
  `agent_conn.enrich_state_event(payload)` method does the same job locally.

- **CLI `handle_tool_command` mutates `self.state["pending_messages"] = []`
  inside the `say` branch.** `daemon.py:1298-1299`. The dispatch handler is
  reaching across into the daemon's state cache. *Fix:* a small `state.clear_pending()`
  on the state-folder module from candidate 1.

### Programming by coincidence

- **Tests patch `routers.town._handle_chat` even though the function lives in
  `core.services.conversation_service`** (candidate 9). The path is preserved
  by an alias-import. Tests that succeed today succeed because of an
  incidental fact about the module graph.

- **Two independent `think` decisions** at
  `daemon.py:773-825` (`_handle_agent_state`) compute one think value while
  `_handle_world_update:910` reads `data["think"]` (which the backend computed
  separately). The daemon's local computation runs only on `agent_state`
  events; the backend's computation runs on `world_state`. They don't meet —
  but a future refactor that introduces a third event type might assume they
  do. *Fix:* one place owns the think-prompt-frequency policy.

- **`_arrived_event` is set in BOTH `_on_arrived` and `_on_move_rejected`.**
  `daemon.py:669, 706`. The legacy `_arrived_event` is kept for "code paths
  still polling on it" (`_on_move_rejected:643-644`). New code uses
  `_move_outcome` (a future). Two parallel signalling channels for the same
  thing. *Fix:* delete `_arrived_event` once nothing reads it.

- **`participantAId / participantBId` plus `participants[]` plus the legacy
  fallback** (DBC entry above). The newer participants array exists *next to*
  the older A/B columns. Code has to remember which to read.

- **`spriteUrl` updates that don't bump `spriteUpdatedAt`** if the caller
  doesn't go through `update_agent_state`. `sprite_pipeline.py` always uses
  `update_agent_state`, but a future direct `update_item` would silently
  skip the bump. *Fix:* candidate 2.

---

## Section 3 — Cross-cutting concerns

### AI-navigability

I tried to "find" three things from cold start:

**The conversation lifecycle.** Started at `routers/town.py:1411-1418` (the
dispatch ladder). Followed the import to `core.services.conversation_service`.
Read `_handle_chat` → noticed `service.create_active_conversation` →
`town_service.py:797`. Then `service.append_message_to_conversation` →
`town_service.py:911`. Then `_finalize_conversation` →
`service.end_conversation` → `town_service.py:1040`. Then *the cache mirror*
at `ws_manager.update_conversation`. Then *the inactivity timer* at
`_inactivity_tasks`. Then *the daemon side* at `daemon.py:_on_conversation_*`.
Then *the chat bubbles* at `gs_client.send_command(..., "set_speech")`.

That's seven file-jumps to get a complete picture. **Verdict: not navigable.**
The conversation concept doesn't have a home; it has fragments in five files.
This is exactly the friction the deepening candidate 8 would resolve.

**The sprite pipeline.** Better. `sprite_pipeline.py:1-33` has an excellent
header comment listing the three phases, and the function bodies tell a clear
story top-to-bottom. The deepening candidate 3 (onboarding lattice) would
sharpen this further but the pipeline is currently the single best-organised
domain in the backend.

**The agent state machine.** Asked: "what fields move when an agent
transitions from `apartment` to `town`?" Found
`context_transition_service.py:117-294` quickly — the docstring at lines 1-37
is a model of clarity for the protocol. Three storage layers documented in
order. **Verdict: navigable.** This is what the conversation lifecycle should
look like.

The pattern: modules with *prose docstrings naming their invariants* are
navigable. Modules where the invariants are scattered across handlers are not.
Candidates 1, 2, 3, 8, 10 are all "concentrate the invariant somewhere a
reader can find."

### Should the project adopt `CONTEXT.md` and `docs/adr/`?

**Yes.** Both. Specifically:

- **`CONTEXT.md` (domain glossary)** — the project has a *de facto* glossary
  scattered across the CLAUDE.md "Architecture" section. Pulling it into a
  short canonical file would let the mattpocock skill (and future code review)
  reach for the right names. Candidate names that should appear: `Agent`,
  `Daemon`, `Game Server`, `Skill`, `World State`, `Agent State`,
  `Onboarding Phase`, `Conversation` (vs the *Conversation Record* — the DB
  row), `Activity`, `Context` (the `town`/`apartment`/`library` setting),
  `Marker` (Godot Marker2D vs the `_MARKER_NORMALIZE` artifacts), `Speech
  Bubble` (cosmetic) vs `Public Log` (canonical), `town_token`,
  `Daemon Mailbox` (if candidate 5 lands), `Lifecycle` (if candidate 8 lands).

- **`docs/adr/`** — the project has 20+ design specs in
  `docs/superpowers/specs/` already, but they are *evolving design proposals*
  not *decisions*. ADRs would record load-bearing choices like:
  - "Game server is thin — agents make decisions, not the server" (CLAUDE.md
    Critical Rules).
  - "PixelLab 4-direction endpoint only; 8-direction has bone_scaling bug."
  - "Backend is stateless except for the in-memory ws_manager cache and
    in-flight asyncio tasks."
  - "Tokens carry `iat` + `exp`; legacy tokens without `exp` accepted with a
    warning during the migration window" (PR #214).
  - "`endedAt = '_active'` sentinel because the GSI key cannot be NULL."

  Today these all live in either CLAUDE.md (which churns) or commit messages
  (which are hard to find). An ADR per decision is far easier for an LLM
  reviewer to consult — and lets the mattpocock-style audit avoid
  re-suggesting things that have already been deliberated. The skill's process
  step 1 specifically calls out "Read the project's domain glossary and any
  ADRs in the area you're touching first." There are none right now.

Recommend: start with `CONTEXT.md` (one afternoon) and seed `docs/adr/` with
the 5-6 load-bearing decisions above. Let the rest grow organically.

### Test architecture: pinned to implementation, or to the interface?

**Mostly pinned to implementation, with a few good seams.**

Strong points:
- `tests/unit/test_send_to_daemon_cache.py:37-48` builds an `AgentConnection`
  fixture and tests the cache invariants directly. That's clean.
- `tests/unit/test_relationship_atomicity.py` tests against an actual
  Dynamo-local table, asserting on observable affinity scores. That's a
  Local-Substitutable dependency category test, and it's well-shaped.
- `tests/unit/test_ws_models.py` tests the Pydantic boundary directly.

Weak points:
- The string-path patches (candidate 11). 52 instances. Each one couples a
  test to a specific import statement. The MGMT_PATH constants block in
  `test_agent_activity_handler.py:27-39` is the most explicit symptom; the
  comments at lines 35-38 ("the patch target follows the import style")
  describe the workaround as wisdom rather than as smell.
- `tests/unit/test_conversation_handlers.py:1282-1320` uses
  `monkeypatch.setattr(cs, "_INACTIVITY_TIMEOUT_SEC", 0.01)` and
  `monkeypatch.setattr(cs, "_finalize_conversation", _fake_finalize)`. That's
  testing past the interface — the test reaches into the conversation_service
  module to swap a constant + a private function in order to exercise
  `_inactivity_timeout`. *The interface is the test surface.* If
  `_finalize_conversation` is part of the test surface, it's not a private
  function; if it's private, the test shouldn't need to swap it.
- `tests/unit/test_goosetown_cli.py:54-89` instantiates the full
  `TownDaemon`, seeds `daemon.state["agent"] = {...}`, calls
  `_handle_world_update(world_data)`, then reads the markdown file off disk
  and string-matches. That's the inverse of "pure-function reconciliation +
  pure-function rendering" (candidate 1/10). The test surface is muddy
  because the production surface is muddy.

Net verdict: **the tests are tracking implementation, not interface.** Almost
every shallow-module candidate above (1, 2, 3, 5, 7, 8, 10) would simplify
its tests as a side effect of the deepening — the test architecture
improvement is downstream of the production architecture improvement.

---

## Section 4 — Recommended priority

Ranked by leverage (impact per unit of effort).

### Top 3 — do these in the next sprint

1. **Candidate 3 (`onboardingPhase` lattice).** Smallest of the high-leverage
   moves. Removes a known race the producer already flagged for launch-readiness
   work. Sets the pattern (typed phase + cache mirror in one place) that
   candidate 2 will then propagate.

2. **Candidate 5 (`DaemonMailbox`).** Touches more files than candidate 3
   but the per-site change is mechanical. Eliminates ~30 of the
   string-path patches in candidate 11 as a side effect. Sets the pattern
   (port-and-adapter at a stable seam) that candidate 11 will then complete.

3. **Candidate 1 + 10 together (TownDaemon split into reconcile / events /
   render).** Highest leverage on the daemon side, where every recent feature
   (`_chat_outcome`, `_move_outcome`, marker normalisation, owner messages)
   has had to thread through a 1300-line class. The pure-function refactor is
   internally invasive but *only* touches the daemon module — no API surface
   change, no backend change. Worth doing now because the daemon's growth
   curve is the steepest in the codebase.

### Good when you're in the area

4. **Candidate 7 (KeyedTaskRegistry).** Drop-in once you're already touching
   `agent_connect_service`, `conversation_service`, or `activity_service`.
   Self-contained.

5. **Candidate 12 (viewer_relay broadcast).** ~20 lines. Do it the next time
   anyone touches `context_transition_service`.

6. **Candidate 9 (router alias-import cleanup).** Do it as part of candidate 5
   or 11 — they push tests off `routers.town.*` patches anyway.

### Larger investments — when product priorities allow

7. **Candidate 2 (typed agent-state writers).** High leverage but touches
   every write site. Best done after candidate 3 has demonstrated the
   pattern.

8. **Candidate 4 (delete the bundled CLI / generate it).** Mostly mechanical,
   but sequencing matters — until the public-launch transition completes,
   the legacy bundle is on the contract. CLAUDE.md says "sunsets in sub 4."

9. **Candidate 11 (test ports & adapters).** High value but requires
   infrastructure design. Drives off candidates 5 / 7 / 8 — don't do it
   first.

10. **Candidate 6 (Godot controller base class).** Large GDScript refactor,
    coordinated with `.pck` regeneration + careful testing on the multi-map
    setup. Worth the effort but only after the apartment-navmesh blocker
    lands.

11. **Candidate 8 (`ConversationLifecycle` module).** The most consequential.
    Touches DynamoDB schema decisions, in-memory cache, daemon protocol,
    chat bubble cosmetics. Do this last and only after the smaller wins
    above have made the cost legible. **This is the candidate where I'd
    open an ADR before any work** — the seam choice (whether the lifecycle
    module owns the chat bubbles or stays a peer of the game server client)
    is a real architectural decision.

---

## Closing notes

- The post-sub-project-#3 service layout is *good*. The router went 3076 →
  1039 lines. Module headers are excellent. The remaining friction is the
  next layer down: storages-per-concept, registries-per-feature, and a few
  spots where extracted modules still talk through reach-into-privates.

- **No CONTEXT.md exists** and there are no ADRs. Both should be added — the
  glossary is one afternoon, the ADR seed is half a day. They make every
  future audit (this one, automated reviews, code-review skill output)
  qualitatively better.

- The two areas I did *not* cover deeply: the React frontend (small, mostly
  hooks-of-hooks; no obvious shallow-module pain visible from skim) and the
  PixelLab service (pipeline already in good shape). They'd merit a dedicated
  audit if and when they grow.

- I deliberately did not propose interfaces — that's step 2 of the
  mattpocock process, which the user's prompt explicitly excluded.
