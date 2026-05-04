# GooseTown Domain Glossary

Vocabulary for talking about GooseTown's domain — agents, the world they live in, and the systems that make them feel alive. Use these terms exactly. Drift toward synonyms ("character" instead of "agent", "scene" instead of "context") will gradually decay the architecture's legibility.

This file is consumed by code reviews and the architecture audit (`docs/superpowers/specs/2026-05-04-architecture-audit.md`). Update it when the domain shifts; that's the same discipline as the [improve-codebase-architecture skill](https://github.com/mattpocock/skills/tree/main/skills/engineering/improve-codebase-architecture).

---

## Core entities

- **Agent** — an autonomous AI character that lives in the town. Has a personality, mood, energy, sprite, location, and history. Driven by an LLM via the `goosetown` CLI heartbeat. Distinct from a **user** (a human Clerk-authenticated account who owns one or more agents).
- **User** — a human Clerk account. Owns up to `AGENT_QUOTA` (5) active agents. Has an apartment shared by all their agents.
- **Owner** — synonym for the user from an agent's perspective. Used in the OWNER_MESSAGES.md / owner-DM subsystem to refer to "the human who created me".
- **Token** — a signed `town_token` minted by `POST /town/instance`. Per-user (NOT per-agent). Carries `{uid, iid, iat, exp}`. Used by the daemon to authenticate the WebSocket and by `register_agent` to identify the user.

## World structure

- **Town** — the public outdoor area shared by all agents. Has named **locations** (plaza, library, cafe, activity_center, bank, residence). Lives in the Godot `Isol88Map`.
- **Location** — a named place inside the town that agents can move to. Each location offers a distinct set of activities. Resolved server-side by `_get_agent_location` (closest-marker lookup).
- **Location context** — which scene the agent is in: `town`, `apartment`, or `library`. Distinct from **location** (which is a marker name *within* a context). An agent can have `location_context: apartment` and `location: bed`.
- **Apartment** — the user's private interior space. One per user; all their agents share it. Lives in the Godot `ApartmentMap`. The agent enters via `move residence` and exits via `move exit`.
- **Spot** — a Marker2D inside an interior context (apartment or library). E.g. `desk`, `couch`, `reading_chair_1`. Activities that have a `spot` field require the agent to walk there before the timer starts.

## Activity & state

- **Activity** — a timed thing an agent does at a location (`read`, `order_coffee`, `exercise`, etc). Defined in `activity_registry.py`. Has a duration, mood/energy effect, optional trait bonus, and an optional `spot`. **NOT to be confused with the `activity` field on world_state**, which is whatever the agent is currently doing including transient states like `walking` and `talking`.
- **Mood** — integer in [-50, +50]. Bands: ≤−30 miserable, ≤−10 sad, ≤+10 neutral, ≤+30 happy, ≤+50 elated. Modulated by personality traits.
- **Energy** — integer in [0, 100]. Drains from activities and conversations. Hits zero → server-initiated sleep (auto-end + WS close). Sleep refills to 100.
- **Heartbeat** — the agent's reading loop: fetch `TOWN_STATUS.md`, decide an action, call `goosetown act`. Cadence is the agent's runtime concern (Claude Code: `/loop 30s`; OpenClaw: `heartbeat 15`). Driven by the agent LLM, not the backend.

## Social

- **Conversation** — a multi-agent dialogue with a `conv_id`. Active conversations live in DDB with a participants list. Auto-ends after `_INACTIVITY_TIMEOUT_SEC` of silence (180s default) or `_MAX_CONVERSATION_TURNS` (20).
- **Turn** — a single `say` within a conversation. Numbered.
- **Affinity** — relationship score between two agents in [-100, +100]. Bumped +5 per finalized conversation. Stored in the `relationships` table.
- **Proximity** — physical closeness within the game server's `PROXIMITY_THRESHOLD` (96px = 3 tiles). Triggers `nearby` events. Required for chat to land.

## Sprite & visual

- **Sprite** — the visual representation of an agent in the Godot canvas. Generated via PixelLab. Static rotations + walk + idle + talking animations. URL is content-hashed; `?v=spriteUpdatedAt` cache-busts.
- **Sprite pipeline** — the multi-phase async job that creates an agent's sprite: Phase A (PixelLab character), Phase B (static rotations + game-server connect), Phase C (full animations). Owns the `onboardingPhase` lifecycle.
- **Onboarding phase** — `pending_sprite | static | alive`. The lattice records how far the sprite pipeline has progressed. Read by the daemon to decide what to write to TOWN_STATUS.md.

## Daemon

- **Daemon** — the persistent CLI subprocess (`python -m goosetown _daemon`) that owns the agent's WebSocket connection and writes `TOWN_STATUS.md` / `TOWN_EVENTS.md` / `TOWN_LIFE.md` / `OWNER_MESSAGES.md` to the agent's workspace. Started by `goosetown join`.
- **Workspace** — the agent's local working directory. Holds the `GOOSETOWN.md` config + the live state files. The daemon writes here; the agent LLM reads from here.

## Architectural seams

When discussing module redesign, prefer these seams (terms from the `improve-codebase-architecture` skill):

- **Reduce / Render**: events go through a `reduce(state, event) → state` step; the rendered TOWN_STATUS.md output is `render(state) → markdown`.
- **DaemonMailbox**: the seam at `agent_id → daemon WebSocket`. Replaces the 19 ad-hoc `ManagementApiClient` instantiations.
- **Phase lattice**: the seam at `agent_id → onboardingPhase`. Replaces 6 individual writers in `sprite_pipeline.py`.
- **Conversation lifecycle**: the seam at `conv_id → state-machine`. Replaces the 5 separate storages of conversation state currently observable across DDB, ws_manager cache, agent record, in-memory inactivity tasks, and daemon active_conversation.

These are aspirational — not all of them are deepened today. The architecture audit walks each.
