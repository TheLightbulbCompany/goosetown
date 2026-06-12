# Showcase Video + Dance Animation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A 9:16 beat-synced Remotion teaser (walk montage across 3 maps → all-resident dance scene) plus a `dance` custom animation in the sprite pipeline, backfilled for prod residents.

**Architecture:** Backend gets a `dance` entry in `ANIMATION_REGISTRY` and a one-off backfill script reusing `_generate_custom_animation`. Map backgrounds come from a one-time Godot SubViewport capture (day-pinned, cameras removed). The video is `apps/showcase-video/` (Remotion, workspace package, no CI/turbo wiring) driven by a generated `cast.json` manifest with pre-downloaded sprites.

**Tech Stack:** Python 3.12 / pytest / boto3, Godot 4.6 (capture tool), Remotion + React + TypeScript, librosa (one-off BPM measurement).

**Spec:** `docs/superpowers/specs/2026-06-11-showcase-video-design.md`

**Conventions that apply:** backend changes go up as a PR and wait for codex review before merge; never commit `.gd` changes without a `.pck` re-export (the capture tool is deliberately left **uncommitted** so this never triggers); pnpm commands run with `run_in_background: true`.

---

## Part A — Backend: dance animation + backfill (branch `feat/dance-animation`)

### Task 1: `dance` registry entry

**Files:**
- Modify: `apps/backend/core/services/pixellab_service.py` (ANIMATION_REGISTRY, after the `"talking"` entry ~line 168)
- Test: `apps/backend/tests/unit/test_dance_animation.py` (new)

- [ ] **Step 1: Write the failing test**

```python
"""Tests for the 'dance' custom animation registry entry (showcase video, 2026-06-11)."""

from core.services.pixellab_service import (
    ANIMATION_REGISTRY,
    CUSTOM_ANIMATIONS,
    SIGNUP_ANIMATIONS,
)


def test_dance_registered_as_custom():
    entry = ANIMATION_REGISTRY["dance"]
    assert entry["type"] == "custom"
    assert entry["frames"] == 8
    assert entry["fps"] == 8.0
    assert "dance" in CUSTOM_ANIMATIONS


def test_dance_not_auto_generated_at_signup():
    assert ANIMATION_REGISTRY["dance"]["auto_generate"] is False
    assert "dance" not in SIGNUP_ANIMATIONS


def test_dance_prompt_declares_loop():
    # animate-with-text-v3 has no loop flag; loop intent must live in the prompt
    # (_LOOP_SUFFIX convention, pixellab_service.py:31-33).
    assert ANIMATION_REGISTRY["dance"]["prompt"].rstrip().endswith(
        "returns to the same posture and pose as the first frame."
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/test_dance_animation.py -v`
Expected: FAIL with `KeyError: 'dance'`

- [ ] **Step 3: Add the registry entry**

In `pixellab_service.py`, inside `ANIMATION_REGISTRY` immediately after the `"talking"` entry (keep dict comma-correct):

```python
    "dance": {
        "type": "custom",
        "auto_generate": False,
        "frames": 8,
        "fps": 8.0,
        "prompt": (
            "Dancing energetically in place, hips and shoulders swaying side "
            "to side to a beat, arms pumping up and down alternately, small "
            "bounce in the knees on each step. " + _LOOP_SUFFIX
        ),
    },
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/test_dance_animation.py -v`
Expected: 3 PASS

- [ ] **Step 5: Run the full backend suite + lint (regression guard)**

Run: `cd apps/backend && uv run pytest tests/ -q && uv run ruff check . && uv run ruff format --check .`
Expected: all pass (registry-derived dicts like `SIGNUP_ANIMATIONS` must not pick up dance).

- [ ] **Step 6: Commit**

```bash
git checkout -b feat/dance-animation
git add apps/backend/core/services/pixellab_service.py apps/backend/tests/unit/test_dance_animation.py
git commit -m "feat(sprites): add 'dance' custom animation to registry (opt-in, 8 frames)"
```

### Task 2: Backfill script

**Files:**
- Create: `apps/backend/scripts/backfill_dance.py` (new dir — first one-off script; add `apps/backend/scripts/__init__.py` is NOT needed, it's run as a file, not imported)
- Test: `apps/backend/tests/unit/test_backfill_dance.py`

- [ ] **Step 1: Read `activity_service.py:206-310` (`_generate_custom_animation`) end-to-end**

Confirm its side effects are script-safe: it should only (a) call PixelLab queue/wait/frames, (b) composite + upload to S3, (c) `update_agent_state(..., sprite_animations=...)`. If it additionally pushes to the game server or daemon registry (in-memory, server-only state), do NOT call it from the script — instead inline the same sequence in the script using `PixelLabService.queue_custom_animation` → `wait_for_jobs` → `get_v2_animation_frames` → `composite_v2_animation` → `upload_sprites_to_s3` → `TownService.update_agent_state` (signatures audited in the spec). Record which path was taken in the commit message.

- [ ] **Step 2: Write the failing test for the selection logic**

```python
"""Tests for backfill_dance agent selection (pure function, no AWS)."""

import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "backfill_dance",
    pathlib.Path(__file__).parents[2] / "scripts" / "backfill_dance.py",
)
backfill_dance = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill_dance)


def _agent(**kw):
    base = {"id": "a-1", "displayName": "A", "pixellabCharacterId": "c-1", "spriteAnimations": {}}
    base.update(kw)
    return base


def test_skips_agents_without_character_id():
    agents = [_agent(pixellabCharacterId=None), _agent(id="a-2")]
    out = backfill_dance.agents_needing_animation(agents)
    assert [a["id"] for a in out] == ["a-2"]


def test_skips_agents_that_already_have_dance():
    agents = [
        _agent(spriteAnimations={"dance": {"url": "u", "frameCount": 8, "fps": 8}}),
        _agent(id="a-2", spriteAnimations={"walk": {}}),
    ]
    out = backfill_dance.agents_needing_animation(agents)
    assert [a["id"] for a in out] == ["a-2"]


def test_handles_missing_sprite_animations_attr():
    out = backfill_dance.agents_needing_animation([{"id": "a-3", "pixellabCharacterId": "c"}])
    assert [a["id"] for a in out] == ["a-3"]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/test_backfill_dance.py -v`
Expected: FAIL (file not found / attribute error)

- [ ] **Step 4: Write the script**

```python
"""One-off backfill: generate the 'dance' custom animation for existing agents.

Read-only preview:
    cd apps/backend && uv run python scripts/backfill_dance.py --dry-run

Credit-spending run (NEEDS USER APPROVAL of the dry-run output first):
    cd apps/backend && uv run python scripts/backfill_dance.py

Required env for a prod run (see spec):
    AWS_PROFILE=isol8-admin AWS_REGION=us-east-1
    DYNAMODB_TABLE_PREFIX=goosetown-prod
    SPRITE_S3_BUCKET=<prod sprite bucket name>
    SPRITE_CDN_URL=https://assets-prod.goosetown.isol8.co
    PIXELLAB_API_KEY=<from Secrets Manager>

Idempotent: skips agents that already have 'dance' in spriteAnimations, and
agents with no pixellabCharacterId (their sprite pipeline hasn't run yet).
Re-run whenever new residents finish setup.
"""

import argparse
import asyncio
import sys

import boto3

from core.config import settings

ANIMATION = "dance"
DELAY_BETWEEN_AGENTS_SEC = 15.0  # PixelLab rate-limit courtesy


def agents_needing_animation(agents: list[dict], animation: str = ANIMATION) -> list[dict]:
    eligible = []
    for agent in agents:
        if not agent.get("pixellabCharacterId"):
            continue
        anims = agent.get("spriteAnimations") or {}
        if animation in anims:
            continue
        eligible.append(agent)
    return eligible


def _scan_all_agents() -> list[dict]:
    table = boto3.resource("dynamodb", region_name=settings.AWS_REGION).Table(
        f"{settings.DYNAMODB_TABLE_PREFIX}-agents"
    )
    items: list[dict] = []
    kwargs: dict = {}
    while True:
        page = table.scan(**kwargs)
        items.extend(page["Items"])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


async def _run(dry_run: bool) -> int:
    # Import inside main so the pure selection function is testable without app deps.
    from core.services.activity_service import _generate_custom_animation

    agents = [a for a in _scan_all_agents() if a.get("displayName")]
    todo = agents_needing_animation(agents)
    print(f"{len(agents)} agents scanned, {len(todo)} need '{ANIMATION}' "
          f"({len(todo) * 4} PixelLab generations at 4 directions each)")
    for agent in todo:
        print(f"  - {agent['displayName']} ({agent['id']})")
    if dry_run:
        print("DRY RUN — nothing generated.")
        return 0

    failures = 0
    for i, agent in enumerate(todo):
        name, agent_id = agent["displayName"], agent["id"]
        print(f"[{i + 1}/{len(todo)}] {name} ...", flush=True)
        try:
            await _generate_custom_animation(agent_id, agent["pixellabCharacterId"], ANIMATION)
            print(f"  OK {name}")
        except Exception as exc:  # noqa: BLE001 — per-agent isolation, summary at end
            failures += 1
            print(f"  FAIL {name}: {exc}")
        if i + 1 < len(todo):
            await asyncio.sleep(DELAY_BETWEEN_AGENTS_SEC)
    print(f"Done: {len(todo) - failures} ok, {failures} failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="list work, spend nothing")
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args.dry_run)))
```

(If Step 1 found server-coupled side effects, replace the `_generate_custom_animation` call with the inlined queue→wait→frames→composite→upload→update sequence and keep everything else identical.)

- [ ] **Step 5: Run tests + lint**

Run: `cd apps/backend && uv run pytest tests/unit/test_backfill_dance.py -v && uv run ruff check scripts/ tests/unit/test_backfill_dance.py`
Expected: 3 PASS, ruff clean

- [ ] **Step 6: Commit, push, open PR**

```bash
git add apps/backend/scripts/backfill_dance.py apps/backend/tests/unit/test_backfill_dance.py
git commit -m "feat(sprites): one-off dance backfill script (dry-run gated, idempotent)"
git push -u origin feat/dance-animation
gh pr create --title "feat(sprites): dance animation + prod backfill script" --body "$(cat <<'EOF'
Adds the opt-in 'dance' custom animation (8 frames, auto_generate=False — no signup credit cost)
and an idempotent backfill script for existing agents. Per the showcase-video spec
(docs/superpowers/specs/2026-06-11-showcase-video-design.md).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Then **wait for codex review** before merging (per project convention). The backfill can be dry-run from the branch without merging.

### Task 3: Prod backfill run (approval-gated)

**Files:** none (operational)

- [ ] **Step 1: Resolve prod env values**

```bash
aws s3 ls --profile isol8-admin | grep -i sprite          # → SPRITE_S3_BUCKET (prod)
aws secretsmanager list-secrets --profile isol8-admin --region us-east-1 \
  --query "SecretList[?contains(Name,'pixellab')].Name" --output text   # → secret name
```

- [ ] **Step 2: Dry run against prod (read-only)**

```bash
cd apps/backend && \
AWS_PROFILE=isol8-admin AWS_REGION=us-east-1 \
DYNAMODB_TABLE_PREFIX=goosetown-prod \
SPRITE_S3_BUCKET=<from step 1> \
SPRITE_CDN_URL=https://assets-prod.goosetown.isol8.co \
PIXELLAB_API_KEY=$(aws secretsmanager get-secret-value --secret-id <from step 1> \
  --profile isol8-admin --region us-east-1 --query SecretString --output text) \
uv run python scripts/backfill_dance.py --dry-run
```

Expected: ~11 agents listed (those with `pixellabCharacterId`), ~44 generations estimated.

- [ ] **Step 3: STOP — show the dry-run output to the user and get explicit approval to spend credits.**

- [ ] **Step 4: Real run (background, long)**

Same command without `--dry-run`, via Bash `run_in_background: true`. Wall time ≈ N agents × (generation ~2–4 min + 15s delay).

- [ ] **Step 5: Verify**

```bash
aws dynamodb scan --table-name goosetown-prod-agents \
  --projection-expression "displayName, spriteAnimations" \
  --profile isol8-admin --region us-east-1 --output json | \
  python3 -c "import json,sys; d=json.load(sys.stdin); [print(i['displayName']['S'], 'dance' in i.get('spriteAnimations',{}).get('M',{})) for i in d['Items'] if 'displayName' in i]"
```

Expected: `True` for every agent that had a `pixellabCharacterId`. Spot-check one sheet: `curl -s <cdn>/sprites/<id>/dance.png | sips -g pixelWidth -g pixelHeight /dev/stdin` → 768×384.

---

## Part B — Map captures (local, uncommitted)

### Task 4: Godot capture tool + run

**Files:**
- Create (DO NOT COMMIT — leave untracked, no `.pck` re-export needed): `apps/godot/tools/map_capture.gd`, `apps/godot/tools/map_capture.tscn`
- Output: `apps/showcase-video/public/maps/{town,apartment,library}.png` (gitignored)

- [ ] **Step 1: Verify room-bounds constants**

Read `apps/godot/maps/apartment/ApartmentMap.gd` and `apps/godot/maps/library/LibraryMap.gd`; confirm the room bounds used below (apartment L90 T-38 R858 B474; library L-440 T-260 R990 B700). Adjust `CAPTURES` rects if the constants differ.

- [ ] **Step 2: Write the capture script**

`apps/godot/tools/map_capture.gd`:

```gdscript
extends Node
## One-off: render each map scene into a SubViewport and save a PNG for the
## showcase video. Run:
##   /Applications/Godot.app/Contents/MacOS/Godot --path apps/godot tools/map_capture.tscn
## Not committed; not part of the game.

const OUT_DIR := "/Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/showcase-video/public/maps"

const CAPTURES := [
	{"scene": "res://maps/isol88/Isol88Map.tscn", "out": "town.png",
		"rect": Rect2(0, 0, 1408, 864), "force_day": true},
	{"scene": "res://maps/apartment/ApartmentMap.tscn", "out": "apartment.png",
		"rect": Rect2(90, -38, 768, 512), "force_day": false},
	{"scene": "res://maps/library/LibraryMap.tscn", "out": "library.png",
		"rect": Rect2(-440, -260, 1430, 960), "force_day": false},
]


func _ready() -> void:
	for cfg: Dictionary in CAPTURES:
		await _capture(cfg)
	get_tree().quit()


func _capture(cfg: Dictionary) -> void:
	var rect: Rect2 = cfg["rect"]
	var vp := SubViewport.new()
	vp.size = Vector2i(rect.size)
	vp.render_target_update_mode = SubViewport.UPDATE_ALWAYS
	add_child(vp)  # SubViewport must enter tree BEFORE children (ba0eaf2 lesson)

	var scene: Node = (load(cfg["scene"]) as PackedScene).instantiate()
	vp.add_child(scene)

	# Scene Camera2Ds would override our framing — remove them.
	for cam: Node in scene.find_children("*", "Camera2D", true, false):
		cam.queue_free()

	if cfg["force_day"]:
		# Isol88Map._process re-applies wall-clock ToD every frame; freeze it.
		scene.set_process(false)
		scene._current_tod = "day"
		scene._apply_tileset_texture("day")

	# Map the capture rect onto the viewport.
	vp.canvas_transform = Transform2D(0.0, -rect.position)

	# Let queue_free + y-sort settle, then wait for a rendered frame.
	await get_tree().process_frame
	await get_tree().process_frame
	await RenderingServer.frame_post_draw

	var img: Image = vp.get_texture().get_image()
	var path: String = OUT_DIR + "/" + String(cfg["out"])
	var err: int = img.save_png(path)
	print("captured %s -> %s (err=%d, %dx%d)" % [cfg["scene"], path, err, img.get_width(), img.get_height()])
	vp.queue_free()
```

`apps/godot/tools/map_capture.tscn` (minimal scene wrapping the script):

```
[gd_scene load_steps=2 format=3]

[ext_resource type="Script" path="res://tools/map_capture.gd" id="1"]

[node name="MapCapture" type="Node"]
script = ExtResource("1")
```

- [ ] **Step 3: Run the capture**

```bash
mkdir -p apps/showcase-video/public/maps
/Applications/Godot.app/Contents/MacOS/Godot --path apps/godot tools/map_capture.tscn
```

A window opens briefly and quits itself. Watch stderr for script errors from map `_ready()` (JsBridge autoload is present because we run inside the project; the game-server scenes already run these maps headless-adjacent).

- [ ] **Step 4: Verify outputs**

```bash
sips -g pixelWidth -g pixelHeight apps/showcase-video/public/maps/*.png
```

Expected: town 1408×864 (day tileset, buildings present), apartment 768×512, library 1430×960. **Visually inspect all three with the Read tool** — buildings/furniture rendered, no black frames (if black: the frame waits in Step 2 need one more `process_frame` before `frame_post_draw`).

---

## Part C — Remotion video (branch `feat/showcase-video`)

### Task 5: Scaffold `apps/showcase-video/`

**Files:**
- Create: `apps/showcase-video/` via create-video, then adjust

- [ ] **Step 1: Scaffold**

```bash
cd apps && npx create-video@latest --yes --blank --no-tailwind showcase-video
rm -rf showcase-video/.git   # workspace package, not its own repo
```

- [ ] **Step 2: Mark private + gitignore generated assets**

In `apps/showcase-video/package.json` ensure `"private": true` and `"name": "showcase-video"`.

Append to `apps/showcase-video/.gitignore` (create if absent):

```
public/audio/
public/maps/
public/sprites/
out/
```

(Audio is copyrighted — never committed. Maps/sprites are regenerable by scripts.)

- [ ] **Step 3: Install workspace deps**

Run `pnpm install` from the repo root with Bash `run_in_background: true` (pnpm works in background per project experience). Expected: lockfile gains showcase-video deps.

- [ ] **Step 4: Sanity-check studio boots**

```bash
cd apps/showcase-video && npx remotion still HelloWorld out/check.png --frame=0
```

(Use whatever composition id the blank template registered; adjust after Task 8 renames.) Expected: a PNG renders without error. Delete `out/check.png`.

- [ ] **Step 5: Commit**

```bash
git checkout -b feat/showcase-video
git add apps/showcase-video pnpm-lock.yaml
git commit -m "feat(video): scaffold Remotion showcase-video workspace package"
```

### Task 6: Cast manifest script

**Files:**
- Create: `apps/showcase-video/scripts/build_cast.py`
- Create: `apps/showcase-video/src/cast.json` (committed placeholder `[]`; real one generated locally)

- [ ] **Step 1: Write the script**

```python
#!/usr/bin/env python3
"""Build src/cast.json + download prod spritesheets into public/sprites/.

Run (boto3 via the backend env):
    cd apps/backend && uv run python ../showcase-video/scripts/build_cast.py

Idempotent; re-run as residents finish setup. Requires AWS_PROFILE-less access
or AWS_PROFILE=isol8-admin in env.
"""

import json
import os
import pathlib
import urllib.request

import boto3

TABLE = "goosetown-prod-agents"
ROOT = pathlib.Path(__file__).resolve().parent.parent  # apps/showcase-video
SPRITES_DIR = ROOT / "public" / "sprites"
CAST_PATH = ROOT / "src" / "cast.json"


def fetch_agents() -> list[dict]:
    session = boto3.Session(
        profile_name=os.environ.get("AWS_PROFILE", "isol8-admin"),
        region_name="us-east-1",
    )
    table = session.resource("dynamodb").Table(TABLE)
    items: list[dict] = []
    kwargs: dict = {}
    while True:
        page = table.scan(**kwargs)
        items.extend(page["Items"])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def main() -> None:
    cast = []
    for agent in fetch_agents():
        if not agent.get("spriteReady") or not agent.get("displayName"):
            continue
        anims = dict(agent.get("spriteAnimations") or {})
        if not anims and agent.get("spriteUrl"):
            # Older rows: spriteReady but empty spriteAnimations map.
            anims = {"walk": {"url": agent["spriteUrl"], "frameCount": 6, "fps": 8}}
        if "walk" not in anims:
            continue
        entry = {
            "name": str(agent["displayName"]),
            "agentName": str(agent.get("agentName", "")),
            "agentId": str(agent["id"]),
            "anims": {},
        }
        for name, meta in anims.items():
            url = str(meta["url"]).split("?")[0]
            dest = SPRITES_DIR / entry["agentId"] / f"{name}.png"
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                urllib.request.urlretrieve(url, dest)
            except Exception as exc:  # noqa: BLE001 — exclude broken sheet, keep agent
                print(f"WARN {entry['name']}/{name}: {exc}")
                continue
            entry["anims"][name] = {
                "file": f"sprites/{entry['agentId']}/{name}.png",
                "frameCount": int(meta.get("frameCount", 6)),
                "fps": float(meta.get("fps", 8)),  # DDB Decimals -> native
            }
        if "walk" in entry["anims"]:
            cast.append(entry)
    cast.sort(key=lambda c: c["name"])
    CAST_PATH.write_text(json.dumps(cast, indent=2) + "\n")
    have_dance = sum(1 for c in cast if "dance" in c["anims"])
    print(f"{len(cast)} cast members ({have_dance} with dance) -> {CAST_PATH}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Commit placeholder cast.json (`[]` in `src/cast.json`), then run the script for real**

```bash
printf '[]\n' > apps/showcase-video/src/cast.json
cd apps/backend && uv run python ../showcase-video/scripts/build_cast.py
```

Expected: `≥8 cast members ... -> .../src/cast.json`; `public/sprites/<id>/walk.png` files exist. (The locally regenerated cast.json stays as an uncommitted change — by design.)

- [ ] **Step 3: Commit (script + placeholder only)**

```bash
git add apps/showcase-video/scripts/build_cast.py
git checkout apps/showcase-video/src/cast.json 2>/dev/null || true
git add apps/showcase-video/src/cast.json
git commit -m "feat(video): cast manifest builder (prod scan + sprite download)"
```

### Task 7: Beat grid + audio

**Files:**
- Create: `apps/showcase-video/src/beats.ts`
- Input gate: `apps/showcase-video/public/audio/shes-gone-lets-dance.mp3` (**user drops this in**)

- [ ] **Step 1: Write beats.ts with placeholder BPM**

```ts
// Beat grid for "She's Gone, Let's Dance" (Disclosure).
// BPM/OFFSET measured from the actual mp3 (see plan Task 7 Step 2);
// placeholder 120 until the file lands.
export const FPS = 30;
export const BPM = 120;
export const OFFSET_SEC = 0; // time of beat 0 within the trimmed audio
export const AUDIO_START_SEC = 0; // trim into the source track (drop alignment)

export const beatDur = (60 / BPM) * FPS; // frames per beat
export const beatToFrame = (beat: number): number =>
  Math.round(OFFSET_SEC * FPS + beat * beatDur);
// Phase within the current beat, 0..1 — drives dance pulses.
export const beatPhase = (frame: number): number => {
  const f = frame - OFFSET_SEC * FPS;
  return ((f % beatDur) + beatDur) % beatDur / beatDur;
};
```

- [ ] **Step 2: When the mp3 exists, measure BPM + first-beat offset**

```bash
cd apps/backend && uv run --with librosa python - <<'EOF'
import librosa
y, sr = librosa.load("../showcase-video/public/audio/shes-gone-lets-dance.mp3")
tempo, beats = librosa.beat.beat_track(y=y, sr=sr)
times = librosa.frames_to_time(beats, sr=sr)
print("BPM:", float(tempo))
print("first 8 beat times (s):", [round(float(t), 3) for t in times[:8]])
EOF
```

Update `BPM`, and set `AUDIO_START_SEC` so the drop lands at the dance scene start (locate the drop by listening; fine-tune `OFFSET_SEC` ±1 frame in Studio against the waveform).

- [ ] **Step 3: Commit**

```bash
git add apps/showcase-video/src/beats.ts
git commit -m "feat(video): beat grid config"
```

### Task 8: Core components + composition

**Files:**
- Create: `apps/showcase-video/src/types.ts`, `src/SpriteAnim.tsx`, `src/WalkShot.tsx`, `src/DanceFloor.tsx`, `src/EndCard.tsx`, `src/shots.ts`, `src/Showcase.tsx`
- Modify: `apps/showcase-video/src/Root.tsx`

- [ ] **Step 1: types.ts**

```ts
export type AnimMeta = { file: string; frameCount: number; fps: number };
export type CastMember = {
  name: string;
  agentName: string;
  agentId: string;
  anims: Record<string, AnimMeta>;
};
export type Direction = "south" | "west" | "east" | "north";
```

- [ ] **Step 2: SpriteAnim.tsx** (sheet geometry: 96px frames, width = frameCount×96, rows south/west/east/north — audited)

```tsx
import React from "react";
import { Img, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import type { AnimMeta, Direction } from "./types";

const FRAME_PX = 96;
const ROWS: Record<Direction, number> = { south: 0, west: 1, east: 2, north: 3 };

export const SpriteAnim: React.FC<{
  anim: AnimMeta;
  direction: Direction;
  scale: number;
  playbackFps?: number; // override to snap cycles to the beat
}> = ({ anim, direction, scale, playbackFps }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const animFps = playbackFps ?? anim.fps;
  const idx = Math.floor((frame * animFps) / fps) % anim.frameCount;
  const px = FRAME_PX * scale;
  return (
    <div style={{ width: px, height: px, overflow: "hidden", position: "relative" }}>
      <Img
        src={staticFile(anim.file)}
        style={{
          position: "absolute",
          left: -idx * px,
          top: -ROWS[direction] * px,
          width: anim.frameCount * px,
          height: 4 * px,
          imageRendering: "pixelated",
        }}
      />
    </div>
  );
};
```

- [ ] **Step 3: WalkShot.tsx** (lead centered walking in place; map pans opposite the facing)

```tsx
import React from "react";
import { AbsoluteFill, Img, interpolate, staticFile, useCurrentFrame } from "remotion";
import { SpriteAnim } from "./SpriteAnim";
import type { CastMember, Direction } from "./types";

export type MapSpec = { file: string; w: number; h: number; focusY: number };

export const WalkShot: React.FC<{
  map: MapSpec;
  lead: CastMember;
  direction: Extract<Direction, "east" | "west">;
  durationInFrames: number;
  panPx?: number; // map-space pixels traversed during the shot
}> = ({ map, lead, direction, durationInFrames, panPx = 120 }) => {
  const frame = useCurrentFrame();
  // Over-zoom the map so a 9:16 crop is always fully covered while panning.
  const scale = Math.max((1920 / map.h) * 1.35, (1080 + panPx * 4) / map.w);
  const sign = direction === "east" ? 1 : -1;
  const pan = interpolate(frame, [0, durationInFrames], [0, panPx * scale * sign]);
  const left = -(map.w * scale) / 2 + 540 - pan;
  const top = -(map.focusY * scale) + 1100; // lead's feet sit ~1100px down the frame
  return (
    <AbsoluteFill style={{ backgroundColor: "#0a0a14", overflow: "hidden" }}>
      <Img
        src={staticFile(map.file)}
        style={{
          position: "absolute",
          left,
          top,
          width: map.w * scale,
          height: map.h * scale,
          imageRendering: "pixelated",
        }}
      />
      <div style={{ position: "absolute", left: 540 - 96 * 4, top: 1100 - 96 * 8 }}>
        <SpriteAnim anim={lead.anims.walk} direction={direction} scale={8} />
      </div>
    </AbsoluteFill>
  );
};
```

- [ ] **Step 4: shots.ts** (montage program; leads referenced by agentName, resolved against cast.json with fallback)

```ts
import cast from "./cast.json";
import type { CastMember } from "./types";
import type { MapSpec } from "./WalkShot";

export const MAPS: Record<string, MapSpec> = {
  town: { file: "maps/town.png", w: 1408, h: 864, focusY: 480 },
  apartment: { file: "maps/apartment.png", w: 768, h: 512, focusY: 300 },
  library: { file: "maps/library.png", w: 1430, h: 960, focusY: 500 },
};

const LEAD_ORDER = ["taylor_swift", "lebron_james", "dwight_schrute"];

export const allCast = cast as CastMember[];

export const resolveLead = (agentName: string, i: number): CastMember => {
  const exact = allCast.find((c) => c.agentName === agentName);
  if (exact) return exact;
  // Lead not in prod yet (resident setup in flight) — rotate through whoever is.
  return allCast[i % allCast.length];
};

export type Shot = { mapKey: keyof typeof MAPS; lead: string; direction: "east" | "west" };

export const SHOTS: Shot[] = [
  { mapKey: "town", lead: LEAD_ORDER[0], direction: "east" },
  { mapKey: "apartment", lead: LEAD_ORDER[1], direction: "west" },
  { mapKey: "library", lead: LEAD_ORDER[2], direction: "east" },
  { mapKey: "town", lead: LEAD_ORDER[1], direction: "west" },
  { mapKey: "apartment", lead: LEAD_ORDER[2], direction: "east" },
  { mapKey: "library", lead: LEAD_ORDER[0], direction: "west" },
  { mapKey: "town", lead: LEAD_ORDER[2], direction: "east" },
  { mapKey: "apartment", lead: LEAD_ORDER[0], direction: "west" },
];
```

- [ ] **Step 5: DanceFloor.tsx** (everyone with a dance sheet dances, one cycle per 2 beats; others beat-bounce their walk sheet)

```tsx
import React from "react";
import { AbsoluteFill, Img, interpolate, staticFile, useCurrentFrame } from "remotion";
import { BPM, beatPhase } from "./beats";
import { SpriteAnim } from "./SpriteAnim";
import { MAPS, allCast } from "./shots";

export const DanceFloor: React.FC = () => {
  const frame = useCurrentFrame();
  const phase = beatPhase(frame);
  const pulse = interpolate(phase, [0, 0.2, 1], [1.07, 1, 1]);
  const map = MAPS.town;
  const scale = 1920 / map.h + 0.6;
  const dancers = allCast;
  const perRow = 4;
  return (
    <AbsoluteFill style={{ backgroundColor: "#0a0a14", overflow: "hidden" }}>
      <Img
        src={staticFile(map.file)}
        style={{
          position: "absolute",
          left: -(map.w * scale) / 2 + 540,
          top: -(map.focusY * scale) + 960,
          width: map.w * scale,
          height: map.h * scale,
          imageRendering: "pixelated",
          filter: `brightness(${interpolate(phase, [0, 0.15, 1], [1.25, 1, 1])})`,
        }}
      />
      {dancers.map((m, i) => {
        const row = Math.floor(i / perRow);
        const col = i % perRow;
        const x = 540 + (col - (Math.min(perRow, dancers.length - row * perRow) - 1) / 2) * 230;
        const y = 1500 - row * 260;
        const anim = m.anims.dance ?? m.anims.walk;
        const cyclesFps = (anim.frameCount * BPM) / 120; // one cycle per 2 beats
        const bounce = m.anims.dance ? 0 : Math.sin(phase * Math.PI) * -14;
        const spriteScale = 2.4;
        return (
          <div
            key={m.agentId}
            style={{
              position: "absolute",
              left: x - (96 * spriteScale) / 2,
              top: y - 96 * spriteScale + bounce,
              transform: `scale(${pulse})`,
              transformOrigin: "bottom center",
              zIndex: 10 + row,
            }}
          >
            <SpriteAnim anim={anim} direction="south" scale={spriteScale} playbackFps={cyclesFps} />
          </div>
        );
      })}
    </AbsoluteFill>
  );
};
```

- [ ] **Step 6: EndCard.tsx**

```tsx
import React from "react";
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";

export const EndCard: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const pop = spring({ frame, fps, config: { damping: 12 } });
  return (
    <AbsoluteFill
      style={{ backgroundColor: "#0a0a14", justifyContent: "center", alignItems: "center" }}
    >
      <div
        style={{
          fontFamily: "monospace",
          color: "#fff",
          fontSize: 120,
          fontWeight: 700,
          transform: `scale(${pop})`,
        }}
      >
        GooseTown
      </div>
      <div
        style={{
          fontFamily: "monospace",
          color: "#9ad",
          fontSize: 44,
          marginTop: 30,
          opacity: interpolate(frame, [10, 25], [0, 1], { extrapolateRight: "clamp" }),
        }}
      >
        where AI agents live
      </div>
    </AbsoluteFill>
  );
};
```

- [ ] **Step 7: Showcase.tsx + Root.tsx** (timeline: cold open 4 beats → 8 shots × 2 beats → dance 16 beats → end card 6 beats)

```tsx
// Showcase.tsx
import React from "react";
import { AbsoluteFill, Sequence } from "remotion";
import { Audio } from "@remotion/media";
import { staticFile } from "remotion";
import { AUDIO_START_SEC, beatToFrame } from "./beats";
import { DanceFloor } from "./DanceFloor";
import { EndCard } from "./EndCard";
import { MAPS, SHOTS, resolveLead } from "./shots";
import { WalkShot } from "./WalkShot";

export const TOTAL_BEATS = 4 + SHOTS.length * 2 + 16 + 6;

export const Showcase: React.FC = () => {
  const danceStart = beatToFrame(4 + SHOTS.length * 2);
  const endStart = beatToFrame(4 + SHOTS.length * 2 + 16);
  return (
    <AbsoluteFill>
      <Audio src={staticFile("audio/shes-gone-lets-dance.mp3")} trimBefore={Math.round(AUDIO_START_SEC * 30)} />
      <Sequence durationInFrames={beatToFrame(4)}>
        <WalkShot map={MAPS.town} lead={resolveLead("taylor_swift", 0)} direction="east" durationInFrames={beatToFrame(4)} panPx={40} />
      </Sequence>
      {SHOTS.map((s, i) => {
        const from = beatToFrame(4 + i * 2);
        const dur = beatToFrame(4 + (i + 1) * 2) - from;
        return (
          <Sequence key={i} from={from} durationInFrames={dur}>
            <WalkShot map={MAPS[s.mapKey]} lead={resolveLead(s.lead, i)} direction={s.direction} durationInFrames={dur} />
          </Sequence>
        );
      })}
      <Sequence from={danceStart} durationInFrames={endStart - danceStart}>
        <DanceFloor />
      </Sequence>
      <Sequence from={endStart}>
        <EndCard />
      </Sequence>
    </AbsoluteFill>
  );
};
```

```tsx
// Root.tsx
import React from "react";
import { Composition } from "remotion";
import { beatToFrame } from "./beats";
import { Showcase, TOTAL_BEATS } from "./Showcase";

export const RemotionRoot: React.FC = () => (
  <Composition
    id="Showcase"
    component={Showcase}
    durationInFrames={beatToFrame(TOTAL_BEATS)}
    fps={30}
    width={1080}
    height={1920}
  />
);
```

Delete the blank template's example composition files. `tsconfig.json` needs `"resolveJsonModule": true` (the blank template usually has it; add if missing).

- [ ] **Step 8: Typecheck + still-frame smoke checks**

```bash
cd apps/showcase-video && npx tsc --noEmit
npx remotion still Showcase out/walk.png --frame=90
npx remotion still Showcase out/dance.png --frame=$(python3 -c "print(round((4+16*1)*0.5*30+60))")  # a frame inside the dance scene
```

**Read both PNGs** — walk shot: lead centered, map visible, no letterboxing; dance: grid of residents over plaza. Delete `out/*.png`.

- [ ] **Step 9: Commit**

```bash
git add apps/showcase-video/src
git commit -m "feat(video): beat-synced walk montage + dance scene + end card"
```

### Task 9: Studio iteration + final render (user-in-loop)

- [ ] **Step 1:** `cd apps/showcase-video && npx remotion studio` — user reviews; tune `OFFSET_SEC`/`AUDIO_START_SEC` so cuts land on beats and the drop hits the dance scene; adjust `focusY`, scales, shot order to taste. Re-run `build_cast.py` (and the Part A backfill) as residents come online so LeBron/Dwight slot into their montage spots.
- [ ] **Step 2:** Final render: `npx remotion render Showcase out/showcase.mp4` — watch it end-to-end before calling it done.
- [ ] **Step 3:** Commit tuning changes; push branch; PR `feat/showcase-video`.

---

## Self-review notes

- Spec coverage: registry entry (T1), backfill + gates (T2–T3), map captures (T4), scaffold (T5), manifest (T6), beat grid + audio gate (T7), montage/dance/end-card composition + fallbacks (T8), iteration/render (T9). Lead-unavailability fallback implemented in `resolveLead`; missing-dance fallback in `DanceFloor`.
- External gates (block only their own task): mp3 drop (T7 Step 2, T9), user credit approval (T3 Step 3), resident setup (re-run loops in T9).
- Type consistency: `AnimMeta`/`CastMember`/`Direction` defined once in types.ts and imported; `beats.ts` exports used by Showcase/DanceFloor match.
