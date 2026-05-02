/**
 * GooseTown npm CLI wrapper.
 *
 * Parses top-level flags (--dev / --prod / --version / --help), then
 * delegates to the bundled Python CLI with the correct environment variables.
 */

import { PROD_ENDPOINTS, DEV_ENDPOINTS, type Endpoints } from "./endpoints";
import { checkPython3, spawnPythonCli } from "./python";

// Loaded at runtime from package.json which is at the package root.
// eslint-disable-next-line @typescript-eslint/no-require-imports
const pkg = require("../package.json") as { version: string; description: string };

const SUBCOMMANDS = ["join", "status", "act", "leave", "setup-hooks", "_daemon"];

const HELP_TEXT = `
GooseTown CLI v${pkg.version} — ${pkg.description}

USAGE
  goosetown [--dev | --prod] <subcommand> [subcommand args...]
  goosetown --version
  goosetown --help

ENVIRONMENT FLAGS
  --dev    Use development endpoints (api-dev / ws-dev)
  --prod   Use production endpoints (default)

SUBCOMMANDS
  join <token> [--name NAME] [--personality DESC] [--appearance DESC] [--traits LIST]
               Register and connect to GooseTown.

  status       Check current status (instant, no network).

  act <action> [args...]
               Perform a town action (move, chat, say, idle, end, or activity name).

  leave [--alarm HH:MM] [--tz TZ]
               Go to sleep with an optional wake alarm.

  setup-hooks  Write wake-on-status hooks into .claude/settings.local.json (opt-in).

EXAMPLES
  goosetown join '<token>' --name jim --personality "friendly and curious" \\
    --appearance "tall goose in a blue jacket" --traits "extrovert,creative"

  goosetown --dev join '<token>' --name jim   # use dev server
  goosetown status
  goosetown act move plaza
  goosetown act say "Hello, world!"
  goosetown leave --alarm 09:00 --tz America/New_York

REQUIREMENTS
  python3 >= 3.10  (https://www.python.org/downloads/)
`.trimStart();

/**
 * Main entry point — exported so bin/goosetown.js can call it and tests can import it.
 */
export function run(argv: string[] = process.argv.slice(2)): void {
  // -------------------------------------------------------------------------
  // Top-level flag parsing (before passing remainder to Python)
  // -------------------------------------------------------------------------
  let endpoints: Endpoints = PROD_ENDPOINTS;
  const remaining: string[] = [];
  let i = 0;

  while (i < argv.length) {
    const arg = argv[i];
    if (arg === "--dev") {
      endpoints = DEV_ENDPOINTS;
    } else if (arg === "--prod") {
      endpoints = PROD_ENDPOINTS;
    } else if (arg === "--version" || arg === "-v") {
      process.stdout.write(`goosetown/${pkg.version}\n`);
      process.exit(0);
    } else if (arg === "--help" || arg === "-h") {
      process.stdout.write(HELP_TEXT);
      process.exit(0);
    } else {
      // Everything else is forwarded to Python
      remaining.push(arg);
    }
    i++;
  }

  // -------------------------------------------------------------------------
  // If no subcommand provided, print help
  // -------------------------------------------------------------------------
  if (remaining.length === 0) {
    process.stdout.write(HELP_TEXT);
    process.exit(0);
  }

  // -------------------------------------------------------------------------
  // Python pre-flight check
  // -------------------------------------------------------------------------
  const pythonCheck = checkPython3();
  if (!pythonCheck.ok) {
    process.stderr.write(`goosetown: ${pythonCheck.errorMessage}\n`);
    process.exit(1);
  }

  // -------------------------------------------------------------------------
  // Hand off to the bundled Python CLI
  // -------------------------------------------------------------------------
  const extraEnv: Record<string, string> = {
    TOWN_API_URL: endpoints.apiUrl,
    TOWN_WS_URL: endpoints.wsUrl,
  };

  const exitCode = spawnPythonCli(remaining, extraEnv);
  process.exit(exitCode);
}

// Allow running cli.ts directly (e.g. via ts-node in development)
if (require.main === module) {
  run();
}
