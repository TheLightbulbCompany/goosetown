# Scene Registry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generalize scene/route/filter handling from hardcoded `'town' | 'apartment'` literals into a registry, then plug in `library` as the first new interior so users can navigate to `/library` and see agents inside the shared library SubViewport.

**Architecture:** Two parallel registries — a TS `SCENES: SceneDef[]` in `apps/frontend/src/scenes.ts` (drives routes, scene prop, nav buttons, "View in X" links) and a GDScript `SCENE_REGISTRY: Dictionary` in `js_bridge.gd` (drives lazy scene loading and the per-viewer userId filter). Adding a new interior = one row in each registry + the `.tscn` asset + the backend rows already required for transitions and activities. Coupling is ID-based; both files reference each other via comment.

**Tech Stack:** TypeScript, React 18, Vite, vitest, React Router v6, Clerk auth, Tailwind, Godot 4.6 GDScript, GdUnitTestSuite.

**Spec:** `docs/superpowers/specs/2026-04-29-scene-registry-design.md`

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `apps/frontend/src/scenes.ts` | **Create** | Registry definition + `sceneForPath` / `sceneById` helpers. Single source of truth for scene metadata on the frontend. |
| `apps/frontend/src/scenes.test.ts` | **Create** | vitest unit tests for the registry helpers. |
| `apps/frontend/src/components/InteriorRoute.tsx` | **Create** | Generic auth-gating route element, replaces `pages/Apartment.tsx`. |
| `apps/frontend/src/App.tsx` | **Modify** | Replace ternary scene-prop logic with `sceneForPath()`. Replace hardcoded `<Routes>` with `SCENES.map(...)`. |
| `apps/frontend/src/components/TownNav.tsx` | **Modify** | Iterate `SCENES` filtered by `showInNav`. |
| `apps/frontend/src/components/ApartmentCard.tsx` | **Modify** | Generalize "View in town →" link via `sceneById(agent.location_context)`. |
| `apps/frontend/src/components/GodotCanvas.tsx` | **Modify** | Widen `scene` prop type from `'town' \| 'apartment'` to `string`. |
| `apps/frontend/src/pages/Town.tsx` | **Delete** | Subsumed by `<InteriorRoute>` (the file currently returns `null`). |
| `apps/frontend/src/pages/Apartment.tsx` | **Delete** | Subsumed by `<InteriorRoute>` (the auth-gate logic moves into the new generic component). |
| `apps/godot/scripts/autoloads/js_bridge.gd` | **Modify** | Add `SCENE_REGISTRY` const + `_loaded_scenes` cache. Replace `match scene_name:` block in `_on_switch_scene` with registry lookup. Remove `TOWN_SCENE` / `APARTMENT_SCENE` static vars. |
| `apps/godot/scripts/agent_manager.gd` | **Modify** | Replace hardcoded `if current_context == "apartment"` filter with `JsBridge.SCENE_REGISTRY[current_context].filter_by_owner` lookup. |
| `apps/godot/tests/test_js_bridge_scene_registry.gd` | **Create** | GdUnit tests asserting registry has expected keys, paths resolve, unknown scene id is handled. |
| `apps/frontend/public/godot/goosetown.{html,js,wasm,pck}` | **Modify** | Re-export the WebGL bundle so the GDScript changes ship to the browser. |

Net source LOC: ~+200 / -80. Net test LOC: ~+150.

---

## Task 1: Create `scenes.ts` registry + helpers (TDD)

**Files:**
- Create: `apps/frontend/src/scenes.ts`
- Test: `apps/frontend/src/scenes.test.ts`

- [ ] **Step 1.1: Write the failing tests**

Create `apps/frontend/src/scenes.test.ts`:

```ts
import { describe, it, expect } from 'vitest';
import { SCENES, sceneForPath, sceneById } from './scenes';

describe('SCENES registry', () => {
  it('contains town, apartment, and library', () => {
    const ids = SCENES.map(s => s.id).sort();
    expect(ids).toEqual(['apartment', 'library', 'town']);
  });

  it('uses unique routes', () => {
    const routes = SCENES.map(s => s.route);
    expect(new Set(routes).size).toBe(routes.length);
  });

  it('library and apartment require auth, town does not', () => {
    expect(sceneById('town')?.requireAuth).toBe(false);
    expect(sceneById('apartment')?.requireAuth).toBe(true);
    expect(sceneById('library')?.requireAuth).toBe(true);
  });
});

describe('sceneForPath', () => {
  it('returns "town" for "/"', () => {
    expect(sceneForPath('/', false)).toBe('town');
    expect(sceneForPath('/', true)).toBe('town');
  });

  it('returns the scene id when path matches and auth requirements are met', () => {
    expect(sceneForPath('/library', true)).toBe('library');
    expect(sceneForPath('/apartment', true)).toBe('apartment');
  });

  it('falls back to "town" when path requires auth but user is signed out', () => {
    expect(sceneForPath('/library', false)).toBe('town');
    expect(sceneForPath('/apartment', false)).toBe('town');
  });

  it('falls back to "town" for unknown paths', () => {
    expect(sceneForPath('/does-not-exist', true)).toBe('town');
    expect(sceneForPath('/sign-in', true)).toBe('town');
  });
});

describe('sceneById', () => {
  it('returns the matching SceneDef', () => {
    const def = sceneById('library');
    expect(def?.id).toBe('library');
    expect(def?.displayName).toBe('Library');
    expect(def?.route).toBe('/library');
  });

  it('returns undefined for unknown id', () => {
    expect(sceneById('does-not-exist')).toBeUndefined();
    expect(sceneById('')).toBeUndefined();
  });
});
```

- [ ] **Step 1.2: Run test to verify it fails**

```bash
cd apps/frontend && pnpm test -- scenes.test.ts
```

Expected: FAIL with "Cannot find module './scenes'".

- [ ] **Step 1.3: Implement `scenes.ts`**

Create `apps/frontend/src/scenes.ts`:

```ts
/**
 * Scene registry — single source of truth for which interiors exist on
 * the frontend, their routes, auth requirements, and nav visibility.
 *
 * Coupled by id with the Godot-side ``SCENE_REGISTRY`` in
 * ``apps/godot/scripts/autoloads/js_bridge.gd``. When adding a new
 * interior, add a matching row in BOTH files (ids must match).
 */

export interface SceneDef {
  /** Stable id; must match the Godot scene id and backend ``location_context``. */
  id: string;
  /** Human-readable name shown in nav and "View in X →" sidebar links. */
  displayName: string;
  /** React Router path that activates this scene. */
  route: string;
  /** When true, signed-out users hitting ``route`` are redirected to ``/``. */
  requireAuth: boolean;
  /** Controls whether the nav bar renders a link to this scene. */
  showInNav: 'always' | 'signed_in' | 'never';
}

export const SCENES: SceneDef[] = [
  { id: 'town',      displayName: 'Town',      route: '/',          requireAuth: false, showInNav: 'always'    },
  { id: 'apartment', displayName: 'Apartment', route: '/apartment', requireAuth: true,  showInNav: 'signed_in' },
  { id: 'library',   displayName: 'Library',   route: '/library',   requireAuth: true,  showInNav: 'signed_in' },
];

/**
 * Resolve which scene id should be active for a given pathname.
 * Falls back to ``town`` for unknown paths or auth-gated paths the
 * user can't access. The signed-in scene determines what
 * ``GodotCanvas`` switches to via the JS bridge.
 */
export function sceneForPath(pathname: string, isSignedIn: boolean): string {
  const def = SCENES.find(s => s.route === pathname);
  if (!def) return 'town';
  if (def.requireAuth && !isSignedIn) return 'town';
  return def.id;
}

export function sceneById(id: string): SceneDef | undefined {
  return SCENES.find(s => s.id === id);
}
```

- [ ] **Step 1.4: Run tests to verify pass**

```bash
cd apps/frontend && pnpm test -- scenes.test.ts
```

Expected: 8 PASS.

- [ ] **Step 1.5: Run typecheck**

```bash
cd apps/frontend && pnpm typecheck
```

Expected: no errors.

- [ ] **Step 1.6: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync
git add apps/frontend/src/scenes.ts apps/frontend/src/scenes.test.ts
git commit -m "feat(frontend): add scene registry + sceneForPath helper

Single source of truth for scene metadata (id, route, auth, nav).
Coupled by id with Godot SCENE_REGISTRY (added in next commit).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Add generic `InteriorRoute` and refactor `App.tsx`

**Files:**
- Create: `apps/frontend/src/components/InteriorRoute.tsx`
- Modify: `apps/frontend/src/App.tsx`
- Delete: `apps/frontend/src/pages/Town.tsx`, `apps/frontend/src/pages/Apartment.tsx`

- [ ] **Step 2.1: Create `InteriorRoute.tsx`**

```tsx
import { SignedOut } from '@clerk/clerk-react';
import { Navigate } from 'react-router-dom';
import type { SceneDef } from '../scenes';

/**
 * Generic route element for any scene in the registry. When the scene
 * requires auth, signed-out users are redirected to "/". When it doesn't,
 * renders nothing (the persistent <GodotCanvas/> in App handles rendering).
 */
export default function InteriorRoute({ scene }: { scene: SceneDef }) {
  if (!scene.requireAuth) return null;
  return (
    <SignedOut>
      <Navigate to="/" replace />
    </SignedOut>
  );
}
```

- [ ] **Step 2.2: Edit `App.tsx`**

Open `apps/frontend/src/App.tsx`. Make these three edits (line numbers below are the current state — adjust if upstream lines have shifted by the time you do this):

**Edit A — replace imports (top of file).** Remove `Town` and `Apartment` page imports, add `SCENES`/`sceneForPath` and `InteriorRoute`. Replace:

```ts
import Town from './pages/Town.tsx';
import Apartment from './pages/Apartment.tsx';
```

with:

```ts
import { SCENES, sceneForPath } from './scenes.ts';
import InteriorRoute from './components/InteriorRoute.tsx';
```

Also import `useMemo` if not already imported:

```ts
import { useEffect, useMemo, useState } from 'react';
```

**Edit B — replace the scene ternary at App.tsx:139-140.** Replace:

```ts
const scene: 'town' | 'apartment' =
  location.pathname === '/apartment' && isSignedIn ? 'apartment' : 'town';
```

with:

```ts
const scene = useMemo(
  () => sceneForPath(location.pathname, !!isSignedIn),
  [location.pathname, isSignedIn],
);
```

**Edit C — replace the `<Routes>` block at App.tsx:151-156.** Replace:

```tsx
<Routes>
  <Route path="/" element={<Town />} />
  <Route path="/apartment" element={<Apartment />} />
  <Route path="/sign-in/*" element={<SignInPage />} />
  <Route path="/sign-up/*" element={<SignUpPage />} />
</Routes>
```

with:

```tsx
<Routes>
  {SCENES.map(s => (
    <Route key={s.id} path={s.route} element={<InteriorRoute scene={s} />} />
  ))}
  <Route path="/sign-in/*" element={<SignInPage />} />
  <Route path="/sign-up/*" element={<SignUpPage />} />
</Routes>
```

- [ ] **Step 2.3: Delete obsolete page files**

```bash
rm apps/frontend/src/pages/Town.tsx apps/frontend/src/pages/Apartment.tsx
```

- [ ] **Step 2.4: Verify typecheck and existing tests still pass**

```bash
cd apps/frontend && pnpm typecheck && pnpm test
```

Expected: no errors. (No existing test imports `pages/Town.tsx` or `pages/Apartment.tsx` — verified by grep before delete.)

- [ ] **Step 2.5: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync
git add apps/frontend/src/components/InteriorRoute.tsx apps/frontend/src/App.tsx
git rm apps/frontend/src/pages/Town.tsx apps/frontend/src/pages/Apartment.tsx
git commit -m "refactor(frontend): drive App routes/scene-prop from registry

Replaces the hardcoded /apartment ternary and per-route page files
with SCENES.map(...) and a generic <InteriorRoute/> that reads
requireAuth from the SceneDef. New interiors plug in as one row.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Refactor `TownNav.tsx` to iterate `SCENES`

**Files:**
- Modify: `apps/frontend/src/components/TownNav.tsx`

- [ ] **Step 3.1: Replace the file contents**

Open `apps/frontend/src/components/TownNav.tsx` and replace lines 22-46 (the two hardcoded `<Link>` blocks) with a single iteration. The full target shape of the body:

```tsx
import { Link, useLocation } from 'react-router-dom';
import { useUser } from '@clerk/clerk-react';
import clsx from 'clsx';
import { SCENES } from '../scenes';

export default function TownNav() {
  const location = useLocation();
  const { user, isSignedIn } = useUser();

  const isActive = (path: string) => {
    if (path === '/') return location.pathname === '/';
    return location.pathname === path;
  };

  const visibleScenes = SCENES.filter(
    s => s.showInNav === 'always' || (s.showInNav === 'signed_in' && isSignedIn),
  );

  return (
    <nav className="flex items-center gap-4 px-4 py-2 bg-clay-900 border-b border-clay-700 z-20 shrink-0">
      <span className="font-display text-xl tracking-wider text-brown-200 game-title mr-4">
        GooseTown
      </span>

      {visibleScenes.map(s => (
        <Link
          key={s.id}
          to={s.route}
          className={clsx(
            'font-body text-sm px-3 py-1 rounded transition-colors',
            isActive(s.route)
              ? 'bg-clay-700 text-brown-100'
              : 'text-clay-300 hover:text-brown-100 hover:bg-clay-700/50',
          )}
        >
          {s.displayName}
        </Link>
      ))}

      {user && (
        <span className="ml-auto font-body text-xs text-clay-300">
          {user.firstName || user.username || 'Player'}
        </span>
      )}
    </nav>
  );
}
```

Note: the original wrapped the apartment link in `<SignedIn>`. We replace that gating with the `isSignedIn` boolean from `useUser()` and the `showInNav: 'signed_in'` filter — same effect, no extra component layer.

- [ ] **Step 3.2: Verify typecheck and tests pass**

```bash
cd apps/frontend && pnpm typecheck && pnpm test
```

Expected: no errors, all tests pass.

- [ ] **Step 3.3: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync
git add apps/frontend/src/components/TownNav.tsx
git commit -m "refactor(frontend): iterate SCENES in TownNav

Auth-gating now driven by SceneDef.showInNav instead of <SignedIn>
wrapper around hardcoded links.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Generalize `ApartmentCard.tsx` "View in X" link

**Files:**
- Modify: `apps/frontend/src/components/ApartmentCard.tsx`
- Modify: `apps/frontend/src/components/ApartmentCard.test.tsx`

- [ ] **Step 4.1: Edit `ApartmentCard.tsx`**

Add an import at the top of the file (after the existing `clsx` import on line 2):

```ts
import { sceneById } from '../scenes';
```

Replace the existing "View in town" block at lines 150-160:

```tsx
{/* Action: view in town */}
{agent.location_context === 'town' && (
  <div className="flex justify-end mt-1">
    <Link
      to={`/?focus=${encodeURIComponent(agent.display_name)}`}
      className="font-body text-xs text-clay-300 hover:text-brown-200 transition-colors"
    >
      View in town &rarr;
    </Link>
  </div>
)}
```

with:

```tsx
{/* Action: view in <scene> — town pans the camera, interior routes just navigate. */}
{(() => {
  const def = sceneById(agent.location_context ?? '');
  if (!def || def.showInNav === 'never') return null;
  const href = def.id === 'town'
    ? `/?focus=${encodeURIComponent(agent.display_name)}`
    : def.route;
  return (
    <div className="flex justify-end mt-1">
      <Link
        to={href}
        className="font-body text-xs text-clay-300 hover:text-brown-200 transition-colors"
      >
        View in {def.displayName.toLowerCase()} &rarr;
      </Link>
    </div>
  );
})()}
```

- [ ] **Step 4.2: Add a test for the library link**

Open `apps/frontend/src/components/ApartmentCard.test.tsx`. Find the existing test that checks for `View in town →` (search the file for `View in town` or `current_location: 'library'` — there's already a stub test at line 88 that needs upgrading). Add this case:

```tsx
it('renders "View in library →" link when location_context is library', () => {
  renderCard(makeAgent({ location_context: 'library', current_location: 'reading_armchair' }));
  const link = screen.getByText(/View in library/i);
  expect(link).toBeInTheDocument();
  expect(link.closest('a')?.getAttribute('href')).toBe('/library');
});

it('renders "View in town →" link with ?focus= when location_context is town', () => {
  renderCard(makeAgent({ location_context: 'town', display_name: 'Lucky' }));
  const link = screen.getByText(/View in town/i);
  expect(link.closest('a')?.getAttribute('href')).toBe('/?focus=Lucky');
});

it('renders no "View in" link when location_context is apartment', () => {
  renderCard(makeAgent({ location_context: 'apartment', current_location: 'bed' }));
  expect(screen.queryByText(/View in/i)).toBeNull();
});
```

The "no link for apartment" case is intentional — apartment has `showInNav: 'signed_in'` not `'never'`, so it would render. Update the test expectation to match: apartment SHOULD show "View in apartment →" → `/apartment`. Use this test instead:

```tsx
it('renders "View in apartment →" link when location_context is apartment', () => {
  renderCard(makeAgent({ location_context: 'apartment', current_location: 'bed' }));
  const link = screen.getByText(/View in apartment/i);
  expect(link.closest('a')?.getAttribute('href')).toBe('/apartment');
});
```

(The previous behavior only rendered the link for town. The new generalized behavior renders one for any registered scene. That's intentional — clicking "View in apartment →" navigates the user to their own apartment view, which is correct UX.)

- [ ] **Step 4.3: Run tests**

```bash
cd apps/frontend && pnpm test -- ApartmentCard
```

Expected: all green, including the three new cases.

- [ ] **Step 4.4: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync
git add apps/frontend/src/components/ApartmentCard.tsx apps/frontend/src/components/ApartmentCard.test.tsx
git commit -m "refactor(frontend): generalize ApartmentCard 'View in X' link

Reads target scene from sceneById(agent.location_context). Town
keeps the ?focus= camera-pan; interior routes just navigate.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Widen `GodotCanvas.tsx` `scene` prop type

**Files:**
- Modify: `apps/frontend/src/components/GodotCanvas.tsx`

- [ ] **Step 5.1: Replace the prop type**

At `GodotCanvas.tsx:107-110`, replace:

```ts
interface GodotCanvasProps {
  className?: string;
  scene?: 'town' | 'apartment';
}
```

with:

```ts
interface GodotCanvasProps {
  className?: string;
  /** Scene id from the SCENES registry. Validated by sceneForPath() upstream. */
  scene?: string;
}
```

- [ ] **Step 5.2: Verify typecheck**

```bash
cd apps/frontend && pnpm typecheck
```

Expected: no errors. The `scene` prop is opaquely passed to `_godotBridge.switchScene(scene)`, which already takes a string.

- [ ] **Step 5.3: Run full frontend test + lint**

```bash
cd apps/frontend && pnpm test && pnpm lint
```

Expected: all green.

- [ ] **Step 5.4: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync
git add apps/frontend/src/components/GodotCanvas.tsx
git commit -m "refactor(frontend): widen GodotCanvas scene prop to string

Accepts any scene id registered in SCENES. Validation happens
upstream in sceneForPath(); GodotCanvas just forwards to JS bridge.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Add `SCENE_REGISTRY` to `js_bridge.gd` + refactor `_on_switch_scene`

**Files:**
- Modify: `apps/godot/scripts/autoloads/js_bridge.gd`
- Create: `apps/godot/tests/test_js_bridge_scene_registry.gd`

- [ ] **Step 6.1: Write the failing GdUnit test**

Create `apps/godot/tests/test_js_bridge_scene_registry.gd`:

```gdscript
## Tests for ``JsBridge.SCENE_REGISTRY`` and ``_on_switch_scene`` lookup.
##
## The registry is the GDScript twin of ``apps/frontend/src/scenes.ts``.
## Tests guard the contract — id presence, path resolution, filter flag —
## that the React side relies on when calling ``godotSwitchScene``.
extends GdUnitTestSuite

const JsBridgeScript := preload("res://scripts/autoloads/js_bridge.gd")


func test_scene_registry_contains_town_apartment_library() -> void:
	var registry: Dictionary = JsBridgeScript.SCENE_REGISTRY
	assert_that(registry.has("town")).is_true()
	assert_that(registry.has("apartment")).is_true()
	assert_that(registry.has("library")).is_true()


func test_scene_registry_paths_resolve_to_existing_resources() -> void:
	for scene_id in JsBridgeScript.SCENE_REGISTRY.keys():
		var path: String = JsBridgeScript.SCENE_REGISTRY[scene_id]["path"]
		assert_that(ResourceLoader.exists(path)).is_true()


func test_filter_by_owner_is_true_only_for_apartment() -> void:
	var registry: Dictionary = JsBridgeScript.SCENE_REGISTRY
	assert_that(registry["town"]["filter_by_owner"]).is_false()
	assert_that(registry["apartment"]["filter_by_owner"]).is_true()
	assert_that(registry["library"]["filter_by_owner"]).is_false()
```

- [ ] **Step 6.2: Run the test (expected to fail since SCENE_REGISTRY does not exist yet)**

```bash
cd apps/godot && /Applications/Godot.app/Contents/MacOS/Godot --headless --path . --script addons/gdUnit4/bin/GdUnitCmdTool.gd -a -c -rd reports -a tests/test_js_bridge_scene_registry.gd
```

Expected: FAIL with "Invalid get index 'SCENE_REGISTRY'" or similar.

(If the headless GdUnit runner isn't wired locally, skip the run — the test will be picked up by the project's normal test pipeline. Verify the file parses by opening Godot or running `godot --check-only --script tests/test_js_bridge_scene_registry.gd`.)

- [ ] **Step 6.3: Edit `js_bridge.gd`**

Open `apps/godot/scripts/autoloads/js_bridge.gd`.

**Edit A — replace the lazy-load static vars at lines 16-17.** Replace:

```gdscript
## Load map scenes on demand to avoid blocking editor startup.
var TOWN_SCENE: PackedScene = null
var APARTMENT_SCENE: PackedScene = null
```

with:

```gdscript
## Scene registry — single source of truth for scene id → asset path.
## Coupled by id with the frontend's ``apps/frontend/src/scenes.ts``.
## Adding a new interior: add a row in BOTH files (ids must match).
const SCENE_REGISTRY: Dictionary = {
	"town":      { "path": "res://maps/isol88/Isol88Map.tscn",      "filter_by_owner": false },
	"apartment": { "path": "res://maps/apartment/ApartmentMap.tscn", "filter_by_owner": true  },
	"library":   { "path": "res://maps/library/LibraryMap.tscn",    "filter_by_owner": false },
}

## Lazy-loaded PackedScene cache, keyed by ``SCENE_REGISTRY[id].path``.
## Populated on first ``switchScene`` call to a given scene to avoid
## blocking editor startup with a load() of every map.
var _loaded_scenes: Dictionary = {}
```

**Edit B — rewrite `_on_switch_scene` body at lines 199-238.** Replace the lazy-load block:

```gdscript
	# Lazy-load scenes on first switch to avoid blocking editor startup
	if TOWN_SCENE == null:
		TOWN_SCENE = load("res://maps/isol88/Isol88Map.tscn")
	if APARTMENT_SCENE == null:
		APARTMENT_SCENE = load("res://maps/apartment/ApartmentMap.tscn")

	var target_scene: PackedScene
	match scene_name:
		"town":
			target_scene = TOWN_SCENE
		"apartment":
			target_scene = APARTMENT_SCENE
		_:
			push_error("JsBridge._on_switch_scene: unknown scene '%s'" % scene_name)
			return
```

with:

```gdscript
	if not SCENE_REGISTRY.has(scene_name):
		push_error("JsBridge._on_switch_scene: unknown scene '%s'" % scene_name)
		return

	var path: String = SCENE_REGISTRY[scene_name]["path"]
	if not _loaded_scenes.has(path):
		_loaded_scenes[path] = load(path)
	var target_scene: PackedScene = _loaded_scenes[path]
```

The rest of the function (current_scene assignment, change_scene_to_packed, await frame, emit) is unchanged.

- [ ] **Step 6.4: Run the GdUnit test (expected to pass now)**

```bash
cd apps/godot && /Applications/Godot.app/Contents/MacOS/Godot --headless --path . --script addons/gdUnit4/bin/GdUnitCmdTool.gd -a tests/test_js_bridge_scene_registry.gd
```

Expected: 3 PASS.

If the local runner isn't wired, run the full Godot project headless once to confirm `SCENE_REGISTRY` parses:

```bash
/Applications/Godot.app/Contents/MacOS/Godot --headless --path apps/godot --quit
```

Expected: clean exit, no parse errors.

- [ ] **Step 6.5: Commit (defer push until WebGL re-export in Task 9)**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync
git add apps/godot/scripts/autoloads/js_bridge.gd apps/godot/tests/test_js_bridge_scene_registry.gd
git commit -m "refactor(godot): replace match-based scene-switch with SCENE_REGISTRY

Adds GDScript twin of frontend SCENES registry. Lazy-loads via
_loaded_scenes cache, keyed by registry path. Unknown scene id
push_errors and returns instead of crashing.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Refactor `agent_manager.gd` to use registry filter flag

**Files:**
- Modify: `apps/godot/scripts/agent_manager.gd`

- [ ] **Step 7.1: Replace the apartment-specific filter at lines 55-60**

Open `apps/godot/scripts/agent_manager.gd`. Replace:

```gdscript
			var ctx: String = str(agent_data.get("context", "town"))
			# In town: show all town-context agents
			# In apartment: show only own agents (matching userId)
			if ctx != current_context:
				continue
			if current_context == "apartment" and my_user_id != "":
				var agent_user: String = str(agent_data.get("userId", ""))
				if agent_user != my_user_id:
					continue
			incoming_agents.append(agent_data)
```

with:

```gdscript
			var ctx: String = str(agent_data.get("context", "town"))
			# Drop agents that aren't in the scene we're currently rendering.
			if ctx != current_context:
				continue
			# Per-scene "filter_by_owner" flag from the registry: when true
			# (apartment), drop agents owned by other users so each viewer
			# only sees their own apartment occupants.
			var registry: Dictionary = JsBridge.SCENE_REGISTRY
			var filter_by_owner: bool = (
				registry.has(current_context)
				and registry[current_context].get("filter_by_owner", false)
			)
			if filter_by_owner and my_user_id != "":
				var agent_user: String = str(agent_data.get("userId", ""))
				if agent_user != my_user_id:
					continue
			incoming_agents.append(agent_data)
```

- [ ] **Step 7.2: Verify the project parses headless**

```bash
/Applications/Godot.app/Contents/MacOS/Godot --headless --path apps/godot --quit
```

Expected: clean exit.

- [ ] **Step 7.3: Run the existing js_bridge tests + new registry test**

```bash
cd apps/godot && /Applications/Godot.app/Contents/MacOS/Godot --headless --path . --script addons/gdUnit4/bin/GdUnitCmdTool.gd -a tests/
```

Expected: all PASS, including the new `test_js_bridge_scene_registry.gd`.

- [ ] **Step 7.4: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync
git add apps/godot/scripts/agent_manager.gd
git commit -m "refactor(godot): drive agent_manager owner-filter from SCENE_REGISTRY

Replaces hardcoded \"apartment\" branch with a registry-driven
filter_by_owner lookup. Library and any future shared interior
inherit no userId filter (everyone sees everyone).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Re-export Godot WebGL bundle

**Files:**
- Modify: `apps/frontend/public/godot/goosetown.{html,js,wasm,pck}`

- [ ] **Step 8.1: Re-export headless**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync
/Applications/Godot.app/Contents/MacOS/Godot \
  --headless \
  --path apps/godot \
  --export-release "Web" \
  apps/frontend/public/godot/goosetown.html
```

Expected: regenerated files at `apps/frontend/public/godot/goosetown.{html,js,wasm,pck}`.

- [ ] **Step 8.2: Verify the bundle isn't empty and the .pck changed**

```bash
git diff --stat apps/frontend/public/godot/
```

Expected: `goosetown.pck` shows binary changes; the `.html`/`.js`/`.wasm` may be unchanged.

- [ ] **Step 8.3: Commit**

```bash
git add apps/frontend/public/godot/goosetown.html apps/frontend/public/godot/goosetown.js apps/frontend/public/godot/goosetown.wasm apps/frontend/public/godot/goosetown.pck
git commit -m "build(godot): re-export WebGL bundle for scene registry

Includes SCENE_REGISTRY const and registry-driven scene/filter
logic from prior commits. The .pck must ship with the .gd changes
or the browser will load stale GDScript.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Open PR and run manual smoke test in dev

**Files:** none

- [ ] **Step 9.1: Push branch and open PR**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync
# Assume working on a feature branch; if on main, branch first:
# git checkout -b feat/scene-registry
git push -u origin HEAD
gh pr create --title "feat(scene-registry): library viewer + extensible interior pattern" --body "$(cat <<'EOF'
## Summary
- New \`scenes.ts\` registry on the frontend + \`SCENE_REGISTRY\` constant in \`js_bridge.gd\`. Single source of truth per side.
- Library is now reachable at \`/library\` (auth-gated, mirrors apartment for camera + nav, but shared like town for visibility).
- Adding a new interior is a one-row change in two files + the \`.tscn\` asset.

Spec: \`docs/superpowers/specs/2026-04-29-scene-registry-design.md\`

## Test plan
- [ ] Frontend: \`pnpm test\` passes (incl. new \`scenes.test.ts\` and updated \`ApartmentCard.test.tsx\`)
- [ ] Godot: GdUnit suite passes (incl. new \`test_js_bridge_scene_registry.gd\`)
- [ ] Dev deploy → sign in → \`Library\` link visible in nav
- [ ] Click Library → canvas switches to LibraryMap
- [ ] Drive an agent: \`act move library\` → agent visible inside library
- [ ] Sidebar agent card shows "View in library →" → navigates to \`/library\`
- [ ] Sign out → \`Library\` link gone; direct nav to \`/library\` redirects to \`/\`
- [ ] No regression: \`/apartment\` still works for own-user filter

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 9.2: Wait for CI green + codex review**

Per the project rule: don't merge before codex review even if CI is green. Watch the PR until both ship.

- [ ] **Step 9.3: Run manual smoke test on dev**

After merge → dev deploys automatically.

1. Open Chrome at `https://dev.goosetown.isol8.co`.
2. Sign in with Clerk.
3. Verify the nav bar shows `Town`, `Apartment`, `Library`.
4. Click `Library`. Verify URL → `/library` and the canvas switches to LibraryMap.
5. From the dwight test-team workspace:

   ```bash
   cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/.claude/test-team/dwight
   AGENT_DIR=$(pwd) python3 goosetown/goosetown_cli.py act move library
   ```

   Verify `world_state` log in browser console shows dwight with `context: "library"`. Verify dwight's sprite is visible inside the library SubViewport.
6. From the sidebar, dwight's `ApartmentCard` should show `View in library →`. Click → URL becomes `/library`.
7. Sign out. The `Library` and `Apartment` nav links disappear. Direct-navigate to `https://dev.goosetown.isol8.co/library` → redirects to `/`.
8. Sign in again. Click `Apartment`. Verify `/apartment` still works (regression check).
9. Drive dwight back: `act move exit` (library → town), confirm visible in town view.

If any step fails, open a follow-up issue. The library viewer ships even with minor UX rough edges; only block on functional regressions.

---

## Self-review notes

**Spec coverage check:**
- Registry definition (frontend + Godot): Tasks 1, 6 ✅
- App.tsx scene-prop + routes: Task 2 ✅
- TownNav: Task 3 ✅
- ApartmentCard: Task 4 ✅
- GodotCanvas prop: Task 5 ✅
- js_bridge.gd refactor: Task 6 ✅
- agent_manager.gd refactor: Task 7 ✅
- Tests (scenes, ApartmentCard, scene registry): Tasks 1, 4, 6 ✅
- Godot WebGL re-export: Task 8 ✅
- Manual smoke: Task 9 ✅

**Out-of-spec scope check:**
- No `InteriorManager` base class (matches non-goal #1).
- No empty-state overlay (matches non-goal #3).
- No anonymous viewer auth (matches non-goal #2).

**Type-consistency check:**
- `SceneDef.id`, `sceneById(id)`, `sceneForPath(path, signedIn)`, `SCENE_REGISTRY[id].filter_by_owner` — all referenced consistently across Tasks 1-7.

No placeholders, no TODOs, no "fill in details" steps.
