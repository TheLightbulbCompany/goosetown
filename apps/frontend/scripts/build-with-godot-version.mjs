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
import { renameSync, mkdirSync, existsSync, readFileSync, writeFileSync } from 'node:fs';
import { brotliCompressSync, constants } from 'node:zlib';

// `||` (not `??`): a defined-but-empty VERCEL_GIT_COMMIT_SHA must fall
// through to git, never produce a /godot// path.
const sha = (
  process.env.VERCEL_GIT_COMMIT_SHA ||
  execSync('git rev-parse HEAD').toString().trim()
).slice(0, 12);
if (!sha) throw new Error('could not derive a commit sha for the godot asset dir');

execSync('vite build', {
  stdio: 'inherit',
  env: { ...process.env, VITE_GODOT_BASE: `/godot/${sha}` },
});

if (!existsSync('dist/godot')) {
  throw new Error('dist/godot not found — ensure public/godot/ contains the Godot WebGL export before building');
}

// sha-tagged tmp name: an interrupted run can't collide with the next one
// (vite's emptyOutDir clears dist/ on the following build either way).
const tmpDir = `dist/godot_tmp_${sha}`;
renameSync('dist/godot', tmpDir);
mkdirSync('dist/godot');
renameSync(tmpDir, `dist/godot/${sha}`);
console.log(`[build] godot assets versioned at /godot/${sha}`);

// Pre-compress the Godot wasm with Brotli and serve it with
// Content-Encoding: br (set in vercel.json). Vercel skips automatic
// compression for files above ~10MB, so the 37MB wasm otherwise ships
// uncompressed — by far the biggest first-load cost. Brotli q11 takes it to
// ~6-7MB (~-82%); the browser decompresses transparently and Godot's loader
// sees the original bytes. The dev server is untouched (it serves the raw
// public/godot/ files with no Content-Encoding).
const wasmPath = `dist/godot/${sha}/goosetown.wasm`;
const rawWasm = readFileSync(wasmPath);
const brWasm = brotliCompressSync(rawWasm, {
  params: {
    [constants.BROTLI_PARAM_QUALITY]: 11,
    [constants.BROTLI_PARAM_SIZE_HINT]: rawWasm.length,
  },
});
writeFileSync(wasmPath, brWasm);
console.log(
  `[build] brotli goosetown.wasm: ${(rawWasm.length / 1e6).toFixed(1)}MB -> ` +
    `${(brWasm.length / 1e6).toFixed(1)}MB (-${((1 - brWasm.length / rawWasm.length) * 100).toFixed(0)}%)`,
);
