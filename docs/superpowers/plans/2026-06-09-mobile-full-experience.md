# Mobile Full-Experience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make GooseTown fully usable from a phone — full functionality (watch town, manage agents, join flow, owner chat), desktop-level polish not required — with a fast, progress-visible first load.

**Architecture:** Verification-first. Phase 0 runs cheap spikes that confirm or kill each assumption (touch-pan works via Godot's mouse emulation, `onProgress` fires, versioned-dir caching works, slim engine build is worth it). Phases 1–3 then execute: responsive layout via Tailwind `md:` breakpoint (mobile = canvas-first with bottom-sheet drawers), pinch-zoom in the Godot map, real download progress + immutable caching. The slim custom export template (the big wasm cut) gets its own follow-up plan, gated on the Phase 0 feasibility memo.

**Tech Stack:** React 18 + Tailwind (existing), Vitest + @testing-library/react (existing), Playwright (existing), Godot 4.6 GDScript, vercel.json headers.

**Branch:** `feat/mobile-full-experience` off `main`.

---

## Context an engineer needs (verified 2026-06-09, do not re-derive)

- Wire sizes today on dev: wasm 36MB → **8.9MB brotli**, pck 8.2MB → **6.8MB**. Vercel compresses automatically; nothing to do there.
- `.pck` weight is mostly already-compressed PNG tilesets (`apps/godot/assets/isol88` 5.8MB — four time-of-day variants — and `assets/library` 2.5MB). **Decision: no pck diet in this plan**; it's Phase-4-art territory and brotli can't help PNGs. The wasm is the only worthwhile size lever (follow-up plan).
- `apps/frontend/public/godot/goosetown.js` **does support `onProgress`** in the Engine config (verified by grep: `onProgress = parse('onProgress'`).
- `apps/godot/project.godot` has **no** `input_devices/pointing/emulate_mouse_from_touch` override → Godot default **true** applies → single-finger touch *should* emit emulated mouse events and pan via the existing `InputEventMouseButton`/`Motion` path in `Isol88Map.gd:88-98`. Phase 0 V1 verifies this on an emulated touch device.
- The React app loads the engine via `GodotCanvas.tsx` (`/godot/goosetown.js`), NOT via `public/godot/goosetown.html` — that shell file is dead weight for the React path; its settings don't apply.
- Known-broken CI signal: `e2e/smoke.spec.ts:76` fails on every PR (looks for canvas at `/`, but canvas moved to `/town` in PR #254). **Out of scope here** — do not "fix it while you're in there"; it's tracked as its own launch blocker. Expect that job to be red on your PRs like everyone else's.
- Critical rule: **never commit `.gd` changes without the regenerated `.pck`** (Phase 2 Task G2 does the export; commit them together).
- Critical rule: pnpm via the Bash tool needs `run_in_background: true` (foreground pnpm hangs).
- Run frontend checks from `apps/frontend/`: `pnpm vitest run` (unit), `pnpm lint`, `pnpm typecheck`.

---

## Phase 0 — Verification spikes (run first; each emits a go/no-go)

### Task V1: Does touch already pan the town camera?

Confirms or refutes: "Godot's default mouse-from-touch emulation gives us drag-to-pan for free on phones."

**Files:**
- Create: `apps/frontend/e2e/mobile-touch-probe.spec.ts` (a probe, **not** added to CI)

- [ ] **Step 1: Write the probe spec**

```ts
// apps/frontend/e2e/mobile-touch-probe.spec.ts
//
// Verification probe (Phase 0 V1 of the mobile plan) — NOT a CI test.
// Run manually: pnpm exec playwright test e2e/mobile-touch-probe.spec.ts --project=chromium
// Uses Pixel 5 profile (chromium-based) because CDP touch dispatch needs Chromium.
import { test, expect, devices } from '@playwright/test';

const DEV_URL = 'https://dev.goosetown.isol8.co';

test.use({ ...devices['Pixel 5'] });

test('town loads on mobile and touch-drag pans the camera', async ({ page, context }) => {
  test.setTimeout(180_000);
  await page.goto(`${DEV_URL}/town`);

  // Engine boot: loading overlay disappears when startGame resolves.
  await expect(page.getByText('Loading GooseTown...')).toBeHidden({ timeout: 150_000 });
  const canvas = page.locator('#godot-canvas');
  await expect(canvas).toBeVisible();
  await page.waitForTimeout(2_000); // let first world_state paint

  const before = await canvas.screenshot();

  // Synthetic single-finger drag via CDP (Playwright's high-level API can't swipe).
  const client = await context.newCDPSession(page);
  const box = (await canvas.boundingBox())!;
  const cx = box.x + box.width / 2;
  const cy = box.y + box.height / 2;
  await client.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: cx, y: cy }] });
  for (let i = 1; i <= 12; i++) {
    await client.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: cx - i * 20, y: cy }] });
  }
  await client.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  await page.waitForTimeout(500);

  const after = await canvas.screenshot();
  // If the camera panned, the rendered frame must differ. (Agents moving can
  // also cause a diff — interpret a PASS as "probably works, confirm on a real
  // phone"; interpret a FAIL as "emulated pan is broken, build native touch pan".)
  expect(Buffer.compare(before, after)).not.toBe(0);
});
```

- [ ] **Step 2: Run the probe**

Run from `apps/frontend/` (background per pnpm rule): `pnpm exec playwright test e2e/mobile-touch-probe.spec.ts --project=chromium`
Expected: PASS (screenshots differ) → emulated pan works.

- [ ] **Step 3: Real-device confirmation (user-assisted, 2 min)**

Ask the user to open `https://dev.goosetown.isol8.co/town` on their actual phone and check: page loads, agents render, one-finger drag pans. Record the result in the PR description.

- [ ] **Step 4: Record the gate**

- PASS → Phase 2 Task G1 implements **pinch-zoom only** (Branch A).
- FAIL → Phase 2 Task G1 implements **native touch pan + pinch-zoom** (Branch B).

- [ ] **Step 5: Commit the probe**

```bash
git add apps/frontend/e2e/mobile-touch-probe.spec.ts
git commit -m "test(mobile): add touch-pan verification probe (not in CI)"
```

### Task V2: Does `onProgress` actually fire with usable numbers?

**Files:**
- Modify (temporarily): `apps/frontend/src/components/GodotCanvas.tsx:181-185`

- [ ] **Step 1: Add a temporary probe log to the Engine config**

In `GodotCanvas.tsx`, inside `new window.Engine({...})` add:

```ts
          onProgress: (current: number, total: number) => {
            console.log('[V2 probe] onProgress', current, total);
          },
```

(TypeScript: add `onProgress?: (current: number, total: number) => void;` to the `GodotEngineConstructor` config interface at line ~98 — this edit is kept; it's needed by Task L1 anyway.)

- [ ] **Step 2: Run the dev server and observe**

Run from `apps/frontend/` (background): `pnpm dev`. Open `http://localhost:5173/town` in a browser, watch the console.
Expected: a stream of `[V2 probe] onProgress <bytes> <totalBytes>` lines with `total > 0`.
Known failure mode: Godot's loader sometimes reports `total = 0` when the server doesn't send Content-Length for compressed responses. Note which case we're in.

- [ ] **Step 3: Record the gate, revert the console.log (keep the type)**

- `total > 0` → Task L1 renders a percentage bar.
- `total === 0` → Task L1 hardcodes the known compressed total (~16.2MB) as the denominator with a code comment, or falls back to the indeterminate bar. Record which.

### Task V3: Does the versioned-godot-dir build work locally?

Confirms the Task L2 mechanism (immutable caching needs cache-busting; the engine derives `goosetown.wasm`/`.pck` paths from the `executable` path, so a versioned *directory* is the only clean bust).

- [ ] **Step 1: Dry-run the mechanism by hand**

From `apps/frontend/` (background): run the existing production build (check `package.json:scripts.build` first and use it as-is), then:

```bash
mv dist/godot dist/_godot_tmp && mkdir dist/godot && mv dist/_godot_tmp dist/godot/testsha123
```

- [ ] **Step 2: Point the canvas at the versioned path and preview**

Temporarily edit `GodotCanvas.tsx:14` → `const GODOT_JS_PATH = '/godot/testsha123/goosetown.js';` and line ~182 `executable: '/godot/testsha123/goosetown'`. Rebuild, then (background): `pnpm exec vite preview`. Open the preview URL `/town`.
Expected: engine boots, no 404s in the network tab (verify `goosetown.wasm`, `.pck`, and **both audio worklet files** load from `/godot/testsha123/`).

- [ ] **Step 3: Record the gate, revert the temporary edits**

- All assets load from the versioned dir → Task L2 proceeds as written.
- Worklets 404 (loaded from a hardcoded path) → Task L2 must also copy worklets or keep them at `/godot/` unversioned; record what broke.

### Task V4: Slim export template — feasibility memo only

**Files:**
- Create: `docs/superpowers/specs/2026-06-09-slim-godot-template-feasibility.md`

- [ ] **Step 1: Check toolchain availability**

```bash
which scons emcc; emcc --version 2>/dev/null | head -1; python3 --version
```

- [ ] **Step 2: Write the feasibility memo**

Document in the memo (web research as needed — official docs: https://docs.godotengine.org/en/4.6/contributing/development/compiling/compiling_for_web.html and "Optimizing a build for size"):
- The build command for a size-optimized 2D-only web template, e.g. `scons platform=web target=template_release production=yes optimize=size threads=no build_profile=goosetown.build` with a `goosetown.build` profile disabling 3D, physics_3d, navigation_3d (server), openxr, csg, gridmap, and other unused modules.
- Where the resulting `.zip` template plugs into `apps/godot/export_presets.cfg` (the `custom_template/release` key).
- Expected size cut (published community results for 2D-only builds: ~35MB → ~15-20MB raw).
- Whether to build locally or in CI (a GitHub Actions job caching emsdk is the reproducible option).
- **Go/no-go recommendation.** Gate: if projected wire size cut < 30%, recommend dropping it.

This plan does NOT build the template — that's a follow-up plan (`2026-06-XX-slim-godot-template.md`) authored only if the memo says go.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-06-09-slim-godot-template-feasibility.md
git commit -m "docs(mobile): slim web-template feasibility memo (phase 0 spike)"
```

---

## Phase 1 — Responsive layout (full functionality at 375px)

Pattern for all tasks: mobile-first classes are the default, desktop restores current behavior under `md:` (768px). The product call (user-approved): on phones the **canvas is the screen**; sidebar and chat become bottom sheets.

### Task T1: GameLayout — bottom-sheet agents drawer on mobile

**Files:**
- Modify: `apps/frontend/src/components/GameLayout.tsx` (whole file, currently 21 lines)
- Test: `apps/frontend/src/components/GameLayout.test.tsx` (create)

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/src/components/GameLayout.test.tsx
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect } from 'vitest';
import GameLayout from './GameLayout';

describe('GameLayout mobile drawer', () => {
  it('renders an Agents toggle that opens and closes the drawer', async () => {
    const user = userEvent.setup();
    render(
      <GameLayout sidebar={<div>SIDEBAR CONTENT</div>}>
        <div>MAP</div>
      </GameLayout>,
    );

    const toggle = screen.getByRole('button', { name: /agents/i });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');

    await user.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');

    await user.click(screen.getByRole('button', { name: /close agents panel/i }));
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
  });

  it('always renders sidebar content (CSS hides it on mobile, not React)', () => {
    render(
      <GameLayout sidebar={<div>SIDEBAR CONTENT</div>}>
        <div>MAP</div>
      </GameLayout>,
    );
    expect(screen.getByText('SIDEBAR CONTENT')).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run it, verify it fails**

From `apps/frontend/` (background): `pnpm vitest run src/components/GameLayout.test.tsx`
Expected: FAIL — no button named /agents/i.

- [ ] **Step 3: Implement**

Replace `GameLayout.tsx` with:

```tsx
import { useState } from 'react';
import type { ReactNode } from 'react';

interface GameLayoutProps {
  sidebar: ReactNode;
  children: ReactNode;
}

export default function GameLayout({ sidebar, children }: GameLayoutProps) {
  // Mobile only: sidebar renders as a slide-up bottom sheet. On md+ the
  // drawerOpen state is inert — md: classes pin the sidebar to its
  // original static-column layout regardless.
  const [drawerOpen, setDrawerOpen] = useState(false);

  return (
    <div className="flex w-full h-full">
      {/* Agents sidebar — static column on md+, bottom sheet on mobile */}
      <div
        className={
          drawerOpen
            ? 'fixed inset-x-0 bottom-0 top-[35%] z-40 flex flex-col overflow-y-auto px-4 py-4 rounded-t-xl border-t border-clay-700 bg-clay-900 text-brown-100 shadow-2xl ' +
              'md:static md:inset-auto md:z-auto md:w-80 md:shrink-0 md:rounded-none md:border-t-0 md:border-r md:shadow-none'
            : 'hidden md:flex flex-col overflow-y-auto shrink-0 w-80 px-4 py-4 border-r border-clay-700 bg-clay-900 text-brown-100'
        }
      >
        <button
          aria-label="Close agents panel"
          onClick={() => setDrawerOpen(false)}
          className="md:hidden self-end -mt-1 mb-1 px-3 py-1 text-clay-300 text-2xl leading-none"
        >
          ×
        </button>
        {sidebar}
      </div>

      {/* Map area */}
      <div className="relative flex-1 overflow-hidden bg-brown-900">
        {children}
        {/* Mobile drawer toggle — floats over the canvas */}
        <button
          aria-expanded={drawerOpen}
          onClick={() => setDrawerOpen(true)}
          className="md:hidden absolute bottom-4 left-1/2 -translate-x-1/2 z-30 px-5 py-2.5 bg-clay-800/95 border border-clay-600 rounded-full font-display text-sm tracking-wider text-brown-100 shadow-lg"
        >
          Agents
        </button>
      </div>
    </div>
  );
}
```

Note: the close `×` is inside the sheet (the toggle pill sits at z-30 *under* the z-40 sheet while open, by design). The `×` is `md:hidden` so desktop is pixel-identical to today.

- [ ] **Step 4: Run the test, verify it passes**

`pnpm vitest run src/components/GameLayout.test.tsx` (background). Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/GameLayout.tsx apps/frontend/src/components/GameLayout.test.tsx
git commit -m "feat(mobile): GameLayout sidebar becomes bottom-sheet drawer below md"
```

### Task T2: OwnerChatPanel — bottom sheet on mobile

**Files:**
- Modify: `apps/frontend/src/components/OwnerChatPanel.tsx:41` (container) and `:70` (bubble width)

- [ ] **Step 1: Make the container responsive**

Replace line 41's className:

```tsx
    <div className="fixed z-30 bg-clay-900 flex flex-col shadow-2xl inset-x-0 bottom-0 top-[30%] rounded-t-xl border-t border-clay-700 md:inset-x-auto md:bottom-auto md:top-0 md:right-0 md:h-full md:w-[360px] md:rounded-none md:border-t-0 md:border-l">
```

(Percentage-based `top-[30%]` deliberately instead of `dvh` — works on every browser including iOS 15, no dynamic-viewport edge cases.)

- [ ] **Step 2: Make bubbles proportional**

Line 70: change `max-w-[280px]` → `max-w-[80%]`.

- [ ] **Step 3: Typecheck + visual check**

From `apps/frontend/` (background): `pnpm typecheck`. Expected: clean.
With `pnpm dev` running, in Chrome DevTools device toolbar (375×812): open chat from an agent card → panel rises from the bottom, input visible, no horizontal scroll. At ≥768px: identical to today (right-hand 360px panel).

- [ ] **Step 4: Commit**

```bash
git add apps/frontend/src/components/OwnerChatPanel.tsx
git commit -m "feat(mobile): owner chat renders as bottom sheet below md"
```

### Task T3: TownNav — scrollable compact nav on mobile

**Files:**
- Modify: `apps/frontend/src/components/TownNav.tsx:20-44`

- [ ] **Step 1: Implement**

- Line 20 nav: `className="flex items-center gap-2 md:gap-4 px-3 md:px-4 py-2 bg-clay-900 border-b border-clay-700 z-20 shrink-0 overflow-x-auto"`
- Line 21 title span: `className="font-display text-lg md:text-xl tracking-wider text-brown-200 game-title mr-2 md:mr-4 shrink-0"`
- Line 30 link base classes: `'font-body text-sm px-3 py-1.5 rounded transition-colors whitespace-nowrap shrink-0'` (py bump = bigger tap target)
- Line 41 username span: `className="ml-auto pl-2 font-body text-xs text-clay-300 hidden sm:inline shrink-0"`

- [ ] **Step 2: Verify**

`pnpm typecheck` (background) + device-toolbar check at 375px: all scene links reachable (horizontal scroll if needed), no second row, no clipped title.

- [ ] **Step 3: Commit**

```bash
git add apps/frontend/src/components/TownNav.tsx
git commit -m "feat(mobile): compact horizontally-scrollable TownNav below md"
```

### Task T4: JoinTownModal — cap height, allow scroll

**Files:**
- Modify: `apps/frontend/src/components/JoinTownModal.tsx:86`

- [ ] **Step 1: Implement**

Line 86 modal box className: append `max-h-[85vh] overflow-y-auto`:

```tsx
        className="bg-clay-800 border border-clay-600 rounded-lg p-6 max-w-lg w-full mx-4 shadow-xl max-h-[85vh] overflow-y-auto"
```

- [ ] **Step 2: Verify + commit**

Device-toolbar check at 375×667 (small phone): open Join modal → fully on-screen, internally scrollable, Close button reachable.

```bash
git add apps/frontend/src/components/JoinTownModal.tsx
git commit -m "fix(mobile): JoinTownModal capped to viewport height with internal scroll"
```

### Task T5: Landing hero — add a base size step

**Files:**
- Modify: `apps/frontend/src/pages/Landing.tsx:36`

- [ ] **Step 1: Implement**

Line 36: `text-6xl sm:text-7xl md:text-8xl` → `text-5xl sm:text-7xl md:text-8xl`.

- [ ] **Step 2: Verify + commit**

375px check: "GooseTown" fits one line with breathing room.

```bash
git add apps/frontend/src/pages/Landing.tsx
git commit -m "fix(mobile): landing hero scales down at base breakpoint"
```

### Task T6: ApartmentCard action row — touch-friendly targets

**Files:**
- Modify: `apps/frontend/src/components/ApartmentCard.tsx:200-234`

- [ ] **Step 1: Implement**

Line 200 action-row container: `gap-3` → `gap-1` (the padding below provides the spacing). For each of the three buttons + one Link in the action row (lines 202-234), extend the className: add `px-2 py-2 -my-1 rounded active:bg-clay-700/40` and add an `active:` color twin to each `hover:` color, e.g. the Chat button becomes:

```tsx
                className="font-body text-xs text-clay-300 hover:text-brown-200 active:text-brown-200 active:bg-clay-700/40 transition-colors px-2 py-2 -my-1 rounded"
```

Same pattern for the View link (`hover:text-brown-200` → add `active:text-brown-200`), Remove (`hover:text-red-300` → add `active:text-red-300`), Reactivate (`hover:text-green-300` → add `active:text-green-300`). The `py-2 -my-1` enlarges the hit area ~32→36px without growing the card; `active:` gives tap feedback where hover never fires.

- [ ] **Step 2: Verify + commit**

`pnpm typecheck` (background); tap targets visibly respond in device toolbar.

```bash
git add apps/frontend/src/components/ApartmentCard.tsx
git commit -m "feat(mobile): larger tap targets + active states on agent card actions"
```

### Task T7: Phase 1 gate — full check sweep

- [ ] **Step 1: Run everything**

From `apps/frontend/` (all background): `pnpm vitest run` → all pass; `pnpm lint` → clean; `pnpm typecheck` → clean.

- [ ] **Step 2: Manual device-toolbar sweep at 375×812, document in PR**

Checklist: `/` landing readable → `/town` canvas full-width, Agents pill opens drawer → card actions tappable → Chat opens bottom sheet, send works → Join modal usable → `/apartment` and `/library` render → sign-in page usable (Clerk's own UI, just confirm not clipped).

- [ ] **Step 3: Push and open PR #1 (Phase 1 only)**

```bash
git push -u origin feat/mobile-full-experience
gh pr create --title "feat(mobile): responsive layout — full experience at phone widths" --body "<summary + V1/V2/V3 gate results + device sweep checklist>"
```

Per `feedback_codex_review_before_merge`: wait for codex review before merging.

---

## Phase 2 — Touch input in the Godot viewer (gated on V1)

### Task G1: Pinch-zoom (+ native pan only if V1 failed)

**Files:**
- Modify: `apps/godot/maps/isol88/Isol88Map.gd:26-27` (state vars) and `:88-111` (`_unhandled_input`)

- [ ] **Step 1: Add touch state + constants**

After line 6 (`const DEFAULT_ZOOM := 2.0`):

```gdscript
const MIN_ZOOM := 0.75
const MAX_ZOOM := 4.0
```

After line 26 (`var _dragging: bool = false`):

```gdscript
var _touches: Dictionary = {}  # touch index -> screen position
var _pinch_start_dist: float = 0.0
var _pinch_start_zoom: float = DEFAULT_ZOOM
```

- [ ] **Step 2: Handle touch events in `_unhandled_input`**

Append to `_unhandled_input` (after the `InputEventPanGesture` block, line 111). **Branch A (V1 passed — emulated mouse pan works; we add pinch only):**

```gdscript
	# Touch: two-finger pinch zoom. Single-finger pan arrives via Godot's
	# emulate_mouse_from_touch (project default) through the mouse path above.
	if event is InputEventScreenTouch:
		var st: InputEventScreenTouch = event as InputEventScreenTouch
		if st.pressed:
			_touches[st.index] = st.position
		else:
			_touches.erase(st.index)
		if _touches.size() == 2:
			var pts: Array = _touches.values()
			_pinch_start_dist = (pts[0] as Vector2).distance_to(pts[1] as Vector2)
			_pinch_start_zoom = camera.zoom.x
			_dragging = false  # second finger down: stop emulated-mouse panning

	if event is InputEventScreenDrag:
		var sd: InputEventScreenDrag = event as InputEventScreenDrag
		_touches[sd.index] = sd.position
		if _touches.size() == 2 and _pinch_start_dist > 0.0:
			var pts2: Array = _touches.values()
			var dist: float = (pts2[0] as Vector2).distance_to(pts2[1] as Vector2)
			var z: float = clampf(_pinch_start_zoom * dist / _pinch_start_dist, MIN_ZOOM, MAX_ZOOM)
			camera.zoom = Vector2(z, z)
			_clamp_camera()
			get_viewport().set_input_as_handled()
```

**Branch B (V1 failed — emulated pan broken):** same as Branch A, plus an `elif` on the drag handler for single-finger native pan:

```gdscript
		elif _touches.size() == 1:
			camera.position -= sd.relative / camera.zoom
			_clamp_camera()
			get_viewport().set_input_as_handled()
```

(Branch B only: also guard the mouse-motion pan at line 95 with `and _touches.is_empty()` so an emulated+native double-pan can never occur.)

- [ ] **Step 3: Also clamp zoom in `_clamp_camera` edge case**

`_clamp_camera()` (line 114) divides by `camera.zoom`; at MIN_ZOOM 0.75 the visible area can exceed map bounds, making `clampf`'s min > max (clampf handles this by returning min — camera pins to a corner, acceptable). No change required; this step is a documented check, verify visually in Step 4.

- [ ] **Step 4: Desktop regression check**

Open the project headlessly to confirm no script errors: `/Applications/Godot.app/Contents/MacOS/Godot --headless --path apps/godot --quit` → exit 0, no SCRIPT ERROR lines. Then run the editor or local serve and confirm mouse drag-pan and trackpad pan still work.

- [ ] **Step 5: Commit (code only — export comes with G2 in the same PR, never push separately)**

```bash
git add apps/godot/maps/isol88/Isol88Map.gd
git commit -m "feat(mobile): pinch-zoom (and touch pan if needed) on town camera"
```

### Task G2: Re-export WebGL build + ship together

**Files:**
- Regenerates: `apps/frontend/public/godot/goosetown.{html,js,wasm,pck}`

- [ ] **Step 1: Headless export**

```bash
/Applications/Godot.app/Contents/MacOS/Godot --headless --path apps/godot --export-release "Web" "$(pwd)/apps/frontend/public/godot/goosetown.html"
```

Expected: exit 0; `goosetown.pck` mtime updated.

- [ ] **Step 2: Local smoke**

`pnpm dev` (background) → `/town` in device toolbar with touch emulation: pinch zooms (DevTools: hold Shift+drag emulates pinch), drag pans, desktop mouse still pans at full size.

- [ ] **Step 3: Commit export with the matching .gd already in branch**

```bash
git add apps/frontend/public/godot/
git commit -m "build(godot): re-export Web build with touch camera input"
```

- [ ] **Step 4: Push, PR #2, rerun the V1 probe against the Vercel preview**

After deploy, run the V1 probe spec with `DEV_URL` pointed at the preview/dev URL; pinch can only be confirmed on a real phone — ask the user for a 1-minute check. Wait for codex review before merge.

---

## Phase 3 — Loading experience + caching

### Task L1: Real download progress bar (uses V2's result)

**Files:**
- Modify: `apps/frontend/src/components/GodotCanvas.tsx` (config interface ~line 98, engine creation ~line 181, loading overlay ~line 270)

- [ ] **Step 1: Type + state + wiring**

In the `GodotEngineConstructor` config interface add (kept from V2):

```ts
    onProgress?: (current: number, total: number) => void;
```

In `GodotCanvas` component add state:

```ts
  const [progress, setProgress] = useState<number | null>(null);
```

In the `new window.Engine({...})` call add:

```ts
          onProgress: (current: number, total: number) => {
            // V2 gate result: if total === 0 on this server config, fall back
            // to the known compressed payload (~16.2 MB) as denominator.
            const denom = total > 0 ? total : 16_200_000;
            setProgress(Math.min(100, Math.round((current / denom) * 100)));
          },
```

(If V2 showed `total > 0` reliably, drop the fallback and use `total` directly, keeping `null` → indeterminate for the pre-first-callback state.)

- [ ] **Step 2: Replace the loading overlay (lines 270-274)**

```tsx
      {loading && !error && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-black/50 px-8">
          <p className="text-brown-200 font-body text-lg">Loading GooseTown...</p>
          <div className="w-full max-w-xs h-2 bg-clay-800 rounded-full overflow-hidden">
            <div
              className={
                progress === null
                  ? 'h-full w-1/3 bg-brown-500 rounded-full animate-pulse'
                  : 'h-full bg-brown-500 rounded-full transition-all duration-200'
              }
              style={progress === null ? undefined : { width: `${progress}%` }}
            />
          </div>
          {progress !== null && (
            <p className="text-clay-300 font-body text-xs">{progress}%</p>
          )}
        </div>
      )}
```

- [ ] **Step 3: Verify with throttling**

`pnpm dev` (background) → DevTools Network tab → throttle "Fast 4G" → hard-reload `/town`: bar advances smoothly to 100 then the town appears. No-throttle reload: bar flashes briefly, no layout glitch.

- [ ] **Step 4: Commit**

```bash
git add apps/frontend/src/components/GodotCanvas.tsx
git commit -m "feat(mobile): real download progress bar for engine load"
```

### Task L2: Immutable caching via versioned godot dir (uses V3's result)

**Files:**
- Create: `apps/frontend/scripts/build-with-godot-version.mjs`
- Modify: `apps/frontend/package.json` (build script), `apps/frontend/src/components/GodotCanvas.tsx:14` + engine `executable`, `apps/frontend/vercel.json` (headers)

- [ ] **Step 1: Write the build orchestrator**

```js
// apps/frontend/scripts/build-with-godot-version.mjs
//
// Wraps the production build so the Godot payload lands at /godot/<sha>/,
// letting vercel.json serve it with Cache-Control: immutable. Dev server is
// untouched (VITE_GODOT_BASE unset -> /godot).
import { execSync } from 'node:child_process';
import { renameSync, mkdirSync } from 'node:fs';

const sha = (
  process.env.VERCEL_GIT_COMMIT_SHA ??
  execSync('git rev-parse HEAD').toString().trim()
).slice(0, 12);

// IMPORTANT: keep this in sync with the previous "build" script's compile
// steps (read package.json before editing; preserve tsc/vite invocation).
execSync('tsc -b && vite build', {
  stdio: 'inherit',
  env: { ...process.env, VITE_GODOT_BASE: `/godot/${sha}` },
});

renameSync('dist/godot', 'dist/_godot_tmp');
mkdirSync('dist/godot');
renameSync('dist/_godot_tmp', `dist/godot/${sha}`);
console.log(`[build] godot assets versioned at /godot/${sha}`);
```

(First read `package.json:scripts.build` and mirror its exact compile steps in the `execSync` line — if it's `vite build` without `tsc -b`, match that.)

- [ ] **Step 2: Wire package.json + GodotCanvas**

`package.json`: `"build": "node scripts/build-with-godot-version.mjs"`.

`GodotCanvas.tsx:14`:

```ts
const GODOT_BASE = import.meta.env.VITE_GODOT_BASE ?? '/godot';
const GODOT_JS_PATH = `${GODOT_BASE}/goosetown.js`;
```

and in the Engine config: `executable: \`${GODOT_BASE}/goosetown\`,`.

Also add `VITE_GODOT_BASE?: string` to the `ImportMetaEnv` interface in `apps/frontend/src/vite-env.d.ts` (the project keeps that interface exhaustive — see Known Issues "frontend any casts" resolution).

- [ ] **Step 3: vercel.json immutable header**

Add to the `headers` array (alongside the existing `/godot/(.*)` content-type entries, which still match the deeper path):

```json
    {
      "source": "/godot/(.*)",
      "headers": [
        { "key": "Cache-Control", "value": "public, max-age=31536000, immutable" }
      ]
    }
```

- [ ] **Step 4: Verify locally (repeat of V3, now automated)**

From `apps/frontend/` (background): `pnpm build` then `pnpm exec vite preview` → `/town` boots, all four godot assets + worklets served from `/godot/<sha>/` (apply V3's worklet finding if it required special handling).

- [ ] **Step 5: Commit, push, verify on preview**

```bash
git add apps/frontend/scripts/build-with-godot-version.mjs apps/frontend/package.json apps/frontend/src/components/GodotCanvas.tsx apps/frontend/src/vite-env.d.ts apps/frontend/vercel.json
git commit -m "perf(mobile): immutable caching for godot payload via versioned dir"
```

After the Vercel deploy: `curl -sI -H 'Accept-Encoding: br' https://dev.goosetown.isol8.co/godot/<sha>/goosetown.wasm | grep -i cache-control` → `public, max-age=31536000, immutable`. Reload the page twice on a phone: second load skips the 16MB (Network tab: served from disk cache).

Note: the dev-domain clobbering gotcha (Critical Rules) may require `vercel alias <latest-preview> dev.goosetown.isol8.co` before verifying on the dev domain.

- [ ] **Step 6: PR #3, wait for codex review**

---

## Phase 4 — Follow-up (separate plan, not in this branch)

- **Slim Godot web template** (`scons` custom build, CI job, `custom_template/release` in export presets) — author `docs/superpowers/plans/2026-06-XX-slim-godot-template.md` iff V4's memo says go. Projected payoff: wire 8.9MB → ~4-5MB for the wasm.
- **pck/art diet** (merge time-of-day tilesets into a shader tint, slim library assets) — Phase 4 art territory; out of scope.

## Execution order & PR boundaries

1. **PR #1** = Phase 0 (V1 probe + V4 memo committed; V2/V3 are throwaway local spikes recorded in the PR body) + Phase 1.
2. **PR #2** = Phase 2 (`.gd` + `.pck` together, never split).
3. **PR #3** = Phase 3.

Each PR: CI green (except the pre-existing `smoke.spec.ts` failure — note it in the PR body), codex review before merge, device-toolbar screenshots in the description.
