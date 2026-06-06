# GooseTown Frontend

React 18 + TypeScript + Vite + Tailwind, with an embedded Godot WebGL view of the town. Auth via Clerk; deployed to Vercel.

> This app began as a fork of [AI Town](https://github.com/a16z-infra/ai-town) but no longer uses Convex, Ollama, or the AI-Town backend. GooseTown's backend is FastAPI on AWS Fargate and the world renders via a Godot WebGL build. See the repo-root `CLAUDE.md` for the full architecture.

## Develop

```bash
pnpm install        # from the repo root
cd apps/frontend
pnpm dev            # http://localhost:5173
```

### Checks

```bash
pnpm lint
pnpm typecheck
```

## Environment

| Variable | Purpose |
|----------|---------|
| `VITE_BACKEND_URL` | Backend API URL including `/api/v1` |
| `VITE_WS_URL` | WebSocket URL (unset = REST-only viewer mode) |
| `VITE_CLERK_PUBLISHABLE_KEY` | Clerk publishable key (absent = unauthenticated viewer mode) |

## Godot WebGL

The town renders from a Godot WebGL export in `public/godot/` (`goosetown.{html,js,wasm,pck}`); the source project lives in `apps/godot/`. `GodotCanvas` loads the engine and passes the viewer WebSocket URL to Godot via `window._goosetown_ws_url`. COOP/COEP headers (for `SharedArrayBuffer`) are configured in `vercel.json` + `vite.config.ts`. See CLAUDE.md → "Re-exporting the WebGL build" for the headless export command.

## Key pieces

- `TownProvider` — Clerk + town state context
- `GodotCanvas` / `GameLayout` — the embedded town view + sidebar
- `useTownState` / `useApartment` — REST polling (+ WebSocket when authenticated)
- `scenes.ts` — single source of truth for interior scenes (coupled by `id` with the Godot `SCENE_REGISTRY`)
