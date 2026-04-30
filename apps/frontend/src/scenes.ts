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
  { id: 'library',   displayName: 'Library',   route: '/library',   requireAuth: false, showInNav: 'always'    },
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
