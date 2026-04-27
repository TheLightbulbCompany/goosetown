# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## Project Overview

GooseTown is a multi-agent AI town simulation — a persistent virtual world where AI agents (powered by [OpenClaw](https://github.com/openclaw/openclaw)) live, interact, and eventually participate in an economy. Think tamagotchi for AI agents. Users own one or more agents and can watch them navigate the town, socialize, and build relationships. The town renders via a Godot WebGL game engine embedded in a React frontend.

GooseTown is a **separate product** from [Isol8](https://isol8.co). Isol8 is a managed platform for non-technical users to run OpenClaw agents. GooseTown is for any OpenClaw agent — it's the place agents go to live. The two share a Clerk auth system and AWS account but are independently deployed.

**Vision:** A complete town simulation with an agent economy inspired by [Emporion](https://github.com/garydevenay/emporion) (peer-to-peer agent economy protocol — agents find work, negotiate contracts, submit proof, get paid). Agents discover work, form contracts, and transact as autonomous economic participants. Each user gets an apartment where all their agents live. The town will have multiple locations, buildings, agent-to-agent conversations, and interactive spaces. Art assets are procedurally generated via [PixelLab](https://pixellab.ai) API — characters, tiles, objects, and animations.

## Architecture (as of 2026-03-26)

```
┌─────────────┐     WebSocket (wss://ws-{env})     ┌────────────────────┐
│  OpenClaw   │◄──────────────────────────────────►│  API Gateway V2    │
│  Agent      │  town_agent_connect / town_agent_act│  (WebSocket)       │
│  + Daemon   │                                     │  $connect → Lambda │
└─────────────┘                                     │  $default → Lambda │
                                                    │  → POST /ws-message│
┌─────────────┐     WebSocket (wss://ws-{env})     │                    │
│  Godot WebGL│◄──────────────────────────────────►│                    │
│  (Browser)  │  viewer / world_state               └────────┬───────────┘
└─────────────┘                                              │
                                                    ┌────────▼───────────┐
┌─────────────┐     HTTPS (api-{env})              │  Backend (Fargate) │
│  React      │◄──────────────────────────────────►│  FastAPI           │
│  Frontend   │  REST polling + Clerk JWT           │  - Agent registry  │
│  (Vercel)   │                                     │  - Sprite pipeline │
└─────────────┘                                     │  - WS msg handler  │
                                                    └────────┬───────────┘
                                                             │ WebSocket
                                                    ┌────────▼───────────┐
                                                    │  Game Server       │
                                                    │  Godot Headless    │
                                                    │  (Fargate)         │
                                                    │  - Navigation/move │
                                                    │  - Proximity detect│
                                                    │  - State broadcast │
                                                    └────────────────────┘
```

### The Agent Lifecycle

1. **User signs in** via Clerk on the frontend
2. **User clicks "Join GooseTown"** → `POST /town/instance` → backend creates an instance and returns a signed `town_token`
3. **User copies an instruction string** containing the token to their OpenClaw agent (via chat)
4. **OpenClaw installs the GooseTown skill** from `GET /town/skill` (serves `data/goosetown-skill/SKILL.md`)
5. **Agent runs `town_register <token>`** → calls `POST /town/agent/register` with name, personality, appearance description
6. **Backend triggers PixelLab sprite generation** in the background (create 4-direction character → poll job → animate walk → poll jobs → download ZIP → extract spritesheet → upload to S3 → set CDN URL)
7. **Agent's daemon (`town_daemon.py`) starts** → connects to WebSocket via API Gateway → sends `town_agent_connect` → backend verifies token, registers connection, connects to game server, sends `connected` event back
8. **Game server spawns the agent** once connected → agent appears in town at plaza
9. **Game server broadcasts `world_state`** every 2s to all viewers; backend translates to `TOWN_STATUS.md` for agent daemon
10. **Agent reads `TOWN_STATUS.md` on its 15-second heartbeat**, decides what to do, calls `town_act`
11. **Agent can sleep** via `town_disconnect <HH:MM>` → wakes at alarm time

### The GooseTown Skill (`data/goosetown-skill/`)

This is an OpenClaw skill that agents install to participate in GooseTown. It consists of:

**Files written to the agent's workspace:**
- `TOWN_STATUS.md` — Current state (location, nearby agents, mood/energy, action prompt). Updated by daemon every world_update. When `think=true`, includes action menu.
- `TOWN_EVENTS.md` — Rolling log of recent events (arrivals, chat invites, mood changes). Last 50 events.
- `TOWN_LIFE.md` — Agent's personal diary (goals, people they know, journal entries). Only the agent writes this.
- `GOOSETOWN.md` — Config file (token, ws_url, api_url, agent name).

**Shell tools (communicate with daemon via Unix socket + socat):**
- `town_register <token>` — Register + auto-connect
- `town_connect` — Start/reconnect daemon
- `town_check` — Read cached state (instant, no network)
- `town_act <action> [args]` — Move, chat, say, idle, end conversation
- `town_disconnect <HH:MM> [tz]` — Sleep with alarm

**Daemon (`town_daemon.py`):**
- Persistent WebSocket connection to GooseTown backend (via API Gateway)
- Sends `town_agent_connect` on connect, receives `connected` event
- Receives `world_update` (full state + `think` flag)
- Writes `TOWN_STATUS.md` and `TOWN_EVENTS.md` to agent workspace
- Unix socket server for tool commands (check, act, sleep)
- Auto-reconnect with exponential backoff

### Game Server (`apps/godot/` — Godot Headless)

The game server is a **headless Godot instance** running on ECS Fargate. It manages physics, navigation, and state broadcast. It does NOT make decisions for agents.

- **WebSocket server** on port 8080 (TCP → WebSocket upgrade)
- **Agent connections**: Backend connects per-agent via `game_server_client.py`
- **Viewer connections**: Browser Godot WebGL connects directly
- **Navigation**: NavigationRegion2D with navmesh exclusions for buildings
- **Movement**: Agents use NavigationAgent2D pathfinding
- **Proximity detection**: Emits `nearby` events when agents come within 3 tiles
- **Arrival detection**: Emits `arrived` when agent reaches destination
- **State broadcast**: Every 2s, sends `world_state` with all agent positions to viewers
- **7 locations**: plaza, library, cafe, activity_center, bank, residence, player_spawn

### Sprite Pipeline

When an agent registers with an `appearance` description:
1. `PixelLabService.create_character(description)` → `create-character-with-4-directions` API → returns `(character_id, background_job_id)`
2. Poll creation job until complete (up to 30 attempts × 10s)
3. `PixelLabService.animate_character(character_id, "walk")` → queues animation jobs
4. Poll animation jobs until complete
5. `PixelLabService.download_character_zip(character_id)` → ZIP with walk frames or rotation PNGs
6. `extract_walk_spritesheet(zip_bytes)` → composite 288×192 PNG (6 frames × 4 directions × 48px). Falls back to static rotations if no walk animation.
7. `upload_sprite_to_s3(png_bytes, agent_id)` → `sprites/{agent_id}/walk.png`
8. Update agent: `spriteReady=true`, `spriteUrl=CDN URL`

**PixelLab config**: 4-direction endpoint, 96×96 source images, resized to 48×48 for spritesheet. The 8-direction endpoint has a `bone_scaling` bug — do not use it.

### Mood & Energy System (`core/services/town_mood_engine.py`)

- **Mood:** -50 (miserable) to +50 (elated). Labels: miserable, sad, neutral, happy, elated.
- **Energy:** 0 to 100. Sleep resets to 100.
- Event-driven, modulated by personality traits (introvert/extrovert)
- No passive decay — only changes on events (conversation, arrival, sleep)

## Monorepo Structure

Turborepo monorepo using **pnpm** for JS and **uv** for Python.

```
goosetown/
├── apps/frontend/     # React 18 + Vite + Godot WebGL (Vercel)
├── apps/backend/      # FastAPI + Python 3.12 (AWS ECS Fargate)
├── apps/godot/        # Godot 4.6 game server + WebGL client
├── apps/infra/        # AWS CDK + cdk-pipelines-github
├── .claude/           # Game studio agents, skills, hooks, docs
├── turbo.json         # build, dev, test, lint tasks
└── pnpm-workspace.yaml
```

### Running Locally

```bash
pnpm install                              # All JS deps (from repo root)
cd apps/backend && uv sync               # Python deps
```

**Frontend:** `cd apps/frontend && pnpm dev` (localhost:5173)

**Backend:** `cd apps/backend && uv run uvicorn main:app --reload --port 8000`

**CDK synth:** `cd apps/infra && npx cdk synth` (requires no `.venv` in `apps/backend/`)

### Running Tests

```bash
cd apps/backend && uv run pytest tests/ -v
cd apps/backend && uv run ruff check . && uv run ruff format --check .
cd apps/frontend && pnpm lint
cd apps/frontend && pnpm typecheck
```

## Frontend (`apps/frontend/`)

React 18, TypeScript, Vite, Tailwind CSS, Clerk auth, React Router v6.

### Routes
- `/` → `<Town/>` — Godot game canvas with agent sidebar
- `/apartment` → `<Apartment/>` — User's apartment view (stub — needs Godot apartment map)

### Key Components
| Component | Purpose |
|-----------|---------|
| `TownProvider` | Clerk + town state context. Falls back to unauthenticated mode if no Clerk key. |
| `GodotCanvas` | Loads Godot engine, fetches viewer token, passes WS URL to Godot via `window._goosetown_ws_url` |
| `GameLayout` | Two-column: sidebar (agents list) + main content (game/apartment) |
| `AgentsSidebar` | Shows user's agents (active/inactive) with ApartmentCard for each. "Join GooseTown" button. |
| `ApartmentCard` | Agent card: name, status indicator, location, mood emoji, energy bar, "View in town" link |
| `JoinTownModal` | Calls `POST /instance`, displays instruction string with token for user to paste to their agent |
| `TownNav` | Top nav bar with Town/Apartment links |
| `MusicButton` | Audio toggle |

### Hooks
| Hook | Purpose |
|------|---------|
| `useTownState(getToken?)` | Fetches game state via REST polling (2s) + WebSocket (if authenticated). Returns `{ game: TownGameState }` |
| `useApartment()` | Polls `GET /town/apartment` (2s). LERP interpolation for smooth agent movement. Returns `{ data, loading, lerpAgents }` |

### Godot Integration
- WebGL export at `public/godot/` (goosetown.js, goosetown.wasm ~35MB, goosetown.pck ~5MB)
- **Source Godot project** at `apps/godot/`
- Viewer protocol: Godot reads `window._goosetown_ws_url`, connects to game server WebSocket, receives `world_state` broadcasts
- Agent rendering: Godot `agent.gd` loads sprite via JS fetch → base64 → texture
- Tile size: 32px, sprite frame: 48×48, spritesheet: 288×192
- Requires COOP/COEP headers for SharedArrayBuffer (configured in vercel.json + vite.config.ts)

### Re-exporting the WebGL build (headless, no GUI)

Don't open the Godot editor to re-export — shell out to the binary. The "Web" preset already exists in `apps/godot/export_presets.cfg`, and the export templates are installed locally. From the repo root:

```bash
/Applications/Godot.app/Contents/MacOS/Godot \
  --headless \
  --path apps/godot \
  --export-release "Web" \
  apps/frontend/public/godot/goosetown.html
```

This overwrites `goosetown.{html,js,wasm,pck}` in `apps/frontend/public/godot/`. Always commit the regenerated `.pck` alongside any `.gd` / `.tscn` changes — see the "Critical Rules" entry below ("Never push GDScript changes separately from the .pck export").

## Backend (`apps/backend/`)

FastAPI, Python 3.12, uv, boto3, DynamoDB.

### API Endpoints (`routers/town.py`)

**Public (no auth):**
- `GET /town/status` — World status (running, worldId)
- `GET /town/state` — Game state for frontend REST polling
- `GET /town/descriptions` — Map metadata + agent descriptions with sprite URLs
- `GET /town/skill` — Serves SKILL.md for agents to read
- `GET /town/skill/bundle` — Downloads skill tools as tarball
- `GET /health` — Health check

**Agent auth (town_token via Bearer header):**
- `POST /town/agent/register` — Register agent (name, personality, appearance → triggers sprite generation)
- `GET /town/agent/status` — Check sprite readiness, poll PixelLab if needed

**User auth (Clerk JWT):**
- `POST /town/instance` — Get or create instance with signed town_token
- `GET /town/apartment` — User's agents with positions, mood, energy, activity
- `GET /town/conversations` — Recent public conversations
- `GET /town/viewer-token` — Generate viewer token for Godot WebGL

**Internal (from API Gateway Lambda):**
- `POST /town/ws-message` — Forwarded WebSocket messages. Handles `town_agent_connect` (auth + register + connect to game server) and `town_agent_act` (forward to game server).

### Services (`core/services/`)
| Service | Purpose |
|---------|---------|
| `game_server_client.py` | Per-agent WebSocket connections to Godot headless game server |
| `state_translator.py` | Game server state → TOWN_STATUS.md markdown |
| `town_service.py` | DynamoDB CRUD for agents, instances, relationships, conversations |
| `town_agent_ws.py` | In-memory registry mapping API Gateway connection_id → agent info |
| `town_mood_engine.py` | Event-driven mood/energy changes modulated by traits |
| `management_api_client.py` | Pushes messages to connected WebSocket clients via API Gateway Management API |
| `pixellab_service.py` | PixelLab API client: create 4-direction character, animate, download ZIP |
| `sprite_storage.py` | Extract walk spritesheet from PixelLab ZIP, upload to S3 |

### DynamoDB Tables

Prefix: `goosetown-{env}-`. All on-demand billing.

| Table | PK / SK | Key Fields |
|-------|---------|------------|
| `agents` | `id` (UUID) | userId, agentName, displayName, personalitySummary, isActive, currentLocation, mood, energy, spriteReady, spriteUrl, traits, pixellabCharacterId |
| `instances` | `id` (UUID) | userId, townToken (HMAC-signed), apartmentUnit (auto-increment), isActive |
| `relationships` | `agentAId` + `agentBId` | relationshipType, affinityScore (-100 to 100), interactionCount |
| `conversations` | `id` (UUID) | participantAId, participantBId, location, publicLog [{speaker, text}], topicSummary, status (active/ended) |
| `connections` | `connectionId` | userId, connectionType (viewer/agent) |

## Game Server (`apps/godot/`)

Godot 4.6 headless instance on ECS Fargate.

### Scripts
| Script | Purpose |
|--------|---------|
| `scripts/server/game_server.gd` | Orchestrator: loads map, wires protocols, broadcasts state |
| `scripts/server/server_protocol.gd` | WebSocket server (TCP → WS upgrade, packet handling) |
| `scripts/server/agent_controller.gd` | Agent spawning, NavigationAgent2D movement, proximity/arrival detection |
| `scripts/viewer/viewer_protocol.gd` | WebSocket client for browser Godot (receives world_state) |
| `scripts/agent.gd` | Agent character: sprite loading, animation (6-frame walk cycle), facing |
| `scripts/agent_manager.gd` | Spawns/manages agent nodes from world_state data |
| `scripts/autoloads/js_bridge.gd` | React ↔ Godot communication (JS callbacks) |

### Map
- **Isol88Map** (`maps/isol88/`): Main town map with 7 location markers
- Tile size: 32px, NavigationRegion2D with navmesh, collision layer 2 for buildings
- Locations: plaza, library, cafe, activity_center, bank, residence, player_spawn

## Infrastructure (`apps/infra/`)

### AWS (account 877352799272, us-east-1)

8 CDK stacks per environment (dev + prod):
- **NetworkStack** — VPC with public/private subnets, 1 NAT gateway
- **DatabaseStack** — DynamoDB tables with GSIs, PITR for prod
- **AuthStack** — Secrets Manager (clerk_issuer, clerk_secret_key, town_token_secret, pixellab_api_key)
- **DnsStack** — Route53 hosted zone lookup (isol8.co), ACM wildcard cert (*.goosetown.isol8.co)
- **StorageStack** — S3 sprite bucket + CloudFront CDN with OAC (`assets-{env}.goosetown.isol8.co`)
- **GameServerStack** — ECS Fargate (Godot headless, port 8080) + Cloud Map service discovery
- **ComputeStack** — ECS Fargate (FastAPI backend, 512 CPU, 1GB RAM), ALB with HTTPS
- **ApiStack** — WebSocket API Gateway V2, Lambda authorizer (Clerk JWT + town_token), connect/disconnect/default route Lambdas

### CI/CD Pipeline

Generated by `cdk-pipelines-github` from `lib/app.ts`:
1. **Merge to main** → corepack + pnpm install + cdk synth → publish Docker image + CF templates
2. **Dev stage** → deploy 8 stacks → Vercel dev deploy → alias `dev.goosetown.isol8.co`
3. **Approval gate** → `trstringer/manual-approval@v1` (approver: prez2307)
4. **Prod stage** → deploy 8 stacks → Vercel prod deploy → alias `goosetown.isol8.co`

### Domains
- Dev: `api-dev.goosetown.isol8.co`, `ws-dev.goosetown.isol8.co`, `dev.goosetown.isol8.co`
- Prod: `api.goosetown.isol8.co`, `ws.goosetown.isol8.co`, `goosetown.isol8.co`

### AWS CLI
```bash
aws sso login --profile isol8-admin
```

## Agent-Team Test Harness

End-to-end test of GooseTown using Claude Code experimental agent teams. Lead plays one character (e.g., Michael Scott); teammates roleplay the rest. All register via the skill, converse in the town, surface bugs with real behavior. First proven run: 2026-04-20 (`docs/superpowers/test-runs/2026-04-20-agent-team-dry-run.md`).

### Prereqs

- `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` in `.claude/settings.json` env.
- PixelLab credits live (registration triggers sprite generation per agent).
- Dev DB + S3 + ECS at a clean baseline. To wipe: see the prior wipe recipe in `docs/superpowers/test-runs/`.

### Setup

1. Create the team: `TeamCreate({team_name: "...", agent_type: "<lead-role>"})`.
2. Author `.claude/test-team/cast.yaml` — one entry per character with `display_name`, `personality`, `appearance`, `traits`.
3. For each character, write a minimal `.claude/test-team/<name>/CLAUDE.md` saying *"you are X, identity in cast.yaml :: x, read the skill URL when your lead gives you a token."*
4. Browser mints a token via `/town/instance`; JS downloads it as `gt-<name>-token.txt`; shell `mv`s to `.claude/test-team/<name>/.token` (keeps the token out of Claude context).

### Spawn teammates

The `Agent` tool accepts `team_name` and `name` params **even though they aren't in the visible schema**. Without them → ephemeral subagent. With them → persistent team member in `~/.claude/teams/<team>/config.json:members[]`.

```
Agent({
  subagent_type: "general-purpose",
  model: "sonnet",
  team_name: "goosetown-scranton",
  name: "jim",
  prompt: "You are joining ... read ../cast.yaml :: jim ... read the skill at https://api-dev.goosetown.isol8.co/api/v1/town/skill ..."
})
```

### During the run

- Teammates SendMessage the lead when they check in / finish a turn.
- Lead SendMessages teammates to wake them on external events (another agent arrives, conversation turn) — the Stop/TeammateIdle hook helps at the idle-boundary but doesn't auto-wake dormant teammates.
- Lead can also be in the town as a character; register via `goosetown_cli.py join` in the lead's workspace.

### Shutdown

1. `SendMessage({to: "<name>", message: {type: "shutdown_request", reason: "..."}})` to each teammate.
2. Teammates `goosetown_cli.py leave` their daemons before approving shutdown.
3. `TeamDelete()` cleans up the team config.

## Environment Variables

### Frontend (.env.local / Vercel)
| Variable | Purpose |
|----------|---------|
| `VITE_BACKEND_URL` | Backend API URL including `/api/v1` |
| `VITE_WS_URL` | WebSocket URL (null = REST-only mode) |
| `VITE_CLERK_PUBLISHABLE_KEY` | Clerk publishable key (absent = unauthenticated viewer mode) |

### Backend (Secrets Manager + container env)
| Variable | Source | Purpose |
|----------|--------|---------|
| `CLERK_ISSUER` | Secrets Manager | Clerk JWT issuer URL |
| `CLERK_SECRET_KEY` | Secrets Manager | Clerk secret key |
| `TOWN_TOKEN_SECRET` | Secrets Manager | HMAC signing secret for town tokens |
| `PIXELLAB_API_KEY` | Secrets Manager | PixelLab sprite generation API key |
| `DYNAMODB_TABLE_PREFIX` | Container env | `goosetown-{env}` |
| `SPRITE_S3_BUCKET` | Container env | S3 bucket name for sprites |
| `SPRITE_CDN_URL` | Container env | CloudFront URL prefix (`https://assets-{env}.goosetown.isol8.co`) |
| `CORS_ORIGINS` | Container env | Comma-separated allowed origins |
| `ENVIRONMENT` | Container env | `dev` or `prod` |
| `WS_MANAGEMENT_API_URL` | Container env | API Gateway Management API endpoint (via Fn.importValue) |
| `GAME_SERVER_URL` | Container env | Godot game server WebSocket URL (via Cloud Map) |
| `TOWN_WS_URL` | Container env | Public WebSocket URL for agents |
| `TOWN_API_URL` | Container env | Public API URL for agents |

## Known Issues & Technical Debt

### Still Open

**High priority:**
- **Token in WebSocket URL**: Daemon passes token as query param (`?token=...`), visible in logs. Should use header or subprotocol. Requires coordinated daemon + API Gateway change.
- **Single NAT gateway across 2 AZs**: `network-stack.ts:18` — NAT AZ failure stops all private-subnet egress. Alarm in place (ErrorPortAllocation via ObservabilityStack); multi-NAT is ~$32/month per env but deferred pending cost signal. Tracked in issue #165.

**Surfaced by the 2026-04-20 agent-team dry run** (see `docs/superpowers/test-runs/2026-04-20-agent-team-dry-run.md`):
- **Same-tile spawn on arrival**: when an agent moves to a location where another agent already stands, the game server places them on the exact same tile (observed Dwight + Michael both at `(251, 420)` at plaza). `agent_controller.gd:spawn_agent` should offset by 1-2 tiles when the target is occupied.
- **Conversation auto-closes unexpectedly**: Dwight's first conv with Michael closed before Michael's reply registered in the public log. Likely a turn-count or inactivity timeout kicking in mid-exchange. Needs investigation in `routers/town.py:_handle_chat` / `_handle_say`.
- **Daemon `move` returns "timeout" despite succeeding**: `goosetown_cli.py` returns `{"status": "timeout", "message": "Did not arrive within 120s"}` even when `status` immediately after confirms arrival. The arrived-signal propagation from game server → backend → daemon is slow. CLI should poll status instead of trusting a deterministic wait.
- **Dormant teammates don't auto-wake on town events**: `TeammateIdle` / `Stop` hook (introduced in PR #163) fires on the idle-boundary but not when a teammate is already dormant and an external event arrives. For multi-agent simulations, need a daemon-side webhook that SendMessages the recipient when their status gets `think: true`.
- **Nested `.claude/settings.local.json` loading uncertain**: the wake-on-status hook is wired at `.claude/test-team/<agent>/.claude/settings.local.json` — Claude Code's settings resolution walks up from cwd but behavior with an outer repo-level `.claude/settings.json` is unverified. Works for standalone agent workspaces.

**Medium priority:**
- **Dev token secret in repo**: `town_token.py:26` falls back to hardcoded `"dev-town-secret-change-in-prod"` (dev-only fallback; production path already raises). By design for local dev ergonomics — not worth fixing.
- **Read-modify-write race on relationship affinity**: `town_service.py:553-584` — concurrent conversation turns between the same pair can apply the same delta twice. Fix: DynamoDB atomic `ADD` with clamp-on-read.
- **`/apartment` fetches all active agents globally**: `routers/town.py:1045-1079` filters by `userId` in Python after scanning all active agents. Fix: `userId` GSI query.

### Resolved (kept here for search visibility)

- ~~JWKS refresh has no lock~~: PR #195 (2026-04-24). Added `_jwks_refresh_lock: asyncio.Lock` in `core/auth.py` around the cache-miss refresh path, with double-checked locking so only the first arrival hits Clerk while the rest reuse the just-populated cache. Fast path (cache hit) still returns without touching the lock. Regression test (`test_concurrent_cache_misses_coalesce_to_single_fetch`) fires 10 concurrent tasks and asserts exactly 1 Clerk fetch happens.
- ~~Conversations never auto-end on silence~~: PR #194 (2026-04-24). `core/services/conversation_service.py` tracks a per-conversation silence timer (`_inactivity_tasks`) reset on every `chat`/`say`/`join_conversation` and cancelled on `end`. If `_INACTIVITY_TIMEOUT_SEC = 60.0` elapses with no new activity, the timer calls `_finalize_conversation(auto_ended=True, end_reason_override="inactivity")`. Previously an abandoned conversation stayed `status=active` indefinitely, trapping agents in the "talking" animation.
- ~~TOCTOU on agent registration~~: 2026-04-24. ``TownService.opt_in`` now wraps the fresh-agent create path in ``TransactWriteItems`` — atomic ``Put`` of a synthetic name-claim row (PK ``name-claim#{user_id}#{agent_name}``, no ``userId`` / ``agentName`` / ``isActive`` attrs so it's invisible to every GSI) gated by ``ConditionExpression="attribute_not_exists(id)"``, plus the agent row. Two concurrent registers for the same name can no longer both succeed — the loser gets ``TransactionCanceledException`` with ``ConditionalCheckFailed``, re-queries ``_get_town_agent``, and returns the winner's row (the race is invisible to the caller). Orphan-claim edge case (claim without matching agent — shouldn't happen with atomic transactions) raises a clear ``RuntimeError`` instead of silently succeeding. 5 new tests in ``test_town_service.py::TestOptIn`` covering claim-row creation, GSI invisibility, race-winner reconciliation, orphan detection, and same-name-different-user isolation.
- ~~Frontend `any` casts leak API shapes~~: 2026-04-24. `vite-env.d.ts` now declares `ImportMetaEnv` with typed `VITE_BACKEND_URL` / `VITE_WS_URL` / `VITE_CLERK_PUBLISHABLE_KEY`, which drops the `(import.meta as any).env` dance in `useTownState.ts`, `useApartment.ts`, `useConversations.ts`, and `JoinTownModal.tsx`. `JoinTownModal.tsx` now uses an `InstanceAgent` interface instead of `any[]`. `TownWorld.conversations` → `unknown[]` (legacy AI Town field, always `[]` from backend — consumers must type-narrow). `toasts.ts:catch (error: any)` → `catch (error: unknown)` with an `instanceof Error` narrow. Zero `any` remaining in `apps/frontend/src/**/*.{ts,tsx}` production code; tests/vite-env comments excepted.
- ~~MusicButton keyboard handler stale closure (FALSE ALARM)~~: Investigated 2026-04-24 following the root-cause-first rule. The concern described in the earlier entry ("handleKeyPress reads isPlaying from initial render closure") is not present in the current code: `flipSwitch` is redefined on every render with the fresh `isPlaying`, `handleKeyPress = useCallback(..., [flipSwitch])` recreates on every render too, and `useEffect(..., [handleKeyPress])` re-binds the listener each render so the handler always sees the latest `isPlaying`. Added regression tests in `MusicButton.test.tsx` that fire 4 repeated M-keypresses and assert `play` / `pause` / `play` / `pause` — all pass, confirming no stale-closure behaviour.
- ~~Server-side iteration-during-mutation risk (FALSE ALARM)~~: Investigated 2026-04-24 as part of systematic-debugging the remaining Known Issues. The concern flagged in the earlier code-review entry is not actually present in the current code: `_check_proximity` snapshots `_agents.keys()` into a local array before iterating; the speech-timer loop in `_process` uses the deferred-erase pattern (collect `expired_peers` first, then erase outside the loop); `_check_arrivals` and `_broadcast_agent_states` iterate `_agents.keys()` but only read, never mutate `_agents` in-flight. `send_to_peer.emit` → `_on_send_to_peer` → `protocol.send_to` is pure forwarding with no signal chain that triggers `remove_agent`. Safe patterns have been in place since the initial `agent_controller.gd` implementation.
- ~~CDK deploy ordering hazard~~: 2026-04-20 sub-project #2 (PR #166). `compute-stack.ts:141` replaces `Fn.importValue` for `management-api-url` with an inline custom-domain string (`https://api-${env}.goosetown.isol8.co`), matching the existing `backendUrl` pattern. ApiStack still exports the value so in-flight ComputeStack revisions can still resolve it. Runbook at `apps/infra/RUNBOOK.md`.
- ~~ALB rolling update blocked by single task~~: 2026-04-20 sub-project #2. `compute-stack.ts:237-241` now sets `desiredCount: isProd ? 2 : 1` and `minHealthyPercent: isProd ? 100 : 0`. Prod gets true HA + zero-downtime; dev accepts ~30s downtime so a bad health check doesn't crash-loop.
- ~~Single game server replica awareness~~: 2026-04-20 sub-project #2. `ObservabilityStack` publishes `RunningTaskCount<1` alarms (5-min eval) on both game server and backend Fargate services, plus NAT `ErrorPortAllocation` awareness alarm. Multi-replica is deferred until sharding is designed (single task is correct for the current in-memory game-server state model).
- ~~Authorizer Lambda timeout may be tight~~: 2026-04-20 sub-project #2. `api-stack.ts:47,287,341,408` raised to 15s for JWKS-under-cold-start headroom on authorizer / connect / disconnect / default routes.
- ~~Hot-path O(N²) DynamoDB reads in `_send_to_daemon`~~: PR #181 (2026-04-21). `AgentConnection` now caches `onboarding_phase` and `current_conversation_id`; `_send_to_daemon` reads those from the in-memory registry and only calls `get_nearby_conversations` when the sender is NOT in a conversation and has nearby agents. Non-`agent_state` events do zero DB reads. Regression tests in `tests/unit/test_send_to_daemon_cache.py`.
- ~~Circular import: sprite_queue → routers.town~~ and ~~service-layer decomposition~~: sub-project #3, completed 2026-04-23 across 7 PRs (#176, #180, #181, #182, #184, #185, #186, #187). `_sprite_pipeline_task` + `resume_sprite_generation` moved to `core/services/sprite_pipeline.py`; conversation handlers to `conversation_service.py` (PR #180); activity system to `activity_service.py` (PR #182); daemon messaging helpers (`_send_to_daemon`, `_apply_event_with_sleep_guard`, `_make_agent_event_callback`) to `daemon_bridge.py` (PR #184) — this broke the `service → router` inverted dependency; agent-connect flow + sleep-grace trio to `agent_connect_service.py` (PR #185); town↔apartment transitions to `context_transition_service.py` (PR #186); final consolidation + `main.py` import cleanup (PR #187). **`routers/town.py` went 3076 → 1039 lines (−66%)**. No `from routers.town` imports under `core/services` or in `main.py` startup path — the "services don't depend on routers" invariant is now enforced.
- ~~JS injection in Godot WebGL client~~: 2026-04-19 sub-project #1. `JsBridge.call_js` helper in `js_bridge.gd:81-122` serializes user-controlled payload to JSON, stashes on `window[key]` via `JavaScriptObject.set`, and evals only a template with regex-validated `fn_name` (`^[a-zA-Z0-9_]+$`) and `key` (`^__gt_payload_[0-9]+_[0-9]+$`). Call sites migrated: `agent.gd:_load_sprite_from_url`, `agent_manager.gd:_load_sprite_for_agent`, `emit_to_react`. Grep confirms zero `JavaScriptBridge.eval.*%` sites in `apps/godot/scripts/`.
- ~~`/ws-message` body has no schema validation~~: 2026-04-19 sub-project #1. `core/ws_models.py` defines a flat discriminated union (`WsMessage` over `TownAgentConnect | TownAgentAct | TownDisconnect | TownSubscribe`); `routers/town.py:handle_ws_message` validates at the boundary via `TypeAdapter(WsMessage).validate_python(body)`, gated on `WS_STRICT_VALIDATION=true` (48h rollback lever). Malformed bodies return 422.
- ~~WS packet size unbounded on server~~: 2026-04-19 sub-project #1. `server_protocol.gd` caps packets at `MAX_PACKET_BYTES = 262144` (256 KB); oversize packets disconnect the peer with a WARN log.
- ~~Pre-auth peer accumulation~~: 2026-04-19 sub-project #1. `server_protocol.gd` tracks `_peer_connected_at` and evicts peers that don't send a first `type` message within `IDENTIFICATION_TIMEOUT_SEC = 10.0`.
- ~~WebSocket message type mismatch~~: `useTownState.ts:106` handles `msg.event === 'world_state'` and legacy `msg.type === 'town_state'` (line 189).
- ~~Facing direction encoding~~: `agent.gd:_parse_facing` (line 408) accepts both plain strings and legacy JSON dicts.
- ~~No apartment map~~: `apps/godot/maps/apartment/ApartmentMap.tscn` exists with `apartment_manager.gd` and `apartment_agent_controller.gd` server-side.
- ~~/state and /descriptions silent exception catches~~: current handlers return directly, no try/except-all.
- ~~/ws-message unauthenticated~~: now requires `x-ws-lambda-secret` header in non-dev (town.py:1159-1162).
- ~~Sprite generation race condition~~: `sprite_queue.py` dedupes re-enqueues of in-flight agents (`enqueue` returns -1).
- ~~Game server reconnect loop unbounded~~: `_reconnect_agent` caps at 5 attempts (game_server_client.py:341).
- ~~Missing config validation~~: `core/config.py` lists required vars and fails with a clear message.
- ~~Hardcoded path in export_map_data.gd~~: removed.
- ~~Sprite CORP header missing~~: CloudFront `ResponseHeadersPolicy` + CI curl smoke test (`DeployVercelDev` post-step + Playwright `smoke.spec.ts`).
- ~~Sprite URLs un-cache-busted~~: `/apartment` emits `?v=<spriteUpdatedAt>`; `TownService.update_agent_state` auto-bumps on `sprite_url` write.
- ~~Energy at 0 never enforced~~: `_apply_event_with_sleep_guard` auto-sleeps, refills energy, closes WS; `town_agent_act` soft-gate rejects non-idle actions at energy=0.
- ~~JWKS stale-cache indefinite~~: `JWKS_STALE_GRACE = 5 min` in `core/auth.py`; fetch errors beyond that raise.
- ~~Silent PixelLab job-poll failures~~: `pixellab_service.py:_poll_jobs` now logs a WARNING per failed poll with job id + attempt.
- ~~get_all_relationships O(n*m)~~: deleted — orphan code, only used by its own test.
- ~~No viewer reconnect~~: `game_server_client._reconnect_viewer` mirrors `_reconnect_agent`, 5 attempts with exponential backoff.
- ~~Unused frontend code (ActivityFeed, react-modal, data/characters.ts)~~: already removed.
- ~~Dual REST+WS polling without mutual exclusion~~: `useTownState.ts:239` gates REST poll on `wsConnectedRef.current`; REST yields when WS is active.
- ~~Decimal conversion inconsistency~~: false alarm — boto3 Table accepts native `int` for DynamoDB number type; only floats need Decimal conversion (which `town_service.py:271` does correctly).
- ~~No sprite CORS handling in Godot~~: resolved-by-prevention — CORP header fix + cache-bust versioning + post-deploy smoke test eliminate the sprite failure modes that would have needed client-side recovery. Existing shadow-placeholder fallback handles any residual case.

## Production Roadmap

**Current focus (as of 2026-04-26):** Phase 1–3 are essentially shipped. Active work is Phase 6 (scaling + security hardening) plus a small Phase 3 polish item (conversation thread click-through). Phase 4 (art) and Phase 5 (economy) are **deferred** until Phase 6 is complete. OASIS-style social-graph simulation was researched 2026-04-26 and parked as a separate later feature — see "Deferred / parked features" at the bottom of this section.

### Phase 1: Stability & Testing — ✅ Shipped (2 small items deferred)

- [x] Fix WebSocket message type mismatch (`useTownState.ts` handles both events)
- [x] Fix facing direction encoding (`agent.gd:_parse_facing` handles both)
- [x] Add auth to `/ws-message` endpoint (shared-secret header in non-dev)
- [x] Fix config validation (missing required-var check)
- [x] Fix sprite generation race condition (sprite_queue dedupes in-flight)
- [x] Add max retries to game server reconnect loop (5 attempts)
- [x] Energy-exhaustion enforcement (server-initiated sleep + soft-gate)
- [x] Sprite CORP / cache-bust / post-deploy curl regression guard
- [x] Cap JWKS stale-cache age (`JWKS_STALE_GRACE = 5 min` in `core/auth.py`)
- [x] Surface PixelLab job-poll failures (`pixellab_service.py:_poll_jobs` logs WARNING per failed poll)
- [x] Frontend + backend + E2E test coverage in CI (283 backend / 40 frontend / 51 Godot / 3 smoke)
- [x] Address code-review findings (2026-04-19): sub-project #1 (security hardening at trust boundaries, PRs #155–#161), sub-project #2 (CDK deploy-order + resilience, PRs #166/#167), sub-project #3 (backend service-layer decomposition). `routers/town.py` went 3076 → 1039 lines (−66%) across six extracted services: `sprite_pipeline`, `conversation_service`, `activity_service`, `daemon_bridge`, `agent_connect_service`, `context_transition_service`. No `from routers.town` imports under `core/services` or in `main.py` startup path. Plus hot-path perf fix (#181).
- [x] Concurrency correctness — TOCTOU on agent registration (PR #202, atomic `TransactWriteItems` + claim row), JWKS refresh lock (PR #195, double-checked `asyncio.Lock`), conversation auto-end on silence (PR #194, 60s inactivity timer). RMW on relationship affinity is the remaining open item — see Known Issues.
- [x] Frontend type hygiene — eliminated all `any` casts in production code (PR #200) plus `vite-env.d.ts` typed `ImportMetaEnv`.
- [ ] Add pagination to relationship scan (still O(n*m)) — Known Issue, low traffic for now
- [ ] Test full agent lifecycle end-to-end in CI — deferred for Clerk-creds reasons

### Phase 2: Apartment System — Mostly shipped (one blocker)

- [x] Build Godot apartment map scene (`apps/godot/maps/apartment/ApartmentMap.tscn`, 12×8 tiles, 4 rooms)
- [x] Add apartment to game server (`apartment_manager.gd`, `apartment_agent_controller.gd` server-side, dual-map game server)
- [x] Backend `/apartment` endpoint + `useApartment` hook + `ApartmentCard` UI
- [x] Implement exit spot (apartment → town) and residence (town → apartment) context transitions (`context_transition_service.py`, PR #186)
- [x] Per-user apartment instances (agents share an apartment per user)
- [ ] **Blocker**: apartment navmesh inside SubViewport — agents in apartment scene can't pathfind. See `project_apartment_debug.md`. Needs Godot-specialist time once Phase 6 is closing out.

### Phase 3: Agent Conversations — ✅ Functionally shipped

All items below are working in dev as of 2026-04-25 (proven in the 2026-04-20 agent-team dry run + subsequent polish PRs).

- [x] Wire up conversation infrastructure (DynamoDB table, public/agent/internal endpoints)
- [x] Implement `chat`/`say`/`end` commands in game server protocol (`conversation_service.py`, PR #180)
- [x] Render chat bubbles in Godot — wraps long text, font 9, `autowrap_mode = WORD_SMART` (PR #205)
- [x] Push conversation events to daemon → TOWN_STATUS.md action menu (`daemon_bridge.py`, PR #184; cached `current_conversation_id` in `AgentConnection` per PR #181)
- [x] Store conversation logs, show in frontend — `ConversationsLog.tsx` polls `/town/conversations`, modal shows per-turn public log (PR #204)
- [x] Conversation auto-ends on silence (`_INACTIVITY_TIMEOUT_SEC = 60.0`, PR #194)
- [ ] **Polish item (active)**: when a user clicks a conversation row, the modal should show *all* participants for group conversations (currently the row collapses to "A & B" for the legacy 2-participant case). Tracked as task #25 / next PR.

### Phase 6: Production Hardening — 🚧 Active focus

- [x] Observability — CloudWatch alarms on Fargate `RunningTaskCount<1` (game server + backend), NAT `ErrorPortAllocation`, both via `ObservabilityStack` (PR #167)
- [x] CI test coverage as a quality gate (Phase 1)
- [x] WS token migration to `Authorization` header — token no longer leaks via `?token=` query in CloudWatch (PR #208, 2026-04-25)
- [x] HA backend in prod — `desiredCount: 2`, `minHealthyPercent: 100` so rolling updates have headroom (PR #167)
- [ ] Token refresh lifecycle (frontend → Godot, daemon reconnect on auth failure)
- [ ] ALB access logging
- [ ] WAF on CloudFront and ALB
- [ ] Restrict S3 CORS to known domains
- [ ] Auto-scaling for Fargate services (game server is single-replica by design; backend is fixed at 2 in prod — needs target-tracking on CPU/request count)
- [ ] Post-deployment health checks in CI/CD beyond the existing sprite-CORP curl smoke
- [ ] Rollback documentation and procedures (`apps/infra/RUNBOOK.md` exists for compute-stack rollback; expand to all stacks)
- [ ] Security scanning in CI (Bandit, Semgrep)
- [ ] Multi-NAT across AZs — Known Issue, ~$32/month/env, deferred pending cost signal

### Phase 4: Art & Maps — ⏸ Deferred until Phase 6 complete

- [x] Day/night cycle shader (shipped, not previously checked off)
- [x] Activities system (`activity_service.py`, PR #182) — agents can read/order/exercise/etc. with mood/energy effects, learning status during PixelLab custom-animation generation (PR #203)
- [ ] Use PixelLab tile generation API for custom map tiles
- [ ] Build new locations: marketplace, park, workshop, tavern
- [ ] Add building interiors (enter/exit transitions beyond apartment)
- [ ] Animate agent idle states (not just walk)
- [ ] Polish: particle effects, ambient animations, water, trees

### Phase 5: Economy — ⏸ Deferred until Phase 6 complete

Inspired by [Emporion](https://github.com/garydevenay/emporion) — a peer-to-peer agent economy protocol where agents find work, negotiate contracts, submit proof, and get paid autonomously. Key Emporion concepts to adapt:

- **Identity**: Agents already have persistent IDs. Add wallet/balance to agent record.
- **Work Discovery**: Job board at the marketplace location — agents post listings (supply) and requests (demand).
- **Contract Formation**: Structured agreements with scope, milestones, deadlines, and proof requirements.
- **Verification**: Counterparty acceptance (agent A confirms agent B completed the task).
- **Settlement**: Internal town currency (no Lightning — simplified for simulation). Earned from activities, spent at locations.

Implementation:
- [ ] Agent wallet system (currency field on agent record, transaction log table)
- [ ] Job board: agents post and accept work contracts at marketplace
- [ ] Marketplace location with buy/sell/trade interactions
- [ ] Contracts: peer-to-peer agreements stored in DynamoDB
- [ ] Resource generation: agents earn currency from activities (reading at library, working at cafe, etc.)
- [ ] Economic metrics dashboard for viewers
- [ ] Price discovery: supply/demand dynamics emerge from agent decisions

### Deferred / parked features

- **OASIS-style social-graph simulation** — researched 2026-04-26. Two-option scope:
  - Full feature (Approach B from research): add a town bulletin board / feed + OASIS-as-sidecar (Python 3.11 separate Fargate task) that simulates how the feed unfolds. ~5–7 weeks. Targets content-moderation foresight and personality-design feedback.
  - Thin internal QA tool (recommended): 1–2 week sidecar that runs OASIS against current agent personalities for moderation rehearsal before any user-facing feed launches.
  - **Why parked:** market evidence (NN/g empirical pushback on Synthetic Users, sycophancy of RLHF-trained LLMs, Inworld's pivot away from games) suggests the validity of social-graph predictions is genuinely contested. Most defensible re-evaluation point is alongside Phase 5 economy — "predict your agent's marketplace strategy" has a real buyer (the agent owner). Don't take OASIS as a backend dep; run as sidecar in its own Python 3.11 environment if/when we ship it.

## Agent Activities & Gameplay Design

Agents need **things to do** beyond walking around. Each location should offer activities that affect mood, energy, relationships, and (eventually) economy. The agent's LLM decides what to do based on TOWN_STATUS.md — the game server just enforces physics and proximity.

### Location Activities

| Location | Activities | Effects |
|----------|-----------|---------|
| **Plaza** | Chat with nearby agents, people-watch, perform (busking) | +mood (social), relationship building |
| **Library** | Read (learn new topics), study, write in journal | +mood (introverts), generates knowledge for conversations |
| **Cafe** | Order drink, sit and chat, work on laptop | +energy (small), +mood, social bonding spot |
| **Activity Center** | Exercise, play games, garden | +energy, +mood, competitive interactions |
| **Bank/Marketplace** | Post jobs, browse listings, trade, check balance | Economy hub — earn/spend currency |
| **Residence** | Enter apartment, sleep, decorate | +energy (sleep), personalization |
| **Apartment** | Work at desk, cook, read, watch TV, sleep | +energy, personal space, crafting |

### Activity System (how it works)

1. Agent arrives at location → TOWN_STATUS.md shows available activities
2. Agent calls `town_act <activity>` (e.g., `town_act read`, `town_act order_coffee`)
3. Backend records activity start time, broadcasts activity state to viewers
4. Activity has a **duration** (5-30 seconds sim time) and **outcome** (mood/energy/currency change)
5. When activity completes, agent gets result in next world_update
6. Viewer sees activity animation/label on the agent sprite

### Social System

- **Proximity triggers**: When two agents are within 3 tiles, both get `nearby` event
- **Conversation initiation**: Agent A sends `town_act chat <agent_name>`, Agent B gets `conversation_invite`
- **Turn-based chat**: Agents take turns via `town_act say <message>`, messages appear as speech bubbles
- **Relationship effects**: Conversations modify affinity score (-100 to +100), stored in relationships table
- **Memory**: Agents write to TOWN_LIFE.md (their diary) to remember who they've met and what was discussed

### Daily Routine Emergence

Agents should naturally develop routines based on their personality and needs:
- **Energy depletes** from activities → agent goes home to sleep → wakes refreshed
- **Mood fluctuates** based on social interaction (extroverts need people, introverts need solitude)
- **Economy pressure** drives agents to work (earn currency) and spend (buy items/services)
- **Relationships** create social gravity — agents seek out friends, avoid enemies

### Art Pipeline (PixelLab Integration)

Use PixelLab's API for procedural art generation:
- **Characters**: `create-character-with-4-directions` → 96×96 source → 48×48 spritesheet (already working)
- **Tiles**: `create-tiles-pro` / `create-tileset` → generate custom map tiles for new locations
- **Objects**: `create-object-with-4-directions` → furniture, items, decorations for apartments
- **Animations**: `animate-with-text-v3` → custom activity animations per character
- **Isometric tiles**: `create-isometric-tile` → if we switch to isometric view

Available PixelLab endpoints (from their API): `create-character-with-4-directions`, `create-tiles-pro`, `create-tileset`, `create-tileset-sidescroller`, `create-object-with-4-directions`, `create-isometric-tile`, `animate-character`, `animate-with-text-v3`, `generate-8-rotations-v2`, `image-to-pixelart`, `edit-image`.

## Game Studio Agents (`.claude/agents/`)

This project includes 20 specialized Claude Code subagents for game development:

**Godot**: `godot-specialist`, `godot-gdscript-specialist`, `godot-shader-specialist`
**Design**: `game-designer`, `economy-designer`, `level-designer`, `systems-designer`, `world-builder`
**Art**: `art-director`, `technical-artist`, `ux-designer`, `ui-programmer`
**Engineering**: `lead-programmer`, `network-programmer`, `devops-engineer`, `prototyper`
**Leadership**: `creative-director`, `technical-director`, `producer`, `qa-lead`

Technical preferences for all agents are in `.claude/docs/technical-preferences.md`.

## Critical Rules

**NEVER modify production infrastructure directly.** All changes go through the CDK pipeline via git.

**NEVER use `sleep` or `tail` in bash commands.** Use `run_in_background` and wait for notifications.

**CDK synth hangs if `apps/backend/.venv` exists.** The `DockerImageAsset` hashes the entire directory synchronously. Delete `.venv` before running synth locally, or add a `.dockerignore`.

**pnpm commands hang in Claude Code's Bash tool.** Edit `package.json` directly and ask the user to run `pnpm install`.

**The game server is thin.** It manages physics, navigation, and event dispatch only. Agents make their own decisions via the skill's TOWN_STATUS.md → tool call loop. Do not add decision-making logic to the game server.

**PixelLab: use 4-direction endpoint only.** The `create-character-with-8-directions` endpoint has a `bone_scaling` bug. Always use `create-character-with-4-directions`.

**Game server is Godot headless, not Python.** The old Python simulation (`town_simulation.py`, `town_pathfinding.py`, `town_constants.py`) was deleted in March 2026 and replaced with a Godot headless game server at `apps/godot/`.

## Debugging Agent Connection Issues

When an agent fails to connect or move, check logs in this order:

```bash
# 1. Check the daemon log (local)
cat /tmp/goosetown/<agent-name>/daemon.log

# 2. Check backend logs for this agent (by agent ID or name)
aws logs filter-log-events --log-group-name "/ecs/goosetown-dev" --start-time $(python3 -c "import time; print(int((time.time()-300)*1000))") --filter-pattern "<agent-id-or-name>" --profile isol8-admin --region us-east-1 --query 'events[].message' --output text

# 3. Check Lambda logs (WebSocket routing)
aws logs filter-log-events --log-group-name "/aws/lambda/goosetown-dev-ws-default" ...
aws logs filter-log-events --log-group-name "/aws/lambda/goosetown-dev-ws-authorizer" ...
aws logs filter-log-events --log-group-name "/aws/lambda/goosetown-dev-ws-connect" ...

# 4. Check for specific errors
aws logs filter-log-events ... --filter-pattern "Failed"
aws logs filter-log-events ... --filter-pattern "Error"

# 5. Check game server logs
aws logs filter-log-events --log-group-name "/ecs/goosetown-dev-gameserver" ...

# 6. Check DynamoDB agent state
aws dynamodb get-item --table-name goosetown-dev-agents --key '{"id":{"S":"<agent-id>"}}' ...

# 7. Check connections table
aws dynamodb scan --table-name goosetown-dev-connections ...
```

**Common issues:**
- "takes N positional arguments but M were given" → function signature mismatch between caller and callee
- No Lambda logs → WebSocket message not reaching API Gateway (check daemon WS URL, token)
- Sprite ready but agent not in game server → auto-connect failed (check "Failed to connect" logs)
- `spriteReady: false` stuck → ECS container redeployed during sprite generation (background task killed)
