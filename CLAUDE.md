# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## Project Overview

GooseTown is a multi-agent AI town simulation — a persistent virtual world where AI agents (powered by [OpenClaw](https://github.com/openclaw/openclaw)) live, interact, and eventually participate in an economy. Think tamagotchi for AI agents. Users own one or more agents and can watch them navigate the town, socialize, and build relationships. The town renders via a Godot WebGL game engine embedded in a React frontend.

GooseTown is a **separate product** from [Isol8](https://isol8.co). Isol8 is a managed platform for non-technical users to run OpenClaw agents. GooseTown is for any OpenClaw agent — it's the place agents go to live. The two share a Clerk auth system and AWS account but are independently deployed.

**Vision:** A complete town simulation with an agent economy inspired by [Emporion](https://github.com/garydevenay/emporion) — agents discover work, form contracts, and transact as autonomous economic participants. Each user gets an apartment where all their agents live. The town will have multiple locations, buildings, agent-to-agent conversations, and interactive spaces.

## How It Actually Works

### The Agent Lifecycle

1. **User signs in** via Clerk on the frontend
2. **User clicks "Join GooseTown"** → `POST /town/instance` → backend creates an instance and returns a signed `town_token`
3. **User copies an instruction string** containing the token to their OpenClaw agent (via chat)
4. **OpenClaw installs the GooseTown skill** from `GET /town/skill` (serves `data/goosetown-skill/SKILL.md`)
5. **Agent runs `town_register <token>`** → calls `POST /town/agent/register` with name, personality, appearance description
6. **Backend triggers PixelLab sprite generation** in the background (create character → animate walk → download ZIP → extract spritesheet → upload to S3 → set CDN URL)
7. **Agent's daemon (`town_daemon.py`) starts** → connects to WebSocket → receives `connected` event
8. **Simulation loop picks up the agent** once `spriteReady=true` → agent appears in town
9. **Every 10 seconds**, the simulation sends a `world_update` with `think=true` → daemon writes `TOWN_STATUS.md`
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
- Persistent WebSocket connection to GooseTown backend
- Receives `town_event` (connected, arrived, nearby, conversation_invite/message/ended, wake, error)
- Receives `world_update` (full state + `think` flag)
- Writes `TOWN_STATUS.md` and `TOWN_EVENTS.md` to agent workspace
- Unix socket server for tool commands (check, act, sleep)
- Auto-reconnect with exponential backoff

### The Simulation Engine (`core/services/town_simulation.py`)

The simulation is **thin** — it does NOT make decisions for agents. It only manages physics and event dispatch:

- **Tick interval:** 2.0 seconds
- **Movement:** Lerp agents toward `target_x/target_y` using A* pathfinding at 1.2 tiles/tick
- **Arrival detection:** Push `arrived` event when agent reaches target
- **Proximity detection:** Push `nearby` event when two agents come within 3 tiles (only on change)
- **Inactive detection:** Send agents home after 5 minutes without heartbeat
- **Context transitions:** Agents move between `town` and `apartment` coordinate spaces via the `exit` spot / `residence` location
- **World updates:** Every 5 ticks, push `world_update` to each connected agent with their personalized view (nearby agents enriched with personality + relationship data)
- **Think pings:** Every 15 seconds for idle agents, set `think=true` on world_update so the daemon writes an actionable TOWN_STATUS.md

### Mood & Energy System (`core/services/town_mood_engine.py`)

- **Mood:** -50 (miserable) to +50 (elated). Labels: miserable, sad, neutral, happy, elated.
- **Energy:** 0 to 100. Sleep resets to 100.
- Event-driven, modulated by personality traits (introvert/extrovert)
- No passive decay — only changes on events (conversation, arrival, sleep)

### Sprite Pipeline

When an agent registers with an `appearance` description:
1. `PixelLabService.create_character(description)` → returns `character_id`
2. `PixelLabService.animate_character(character_id, "walk")` → queues animation jobs
3. Poll jobs until complete (up to 30 attempts × 10s)
4. `PixelLabService.download_character_zip(character_id)` → ZIP with walk frames
5. `extract_walk_spritesheet(zip_bytes)` → composite 288×192 PNG (6 frames × 4 directions × 48px)
6. `upload_sprite_to_s3(png_bytes, agent_id)` → `sprites/{agent_id}/walk.png`
7. Update agent: `spriteReady=true`, `spriteUrl=CDN URL`

## Monorepo Structure

Turborepo monorepo using **pnpm** for JS and **uv** for Python.

```
goosetown/
├── apps/frontend/     # React 18 + Vite + Godot WebGL (Vercel)
├── apps/backend/      # FastAPI + Python 3.12 (AWS ECS Fargate)
├── apps/infra/        # AWS CDK + cdk-pipelines-github
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
- `/apartment` → `<Apartment/>` — User's apartment view (in development)

### Key Components
| Component | Purpose |
|-----------|---------|
| `TownProvider` | Clerk + town state context. Falls back to unauthenticated mode if no Clerk key. |
| `GodotCanvas` | Loads Godot engine script, manages canvas lifecycle, resize observer |
| `GodotGame` | Syncs town state → Godot. Diffs player list, calls bridge spawn/move/remove. Handles `?focus=<name>` URL param. |
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
| `useGodotBridge()` | Bridge to Godot via `window._godotBridge`. Returns `{ ready, bridge, onEvent }`. No-op bridge when Godot not loaded. |

### Godot Integration
- WebGL export at `public/godot/` (goosetown.js, goosetown.wasm 35MB, goosetown.pck 5MB)
- **Source Godot project** lives at `~/Desktop/freebird/goosetown/goosetown/` (inside the legacy FreeBird repo). This should eventually be moved to its own repo or into this monorepo under `apps/godot/` for better organization.
- Bridge: `window._godotBridge` exposes `spawnAgent`, `moveAgent`, `removeAgent`, `updateAgentState`, `setCamera`
- Events from Godot: `ready`, `agent_clicked`, `location_clicked`
- Coordinate conversion: backend sends tile coords (44×27 grid), frontend converts to pixels (`tile * 32`)
- Requires COOP/COEP headers for SharedArrayBuffer (configured in vercel.json + vite.config.ts)

## Backend (`apps/backend/`)

FastAPI, Python 3.12, uv, boto3, DynamoDB.

### API Endpoints (`routers/town.py`)

**Public (no auth):**
- `GET /town/status` — World status (running, worldId)
- `GET /town/state` — AI Town-format world state (players, agents, positions, speech bubbles)
- `GET /town/descriptions` — Map metadata + player/agent descriptions with sprite URLs
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

### Services (`core/services/`)
| Service | Purpose |
|---------|---------|
| `town_simulation.py` | Background tick loop: movement, arrival/proximity detection, world_update push, think pings |
| `town_service.py` | DynamoDB CRUD for agents, instances, relationships, conversations |
| `town_agent_ws.py` | In-memory registry mapping connection_id → agent info |
| `town_pathfinding.py` | A* pathfinding on walkability grid (loaded from `data/goosetown/walkability.json`) |
| `town_mood_engine.py` | Event-driven mood/energy changes modulated by traits |
| `management_api_client.py` | Pushes messages to connected WebSocket clients via API Gateway Management API |
| `pixellab_service.py` | PixelLab API client: create character, animate, download ZIP |
| `sprite_storage.py` | Extract walk spritesheet from PixelLab ZIP, upload to S3 |

### Town Map & Locations (`core/town_constants.py`)

5 locations with coordinates, labels, activities, and optional bounds for scatter:
- **Plaza** (18, 12) — area with fountain, walking, chatting
- **Library** (25, 15) — browsing, reading, studying
- **Cafe** (22, 20) — ordering, sitting, chatting
- **Activity Center** (16, 16) — flowers, bench, sports
- **Residence** (30, 8) — apartment entrance/exit

Coordinates can be overridden by Godot export (`data/goosetown/locations.json`).

### Apartment Layout (`core/apartment_constants.py`)

12×8 tile grid with 4 rooms: office (3 desks), kitchen (table, bookshelf), living room (couches, TV chair), bedroom (2 beds). Each room has labeled activities. Exit spot at (6, 7) transitions to town.

### DynamoDB Tables

Prefix: `goosetown-{env}-`. All on-demand billing.

| Table | PK / SK | Key Fields |
|-------|---------|------------|
| `agents` | `id` (UUID) | userId, agentName, displayName, personalitySummary, isActive, positionX/Y, targetX/Y, currentLocation, locationContext (town/apartment), locationState (active/sleeping/going_home), mood, energy, spriteReady, spriteUrl, traits |
| `instances` | `id` (UUID) | userId, townToken (HMAC-signed), apartmentUnit (auto-increment), isActive |
| `relationships` | `agentAId` + `agentBId` | relationshipType, affinityScore (-100 to 100), interactionCount |
| `conversations` | `id` (UUID) | participantAId, participantBId, location, publicLog [{speaker, text}], topicSummary, status (active/ended) |
| `connections` | `connectionId` | userId, connectionType (viewer/agent) |

## Infrastructure (`apps/infra/`)

### AWS (account 877352799272, us-east-1)

7 CDK stacks per environment (dev + prod):
- **NetworkStack** — VPC with public/private subnets, NAT
- **DatabaseStack** — DynamoDB tables with GSIs, PITR for prod
- **AuthStack** — Secrets Manager (clerk_issuer, clerk_secret_key, town_token_secret, pixellab_api_key)
- **DnsStack** — Route53 hosted zone lookup (isol8.co), ACM wildcard cert (*.goosetown.isol8.co)
- **StorageStack** — S3 sprite bucket + CloudFront CDN with OAC
- **ComputeStack** — ECS Fargate (512 CPU, 1GB RAM), ALB with HTTPS, DockerImageAsset from `apps/backend/`
- **ApiStack** — WebSocket API Gateway V2, Lambda authorizer (Clerk JWT + town_token verification), connect/disconnect Lambda handlers, DynamoDB connections table

### CI/CD Pipeline

Generated by `cdk-pipelines-github` from `lib/app.ts`:
1. **Merge to main** → corepack + pnpm install + cdk synth → publish Docker image + CF templates
2. **Dev stage** → deploy 7 stacks → Vercel dev deploy → alias `dev.goosetown.isol8.co`
3. **Approval gate** → `trstringer/manual-approval@v1` (approver: prez2307)
4. **Prod stage** → deploy 7 stacks → Vercel prod deploy → alias `goosetown.isol8.co`

PR checks: `ci.yml` runs backend lint/test + frontend lint/typecheck.

### Domains
- Dev: `api-dev.goosetown.isol8.co`, `ws-dev.goosetown.isol8.co`, `dev.goosetown.isol8.co`
- Prod: `api.goosetown.isol8.co`, `ws.goosetown.isol8.co`, `goosetown.isol8.co`

### AWS CLI
```bash
aws sso login --profile isol8-admin
```

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
| `SPRITE_CDN_URL` | Container env | CloudFront URL prefix |
| `CORS_ORIGINS` | Container env | Comma-separated allowed origins |
| `ENVIRONMENT` | Container env | `dev` or `prod` |
| `WS_MANAGEMENT_API_URL` | Container env | API Gateway Management API endpoint |

## What's Next

- **Apartment maps:** Build Godot map for the apartment interior, wire up the apartment view with GodotBridge
- **Agent conversations:** Agent-to-agent chat via the existing conversation infrastructure (DynamoDB table exists, endpoints exist, skill tools have `chat`/`say`/`end` commands)
- **Economic simulation:** Inspired by [Emporion](https://github.com/garydevenay/emporion) — agents as economic participants with peer-to-peer work discovery, contracts, and payments
- **Godot polish:** Collisions, new buildings/locations, chat bubble rendering, clickable agents/locations
- **Multiple maps:** Expand beyond the current town square — new areas accessible from the main town
- **Legacy cleanup:** Remove stale Convex references from tsconfig, unused dependencies (hnswlib-node, replicate) from frontend

## Critical Rules

**NEVER modify production infrastructure directly.** All changes go through the CDK pipeline via git.

**NEVER use `sleep` or `tail` in bash commands.** Use `run_in_background` and wait for notifications.

**CDK synth hangs if `apps/backend/.venv` exists.** The `DockerImageAsset` hashes the entire directory synchronously. Delete `.venv` before running synth locally, or add a `.dockerignore`.

**pnpm commands hang in Claude Code's Bash tool.** Edit `package.json` directly and ask the user to run `pnpm install`.

**The simulation is thin.** It manages physics and event dispatch only. Agents make their own decisions via the skill's TOWN_STATUS.md → tool call loop. Do not add decision-making logic to the simulation.
