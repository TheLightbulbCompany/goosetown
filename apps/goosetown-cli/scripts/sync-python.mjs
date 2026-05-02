#!/usr/bin/env node
/**
 * sync-python.mjs
 *
 * Copies the canonical Python CLI files from the backend skill directory into
 * apps/goosetown-cli/python/ so they are included in the npm package tarball.
 *
 * Source: apps/backend/data/goosetown-skill/skills/goosetown/
 * Dest:   apps/goosetown-cli/python/
 *
 * Run automatically via `prepack` and `prepublishOnly`.
 */

import { copyFileSync, mkdirSync, readdirSync, statSync } from "fs";
import { join, dirname, relative } from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// Resolve paths relative to this script (apps/goosetown-cli/scripts/)
const PACKAGE_ROOT = join(__dirname, "..");
const REPO_ROOT = join(PACKAGE_ROOT, "..", "..");
const SRC_ROOT = join(
  REPO_ROOT,
  "apps",
  "backend",
  "data",
  "goosetown-skill",
  "skills",
  "goosetown"
);
const DEST_ROOT = join(PACKAGE_ROOT, "python");

/** Copy src → dest, creating parent dirs as needed. */
function copyFile(src, dest) {
  mkdirSync(dirname(dest), { recursive: true });
  copyFileSync(src, dest);
  console.log(`  copied ${relative(REPO_ROOT, src)} → ${relative(REPO_ROOT, dest)}`);
}

/**
 * Files / directories to include (relative to SRC_ROOT).
 * We explicitly list what we want rather than globbing, so __pycache__,
 * GOOSETOWN.md, etc. are not accidentally bundled.
 */
const MANIFEST = [
  "goosetown_cli.py",
  "hooks/check-town-status.sh",
  "references/actions.md",
  "references/owner-dms.md",
  "references/troubleshooting.md",
  "references/openclaw.md",
];

console.log("sync-python: copying Python CLI files...");
for (const rel of MANIFEST) {
  const src = join(SRC_ROOT, rel);
  const dest = join(DEST_ROOT, rel);
  try {
    statSync(src);
    copyFile(src, dest);
  } catch (err) {
    console.error(`sync-python: WARNING — source file not found: ${src}`);
    console.error(`  (${err.message})`);
    // Don't fail the build for missing reference docs; fail for the main CLI.
    if (rel === "goosetown_cli.py") {
      process.exit(1);
    }
  }
}

console.log("sync-python: done.");
