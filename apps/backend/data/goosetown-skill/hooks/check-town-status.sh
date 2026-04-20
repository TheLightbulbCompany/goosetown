#!/usr/bin/env bash
# GooseTown wake-on-status hook.
#
# Fires on Stop or TeammateIdle. Reads TOWN_STATUS.md in the current workspace;
# if the town has something actionable (a pending chat invite, a conversation
# turn, an event since last wake), exits 2 with a feedback message — which
# tells Claude Code to re-wake the agent instead of going idle. Otherwise
# exits 0 and the agent idles normally.
#
# Runtime-agnostic: works for
#   - Agent-team teammates (via TeammateIdle hook)
#   - Solo Claude Code sessions running an agent (via Stop hook)
#
# Self-installed by goosetown/install.sh into the workspace's
# .claude/settings.local.json — each workspace gets its own hook wiring.
#
# Behavior is intentionally cheap: single stat + one file read. Safe to call
# on every idle/stop cycle.

set -e

STATUS_FILE="TOWN_STATUS.md"
EVENTS_FILE="TOWN_EVENTS.md"
LAST_WAKE_MARKER=".goosetown-last-wake"

# If we're not in a goosetown workspace, do nothing.
[ -f "$STATUS_FILE" ] || exit 0
[ -f "GOOSETOWN.md" ] || exit 0

# Current status modtime (seconds since epoch)
status_mtime=$(stat -f %m "$STATUS_FILE" 2>/dev/null || stat -c %Y "$STATUS_FILE" 2>/dev/null || echo 0)

# Last wake marker (seconds since epoch); if missing, treat as 0
if [ -f "$LAST_WAKE_MARKER" ]; then
  last_wake=$(cat "$LAST_WAKE_MARKER" 2>/dev/null || echo 0)
else
  last_wake=0
fi

# Has the status been updated since we last woke?
if [ "$status_mtime" -le "$last_wake" ]; then
  # Nothing new. Let the agent idle.
  exit 0
fi

# Status IS newer. Decide if it's actionable: look for signals the skill's
# daemon writes when it wants the agent to think.
#
# Signals (in priority order):
#   - "Your turn" marker anywhere in TOWN_STATUS.md
#   - pending_messages (non-empty array in the cached JSON state — we grep
#     the status file, which mirrors the daemon's /tmp/goosetown/<name>/state.json)
#   - A nearby agent arriving (new line in TOWN_EVENTS.md with ARRIVED/NEARBY/CHAT)

actionable=false
reason=""

if grep -q -i "your turn\|\*\*pending\|chat invite\|conversation invite" "$STATUS_FILE" 2>/dev/null; then
  actionable=true
  reason="TOWN_STATUS.md flags your turn or a pending message"
fi

if [ -f "$EVENTS_FILE" ]; then
  # Check for events newer than last wake
  events_mtime=$(stat -f %m "$EVENTS_FILE" 2>/dev/null || stat -c %Y "$EVENTS_FILE" 2>/dev/null || echo 0)
  if [ "$events_mtime" -gt "$last_wake" ]; then
    # Look for actionable event types in the last 5 lines
    if tail -n 5 "$EVENTS_FILE" | grep -q -E "NEARBY|CHAT_INVITE|CONV_INVITE|ARRIVED|SAID"; then
      actionable=true
      reason="${reason:+$reason; }new event in TOWN_EVENTS.md (arrival, chat, or message)"
    fi
  fi
fi

# Record this wake attempt BEFORE deciding, so we don't loop on the same event
echo "$status_mtime" > "$LAST_WAKE_MARKER"

if [ "$actionable" = "true" ]; then
  # Emit feedback (to stderr; Claude Code treats it as the wake message) and exit 2
  >&2 echo "[goosetown] Wake: $reason. Read TOWN_STATUS.md + TOWN_EVENTS.md and act if appropriate."
  exit 2
fi

exit 0
