// Wraps the production build so the Godot payload lands at /godot/<sha>/,
// letting vercel.json serve it with Cache-Control: immutable. Godot's web
// exporter writes fixed filenames (goosetown.js/.wasm/.pck) that can't be
// content-addressed per-file, so the versioned directory is the only viable
// cache-buster. Dev server is untouched (VITE_GODOT_BASE unset -> /godot).
//
// Note: local rebuilds without a new commit reuse HEAD's sha. If you
// re-export public/godot/ without committing, commit first so the sha
// (and therefore the cache key) advances. Vercel builds always get a
// fresh sha per deployment.
import { execSync } from 'node:child_process';
import { renameSync, mkdirSync } from 'node:fs';

const sha = (
  process.env.VERCEL_GIT_COMMIT_SHA ??
  execSync('git rev-parse HEAD').toString().trim()
).slice(0, 12);

execSync('vite build', {
  stdio: 'inherit',
  env: { ...process.env, VITE_GODOT_BASE: `/godot/${sha}` },
});

renameSync('dist/godot', 'dist/_godot_tmp');
mkdirSync('dist/godot');
renameSync('dist/_godot_tmp', `dist/godot/${sha}`);
console.log(`[build] godot assets versioned at /godot/${sha}`);
