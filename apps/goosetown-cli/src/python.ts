import { execFileSync, spawnSync } from "child_process";
import type { SpawnSyncOptions } from "child_process";
import path from "path";

export interface PythonCheckResult {
  ok: boolean;
  version?: string;
  errorMessage?: string;
}

/**
 * Check whether python3 is available and meets the minimum version requirement (3.10+).
 * Returns a result object so callers can decide how to surface the error.
 */
export function checkPython3(): PythonCheckResult {
  let raw: string;
  try {
    raw = execFileSync("python3", ["--version"], {
      encoding: "utf8",
      stdio: ["pipe", "pipe", "pipe"],
    }).trim();
  } catch {
    // python3 not found at all
    return {
      ok: false,
      errorMessage:
        "python3 not found in PATH.\n" +
        "GooseTown requires Python 3.10 or later.\n" +
        "Download it from: https://www.python.org/downloads/",
    };
  }

  // raw is something like "Python 3.11.4"
  const match = raw.match(/Python (\d+)\.(\d+)/);
  if (!match) {
    return {
      ok: false,
      version: raw,
      errorMessage: `Could not parse python3 version from: "${raw}"`,
    };
  }

  const major = parseInt(match[1], 10);
  const minor = parseInt(match[2], 10);

  if (major < 3 || (major === 3 && minor < 10)) {
    return {
      ok: false,
      version: raw,
      errorMessage:
        `python3 version ${raw} is too old. GooseTown requires Python 3.10 or later.\n` +
        "Download the latest Python from: https://www.python.org/downloads/",
    };
  }

  return { ok: true, version: raw };
}

/**
 * Locate the bundled goosetown_cli.py inside this npm package's python/ directory.
 * At runtime __dirname is dist/, so python/ is one level up.
 */
export function bundledCliPath(): string {
  return path.join(__dirname, "..", "python", "goosetown_cli.py");
}

/**
 * Spawn python3 with the bundled CLI and the given arguments.
 * TOWN_API_URL and TOWN_WS_URL are injected into the child environment.
 * stdio is inherited so the user sees all output in real time.
 * Returns the child process exit code (0 on success).
 */
export function spawnPythonCli(
  args: string[],
  extraEnv: Record<string, string>,
  options?: Partial<SpawnSyncOptions>
): number {
  const cliPath = bundledCliPath();

  const result = spawnSync("python3", [cliPath, ...args], {
    stdio: "inherit",
    env: { ...process.env, ...extraEnv },
    ...options,
  });

  if (result.error) {
    process.stderr.write(`goosetown: failed to launch python3: ${result.error.message}\n`);
    return 1;
  }

  return result.status ?? 1;
}
