# Technical Preferences — GooseTown

## Engine & Language

- **Engine**: Godot 4.6 (headless game server + WebGL frontend)
- **Language**: GDScript (game), Python 3.12 (backend), TypeScript (frontend)
- **Rendering**: 2D isometric pixel art (WebGL export)
- **Physics**: NavigationRegion2D with navmesh exclusions

## Architecture

- **Game Server**: Godot headless on ECS Fargate (WebSocket server)
- **Backend**: FastAPI on ECS Fargate (DynamoDB, S3, API Gateway WebSocket)
- **Frontend**: React 18 + Vite + Godot WebGL canvas (Vercel)
- **Sprite Pipeline**: PixelLab API (4-direction, 96x96) → resize 48x48 → spritesheet → S3 → CloudFront CDN

## Naming Conventions

- **GDScript Classes**: PascalCase
- **GDScript Variables**: snake_case
- **GDScript Signals**: snake_case (past tense: `agent_arrived`)
- **Python**: snake_case (PEP 8)
- **TypeScript**: camelCase (variables/functions), PascalCase (components/types)
- **Files**: snake_case (GDScript/Python), kebab-case (TypeScript)
- **Constants**: SCREAMING_SNAKE_CASE

## Performance Budgets

- **Target Framerate**: 30fps (headless server), 60fps (WebGL client)
- **WebGL Binary**: <40MB (.wasm + .pck)
- **Tile Size**: 32px
- **Sprite Frame Size**: 48x48px (96x96 source from PixelLab, downscaled)
- **Max Concurrent Agents**: 50 per town instance

## Testing

- **Backend**: pytest (unit + integration)
- **Frontend**: ESLint + TypeScript strict mode
- **Infrastructure**: CDK synth validation
- **Required Tests**: API endpoints, DynamoDB operations, token auth

## Forbidden Patterns

- Hardcoded production URLs (use environment variables)
- `sleep` or `tail` in bash commands (use `run_in_background`)
- Decision-making logic in the simulation (agents decide via TOWN_STATUS.md)
- Direct production infrastructure modifications (use CDK pipeline)
- `.venv` in `apps/backend/` when running CDK synth

## Allowed Libraries / Addons

- **GDScript**: NavigationServer2D, WebSocketPeer, HTTPRequest
- **Python**: FastAPI, boto3, httpx, Pillow, websockets
- **TypeScript**: React, Vite, Tailwind CSS, Clerk
- **Infrastructure**: AWS CDK, cdk-pipelines-github
- **Art Generation**: PixelLab API v2 (4-direction characters, tile generation)

## Architecture Decisions Log

- Game server moved from Python simulation to Godot headless (2026-03-25)
- PixelLab switched from 8-direction to 4-direction endpoint (2026-03-26)
- Sprite source size increased to 96x96 for higher detail (2026-03-26)
