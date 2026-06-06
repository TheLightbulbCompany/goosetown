#!/usr/bin/env bash
# launch-soak.sh — spawn 15 GooseTown agents for the weekend soak.
#
# Usage:
#   SOAK_TOKENS_FILE=/path/to/tokens.txt ./scripts/launch-soak.sh
#   SOAK_TOKENS_DIR=/path/to/tokens-dir    ./scripts/launch-soak.sh
#
# Token ordering when using SOAK_TOKENS_FILE (one per line):
#   social-1 / social-2 / social-3 / social-4 / social-5
#   wanderer-1 / wanderer-2 / wanderer-3 / wanderer-4 / wanderer-5
#   routine-1 / routine-2 / routine-3 / routine-4 / routine-5
#
# Prereqs: the `goosetown` CLI installed and on PATH
# (`uv tool install goosetown` or `pip install goosetown`), 15 tokens minted
# via the GooseTown frontend. Override the command with GOOSETOWN_CMD if the
# CLI is installed under a different name/path.
#
# Output: soak-report.jsonl written by the background reporter.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GOOSETOWN_CMD="${GOOSETOWN_CMD:-goosetown}"
PERSONALITIES_DIR="${SCRIPT_DIR}/soak-personalities"
SOAK_DIR="${HOME}/goosetown-soak"
REPORT_OUTPUT="${SOAK_DIR}/soak-report.jsonl"

# ---------------------------------------------------------------------------
# Helper: emit a timestamped log line
# ---------------------------------------------------------------------------
log() {
  echo "[$(date '+%H:%M:%S')] $*"
}

# ---------------------------------------------------------------------------
# Collect tokens
# ---------------------------------------------------------------------------
declare -A TOKENS  # agent-name -> token

if [[ -n "${SOAK_TOKENS_DIR:-}" ]]; then
  log "Reading tokens from directory: ${SOAK_TOKENS_DIR}"
  for f in "${SOAK_TOKENS_DIR}"/*.txt; do
    agent="$(basename "${f}" .txt)"
    token="$(cat "${f}")"
    TOKENS["${agent}"]="${token}"
  done
elif [[ -n "${SOAK_TOKENS_FILE:-}" ]]; then
  log "Reading tokens from file: ${SOAK_TOKENS_FILE}"
  # File must have tokens in the canonical order below.
  AGENT_ORDER=(
    social-1 social-2 social-3 social-4 social-5
    wanderer-1 wanderer-2 wanderer-3 wanderer-4 wanderer-5
    routine-1 routine-2 routine-3 routine-4 routine-5
  )
  idx=0
  while IFS= read -r line || [[ -n "${line}" ]]; do
    [[ -z "${line}" ]] && continue
    TOKENS["${AGENT_ORDER[${idx}]}"]="${line}"
    idx=$(( idx + 1 ))
  done < "${SOAK_TOKENS_FILE}"
else
  echo "ERROR: set SOAK_TOKENS_FILE or SOAK_TOKENS_DIR before running this script." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Discover personality YAMLs
# ---------------------------------------------------------------------------
YAML_FILES=()
for archetype in social wanderer routine; do
  for n in 1 2 3 4 5; do
    yaml="${PERSONALITIES_DIR}/${archetype}/${n}.yaml"
    if [[ -f "${yaml}" ]]; then
      YAML_FILES+=("${yaml}")
    else
      echo "ERROR: missing personality file: ${yaml}" >&2
      exit 1
    fi
  done
done

log "Found ${#YAML_FILES[@]} personality files."

# ---------------------------------------------------------------------------
# Parse a YAML field (naive key: value, no multi-line support needed here)
# ---------------------------------------------------------------------------
yaml_field() {
  local file="$1"
  local field="$2"
  grep "^${field}:" "${file}" | head -1 | sed "s/^${field}:[[:space:]]*//" | tr -d '"'
}

# ---------------------------------------------------------------------------
# Spawn each agent
# ---------------------------------------------------------------------------
log "Creating workspace directories under ${SOAK_DIR}"
mkdir -p "${SOAK_DIR}"

for yaml in "${YAML_FILES[@]}"; do
  agent_name="$(yaml_field "${yaml}" "name")"
  display_name="$(yaml_field "${yaml}" "display_name")"
  personality="$(yaml_field "${yaml}" "personality")"
  appearance="$(yaml_field "${yaml}" "appearance")"
  traits="$(yaml_field "${yaml}" "traits")"

  token="${TOKENS[${agent_name}]:-}"
  if [[ -z "${token}" ]]; then
    echo "ERROR: no token for agent '${agent_name}'" >&2
    exit 1
  fi

  workspace="${SOAK_DIR}/${agent_name}"
  mkdir -p "${workspace}"

  log "Joining: ${agent_name} (${display_name})"
  (
    cd "${workspace}"
    AGENT_DIR="${workspace}" ${GOOSETOWN_CMD} join "${token}" \
      --name "${agent_name}" \
      --personality "${personality}" \
      --appearance "${appearance}" \
      --traits "${traits}" \
      2>&1 | sed "s/^/[${agent_name}] /"
  ) &

  # Stagger launches to avoid PixelLab burst.
  sleep 1
done

log "Waiting for all join commands to complete..."
wait
log "All agents launched."

# ---------------------------------------------------------------------------
# Install launchd plists so daemons survive lid-close (macOS only)
# ---------------------------------------------------------------------------
if [[ "$(uname)" == "Darwin" ]]; then
  log "Installing launchd plists..."
  for yaml in "${YAML_FILES[@]}"; do
    agent_name="$(yaml_field "${yaml}" "name")"
    workspace="${SOAK_DIR}/${agent_name}"
    AGENT_DIR="${workspace}" ${GOOSETOWN_CMD} install-launchd "${agent_name}" \
      2>&1 | sed "s/^/[launchd] /" || true
  done
fi

# ---------------------------------------------------------------------------
# Start the background reporter
# ---------------------------------------------------------------------------
mkdir -p "$(dirname "${REPORT_OUTPUT}")"
python3 "${SCRIPT_DIR}/soak-report.py" \
  --interval 300 \
  --output "${REPORT_OUTPUT}" &
REPORTER_PID=$!

log "Reporter started (PID ${REPORTER_PID}). Output: ${REPORT_OUTPUT}"
log ""
log "To follow: tail -f ${REPORT_OUTPUT} | python3 -c 'import sys,json; [print(json.dumps(json.loads(l), indent=2)) for l in sys.stdin]'"
log ""
log "To stop all daemons:"
for yaml in "${YAML_FILES[@]}"; do
  agent_name="$(yaml_field "${yaml}" "name")"
  plist="${HOME}/Library/LaunchAgents/com.goosetown.${agent_name}.plist"
  [[ -f "${plist}" ]] && echo "  launchctl unload ${plist}"
done
log "Then kill reporter: kill ${REPORTER_PID}"
