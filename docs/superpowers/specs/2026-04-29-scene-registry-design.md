# Scene Registry — Library viewer + extensibility for future interiors

**Date:** 2026-04-29
**Status:** Design — pending implementation plan
**Owner:** prasiddha

## Problem

PR #241 wired the library interior end-to-end on the backend and game server (cross-context transitions, shared SubViewport, activity registry, world_state broadcast). But there is no user-facing way to view it: no `/library` route, no nav button, and the Godot WebGL client only knows the scene ids `'town'` and `'apartment'`.

The simplest fix is to mirror the apartment frontend wiring file-for-file. That ships fast but adds two more hardcoded scene ids per call site every time a new interior lands. We're about to add several interiors (cafe, marketplace, workshop, tavern), so the work is justified now.

## Goal

Make adding a new interior a one-row change in two registries (frontend `SceneDef[]` and Godot `SCENE_REGISTRY`) plus the `.tscn` asset and any backend rows already required for cross-context transitions and activities. No edits to `App.tsx`, `GodotCanvas.tsx`, `TownNav.tsx`, `ApartmentCard.tsx`, `js_bridge.gd::_on_switch_scene`, or `agent_manager.gd::_on_world_state`.

## Non-goals

- Refactoring `ApartmentManager` and `LibraryManager` into a shared `InteriorManager` base class. They share ~95% of their logic but differ in their ownership shape (per-user `Dictionary` vs shared singleton). Extract after the third interior lands.
- Public/anonymous viewing. Library is auth-gated like apartment because the viewer WebSocket requires a Clerk-signed token. Anonymous-viewer support is a separate, bigger backend change.
- Empty-state overlay ("no one is in the library right now"). Defer until we see whether the empty interior is actually confusing in practice.

## Two layers

There are two layers where "shared vs per-user" lives. The registry only addresses Layer 2.

**Layer 1 — server-side simulation isolation (the managers).** `ApartmentManager` keeps a `Dictionary` of `user_id → { viewport, controller }`: one SubViewport per user, isolated World2Ds. `LibraryManager` keeps one singleton `_viewport` + `_controller`: every library-context agent shares the same World2D. The structural difference is class-level — not a registry flag. Adding a new sharing model = new manager class. Out of scope for this spec.

**Layer 2 — client-side viewer filter (`agent_manager.gd::_on_world_state`).** The world_state WS broadcast carries every agent in every context. The browser's Godot client filters to "agents in the scene I'm currently viewing." Apartment has an extra `userId == my_user_id` filter. This is the only layer the registry needs to parameterize.

## Registry shape

### Frontend — `apps/frontend/src/scenes.ts` (new file)

```ts
export interface SceneDef {
  id: string;                  // matches Godot scene id and backend location_context
  displayName: string;         // shown in nav and ApartmentCard "View in X →" link
  route: string;               // React Router path
  requireAuth: boolean;        // SignedOut → redirect to "/"
  showInNav: 'always' | 'signed_in' | 'never';
}

export const SCENES: SceneDef[] = [
  { id: 'town',      displayName: 'Town',      route: '/',          requireAuth: false, showInNav: 'always'    },
  { id: 'apartment', displayName: 'Apartment', route: '/apartment', requireAuth: true,  showInNav: 'signed_in' },
  { id: 'library',   displayName: 'Library',   route: '/library',   requireAuth: true,  showInNav: 'signed_in' },
];

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

### Godot — `apps/godot/scripts/autoloads/js_bridge.gd`

```gdscript
const SCENE_REGISTRY: Dictionary = {
    "town":      { "path": "res://maps/isol88/Isol88Map.tscn",      "filter_by_owner": false },
    "apartment": { "path": "res://maps/apartment/ApartmentMap.tscn", "filter_by_owner": true  },
    "library":   { "path": "res://maps/library/LibraryMap.tscn",    "filter_by_owner": false },
}
```

A new interior = one row added to each registry (ids must match) + the `.tscn` asset.

## Call-site changes

### `apps/frontend/src/App.tsx`

Replace:
```ts
const scene: 'town' | 'apartment' = location.pathname === '/apartment' && isSignedIn ? 'apartment' : 'town';
// ...
<Routes>
  <Route path="/" element={<Town />} />
  <Route path="/apartment" element={<Apartment />} />
  ...
</Routes>
```

With:
```ts
const scene = useMemo(() => sceneForPath(location.pathname, isSignedIn ?? false), [location.pathname, isSignedIn]);
// ...
<Routes>
  {SCENES.map(s => <Route key={s.id} path={s.route} element={<InteriorRoute scene={s} />} />)}
  <Route path="/sign-in/*" element={<SignInPage />} />
  <Route path="/sign-up/*" element={<SignUpPage />} />
</Routes>
```

`InteriorRoute` is a single new ~5-line component:
```tsx
function InteriorRoute({ scene }: { scene: SceneDef }) {
  if (!scene.requireAuth) return null;
  return <SignedOut><Navigate to="/" replace /></SignedOut>;
}
```

`apps/frontend/src/pages/Town.tsx` and `apps/frontend/src/pages/Apartment.tsx` are deleted.

### `apps/frontend/src/components/TownNav.tsx`

Replace the two hardcoded `<Link>` blocks with:
```tsx
{SCENES
  .filter(s => s.showInNav === 'always' || (s.showInNav === 'signed_in' && isSignedIn))
  .map(s => (
    <Link key={s.id} to={s.route} className={...}>{s.displayName}</Link>
  ))}
```

The signed-in branch wraps in `<SignedIn>` to match the current `<SignedIn>`-gated apartment link.

### `apps/frontend/src/components/GodotCanvas.tsx`

Widen the prop type:
```ts
interface GodotCanvasProps { className?: string; scene?: string }
```

The internal `currentSceneRef` and `switchScene` call already use the string id; no other changes.

### `apps/frontend/src/components/ApartmentCard.tsx`

Replace the hardcoded `View in town →` block:
```tsx
{(() => {
  const def = sceneById(agent.location_context ?? '');
  if (!def || def.showInNav === 'never') return null;
  const href = def.id === 'town'
    ? `/?focus=${encodeURIComponent(agent.display_name)}`
    : def.route;
  return (
    <div className="flex justify-end mt-1">
      <Link to={href} className="...">View in {def.displayName.toLowerCase()} →</Link>
    </div>
  );
})()}
```

The `?focus=` param stays town-only because library/apartment are letterboxed and don't need camera-pan. If a future interior wants focus-on-agent, add it as a `SceneDef` flag.

### `apps/godot/scripts/autoloads/js_bridge.gd::_on_switch_scene`

Replace:
```gdscript
match scene_name:
    "town":      target_scene = TOWN_SCENE
    "apartment": target_scene = APARTMENT_SCENE
    _:           push_error(...)
```

With:
```gdscript
if not SCENE_REGISTRY.has(scene_name):
    push_error("JsBridge._on_switch_scene: unknown scene '%s'" % scene_name)
    return
var path: String = SCENE_REGISTRY[scene_name]["path"]
if not _loaded_scenes.has(path):
    _loaded_scenes[path] = load(path)
var target_scene: PackedScene = _loaded_scenes[path]
```

`_loaded_scenes: Dictionary` replaces the existing `TOWN_SCENE`/`APARTMENT_SCENE` static vars and provides the same lazy-load behavior.

### `apps/godot/scripts/agent_manager.gd::_on_world_state`

Replace the hardcoded apartment branch:
```gdscript
if current_context == "apartment" and my_user_id != "":
    var agent_user: String = str(agent_data.get("userId", ""))
    if agent_user != my_user_id:
        continue
```

With:
```gdscript
var registry: Dictionary = JsBridge.SCENE_REGISTRY
if registry.has(current_context) and registry[current_context].get("filter_by_owner", false) and my_user_id != "":
    var agent_user: String = str(agent_data.get("userId", ""))
    if agent_user != my_user_id:
        continue
```

The first filter (`ctx != current_context`) is unchanged — it's already generic.

## Backend / game server (no structural changes)

Already extensible from PR #241:
- `_TRANSITION_TABLE` in `context_transition_service.py` is keyed by `(source_context, destination)` tuples — table-driven.
- `activity_registry.py` activities are tagged with `location_context` — generic.
- `state_translator.py` emits `context: <location_context>` per agent — generic.
- `game_server.gd::_broadcast_world_state` iterates all manager state collections — adding a new manager is one wiring line.

Each new interior still needs its own manager class (Layer 1), but no spec-level abstractions are introduced here.

## Testing

### New tests

- `apps/frontend/src/scenes.test.ts`:
  - `sceneForPath('/', true)` → `'town'`
  - `sceneForPath('/library', false)` → `'town'` (auth-gated fallback)
  - `sceneForPath('/library', true)` → `'library'`
  - `sceneById('library')?.requireAuth` → `true`
- `apps/godot/tests/test_js_bridge.gd` (or extend existing):
  - `SCENE_REGISTRY.has("library")` and the path resolves.
  - Unknown scene id → `push_error`, no crash.
- `apps/godot/tests/test_agent_manager.gd`:
  - World state with mixed `town`/`apartment`/`library` contexts → filters correctly per `current_scene`.
  - `filter_by_owner: true` drops other-user agents; `filter_by_owner: false` keeps them.

### Registry-consistency check

The two registries are coupled by id but live in different runtimes (TS vs GDScript). To prevent drift, both files carry a short comment block at the registry definition pointing to the other, and PR review checks both are updated. Automating cross-runtime comparison is overkill until we have >5 interiors.

### Manual smoke test

1. Dev deploy.
2. Sign in.
3. Click `Library` in nav → `/library` loads, Godot canvas switches to LibraryMap.
4. From an agent's CLI: `act move library`. Watch the canvas show the agent walking in.
5. From the sidebar: an agent in library context shows `View in library →`. Click → `/library` route.
6. Sign out → `Library` link disappears from nav. Direct-navigate to `/library` → redirects to `/`.
7. Repeat for `/apartment` to confirm no regression.

## Out of scope (follow-ups)

- `InteriorManager` base-class extraction (Layer-1 dedup).
- Empty-state overlay on shared interiors.
- Per-scene camera-focus on viewer entry (currently town-only via `?focus=`).
- Anonymous viewer auth path.
- Cleanup of unused PixelLab-leftover sprites in `apps/godot/maps/library/`.

## Effort

One PR. Estimated changes:
- 1 new file: `apps/frontend/src/scenes.ts` (~40 LOC).
- 1 file edits each: `App.tsx`, `TownNav.tsx`, `GodotCanvas.tsx`, `ApartmentCard.tsx`, `js_bridge.gd`, `agent_manager.gd`.
- 2 file deletes: `pages/Town.tsx`, `pages/Apartment.tsx`.
- New tests: `scenes.test.ts`, agent_manager filter test.
- Re-export Godot WebGL bundle (`goosetown.{html,js,wasm,pck}`).

Net LOC: ~+200 / -80 in source, ~+150 in tests.
