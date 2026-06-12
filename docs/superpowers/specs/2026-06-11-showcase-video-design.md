# GooseTown Showcase Video + Dance Animation — Design

**Date:** 2026-06-11
**Status:** Approved pending user review
**Scope:** (1) A 9:16 social teaser video built in Remotion; (2) a `dance` custom animation added to the sprite pipeline with a one-off prod backfill.

## Goal

A ~25s vertical (1080×1920, 30fps) social teaser set to Disclosure's "She's Gone, Let's Dance":
a beat-synced walk montage cutting between three maps and three lead characters, breaking into
a dance scene with all prod residents at the drop, closing on a GooseTown end card.

## Audit-verified facts this design rests on

- Spritesheets are **96px frames, 4 rows** (south=0, west=1, east=2, north=3); sheet width =
  `frameCount × 96`. Walk/idle are 6 frames (576×384, confirmed against Taylor Swift's live prod
  sheet); an 8-frame dance will be 768×384. CLAUDE.md's 288×192/48px note is stale (pre-`afaff3f`).
- Per-animation URLs + frameCount + fps live in the agent row's `spriteAnimations` map. Older rows
  may have `spriteReady=true` with an empty map → fall back to `spriteUrl` (walk only).
- PixelLab has **no dance template** — dance must be a `type: "custom"` (animate-with-text-v3)
  registry entry, like `performing`/`exercising`.
- The lazy custom-animation generator `_generate_custom_animation(agent_id, character_id, name)`
  (`activity_service.py:206`) generates exactly one animation, merges into `spriteAnimations`,
  and is safe to call for existing agents. `/admin/regenerate-sprite` is the wrong tool (dev-only,
  recreates the whole character).
- Maps are TileMapLayer terrain + Sprite2D buildings/furniture; there is no existing capture
  tooling. The town map swaps tileset textures by wall-clock hour (dawn/day/dusk/night) — captures
  must pin "day". Map sizes: town 1408×864, apartment ~768×512, library 1430×960.
- Prod cast (at audit time): 19 agents, 8 spriteReady, 11 with `pixellabCharacterId`. LeBron James
  and Dwight Schrute (two of the three leads) have no sprites yet — **residents are still being
  set up; their sprites will arrive via the normal registration pipeline**. The video build does
  not block on them; the cast manifest is re-runnable.

## Part 1 — Remotion project (`apps/showcase-video/`)

Private workspace package (React + TypeScript + Remotion). Not wired into turbo tasks or CI.

### Inputs

| Input | Source |
|---|---|
| `public/audio/shes-gone-lets-dance.mp3` | **User drops this in** (rights/clearance on user). |
| `public/maps/{town,apartment,library}.png` | One-time Godot capture script (below). |
| `public/sprites/{agentId}/{anim}.png` | Pre-downloaded from prod CDN by manifest script (deterministic renders, no network at render time). |
| `src/cast.json` | Generated manifest: per agent `{name, agentId, anims: {name: {file, frameCount, fps}}}`. |

### Scripts

- **`scripts/build-cast.ts`** — scans `goosetown-prod-agents` (AWS profile `isol8-admin`), reads
  `spriteAnimations` (fallback: `spriteUrl` → walk@6 frames), downloads each sheet into
  `public/sprites/`, writes `src/cast.json`. Idempotent; re-run as residents come online.
- **Godot capture** — a small temporary scene/script run via the local Godot binary that loads each
  map scene, pins town time-of-day to "day", waits one frame, renders the full scene tree
  (terrain + YSortedObjects) to an image at 2× native resolution, saves PNG, quits. Run three
  times; outputs land in `public/maps/`. Nothing committed to the game project.

### Beat grid

`src/beats.ts`: `{bpm, offsetSec, audioStartSec}` measured from the actual mp3 (tap/onset check in
Remotion Studio against the waveform). Helper `beatToFrame(n)`. All cuts and dance pulses derive
from it. The track section is chosen so the drop lands where the dance scene starts.

### Composition (~25s)

1. **Cold open** (0 → ~beat 4): town capture, slow push-in, "GooseTown" wordmark small.
2. **Walk montage** (~16 beats): hard cut every 2 beats through (map × lead) combos —
   Taylor/LeBron/Dwight rotating across town/apartment/library. Lead walks in place center-frame;
   background pans opposite the facing direction. Facing varies per cut (east/west rows mostly).
3. **Drop → dance scene** (~8–10s): town plaza background, all residents with a dance sheet in
   staggered rows, lead trio front-center. Dance playback speed snapped so one cycle = 2 beats;
   scale-pulse on every beat. Residents lacking `dance.png` get a beat-bounced walk frame
   (video never blocks on the backfill).
4. **End card** (~3s): wordmark + URL on the final beat.

### Sprite rendering

`<SpriteAnim>` component: `<Img>` of the full sheet inside an overflow-hidden 96×96 box,
`background-position` stepped by `Math.floor(frame / (30 / playbackFps)) % frameCount`,
row picked by direction, `imageRendering: 'pixelated'`, scaled ~6–8×. frameCount/fps come from
`cast.json` — never hardcoded.

## Part 2 — Sprite pipeline: `dance`

### Registry entry (`pixellab_service.py`)

```python
"dance": {
    "type": "custom",
    "auto_generate": False,   # opt-in: no credit cost on future signups
    "frames": 8,
    "fps": 8.0,
    "prompt": (
        "Dancing energetically in place, hips and shoulders swaying side to "
        "side to a beat, arms pumping up and down alternately, small bounce "
        "in the knees on each step. " + _LOOP_SUFFIX
    ),
},
```

Unit test alongside existing registry tests (entry exists, custom type, not in SIGNUP_ANIMATIONS).

### Backfill script (`apps/backend/scripts/backfill_dance.py`)

- Scans the agents table; for each agent **with `pixellabCharacterId`** and **without `dance` in
  `spriteAnimations`**, calls `_generate_custom_animation(agent_id, character_id, "dance")`.
- Sequential with a small delay between agents (PixelLab rate-limit courtesy).
- `--dry-run` prints the work list and estimated generations (4 per agent) without spending credits.
- Env for prod run: `DYNAMODB_TABLE_PREFIX=goosetown-prod`, `SPRITE_S3_BUCKET`, `SPRITE_CDN_URL`,
  `PIXELLAB_API_KEY` (from Secrets Manager), `AWS_PROFILE=isol8-admin`.
- **User approves the dry-run output before the credit-spending run.**
- Re-runnable: residents who register later are picked up on the next invocation (skip-existing
  makes it idempotent).

## Error handling

- Manifest script: missing/erroring sprite downloads → warn and exclude agent from cast.json.
- Backfill: per-agent try/except, summary table at end (ok / skipped / failed + reason).
- Video: any cast member with missing dance falls back to beat-bounced walk; missing walk → excluded.

## Testing

- Backend: registry unit test; backfill `--dry-run` exercised against prod read-only.
- Video: lint + `tsc`; correctness is visual — iterate in Remotion Studio, final check is a full
  `remotion render` watched end-to-end before posting.

## Order of work

1. Registry entry + backfill script → PR (small, reviewable).
2. Backfill dry-run → user approval → run against prod (long pole, runs in background).
3. Godot map captures (local, one-time).
4. Remotion scaffold + cast manifest + components; iterate in Studio.
5. As LeBron/Dwight/others come online: re-run manifest + backfill, slot them in.
6. Final render.

## Open items (user-owned)

- Drop the mp3 at `apps/showcase-video/public/audio/shes-gone-lets-dance.mp3`.
- Resident setup (LeBron, Dwight, +6 others) proceeding on the user's side.
- Approval gate before the credit-spending backfill run.
