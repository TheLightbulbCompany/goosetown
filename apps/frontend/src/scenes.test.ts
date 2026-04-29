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
