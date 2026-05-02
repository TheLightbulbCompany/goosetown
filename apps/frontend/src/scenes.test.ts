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

  it('apartment requires auth; town and library do not', () => {
    expect(sceneById('town')?.requireAuth).toBe(false);
    expect(sceneById('apartment')?.requireAuth).toBe(true);
    expect(sceneById('library')?.requireAuth).toBe(false);
  });
});

describe('sceneForPath', () => {
  it('returns "town" for "/" (unknown path falls back to town)', () => {
    // "/" is the Landing page — not a registered scene. sceneForPath falls
    // back to "town" for unrecognised paths, which keeps GodotCanvas in the
    // town scene while App.tsx handles the Landing render separately.
    expect(sceneForPath('/', false)).toBe('town');
    expect(sceneForPath('/', true)).toBe('town');
  });

  it('returns "town" for "/town"', () => {
    expect(sceneForPath('/town', false)).toBe('town');
    expect(sceneForPath('/town', true)).toBe('town');
  });

  it('returns the scene id when path matches and auth requirements are met', () => {
    expect(sceneForPath('/library', true)).toBe('library');
    expect(sceneForPath('/apartment', true)).toBe('apartment');
  });

  it('falls back to "town" when /apartment requested while signed out', () => {
    expect(sceneForPath('/apartment', false)).toBe('town');
  });

  it('returns "library" when /library requested while signed out', () => {
    // Library is public — signed-out users see the same library.
    expect(sceneForPath('/library', false)).toBe('library');
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
