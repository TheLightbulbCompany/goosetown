// Time formatting helpers for the conversations panel.
//
// Conversation records carry ISO-8601 `started_at` / `ended_at` strings (or
// null). These render them as compact relative timestamps and durations for
// the sidebar log, so a viewer can see when a chat happened and how long it
// ran. `now` is injectable for deterministic tests.

function parse(iso: string | null): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : t;
}

function humanizeSeconds(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h`;
  return `${Math.floor(h / 24)}d`;
}

/** "just now" / "5m ago" / "2h ago" / "3d ago" for an ISO timestamp. */
export function timeAgo(iso: string | null, now: number = Date.now()): string {
  const t = parse(iso);
  if (t == null) return '';
  const deltaSec = (now - t) / 1000;
  if (deltaSec < 10) return 'just now';
  return `${humanizeSeconds(deltaSec)} ago`;
}

/**
 * Conversation duration label.
 * - Active (no end): "live · 5m" measuring from start to now.
 * - Ended: "lasted 3m" measuring start to end.
 * Returns '' when start is missing/unparseable.
 */
export function conversationDuration(
  startedAt: string | null,
  endedAt: string | null,
  now: number = Date.now(),
): string {
  const start = parse(startedAt);
  if (start == null) return '';
  const end = parse(endedAt);
  if (end == null) {
    return `live · ${humanizeSeconds((now - start) / 1000)}`;
  }
  return `lasted ${humanizeSeconds((end - start) / 1000)}`;
}

/** Public feeds only show items from the last 48 h (mirrors the backend). */
export const FEED_MAX_AGE_MS = 48 * 60 * 60 * 1000;

/** True if `iso` is a valid timestamp within FEED_MAX_AGE_MS and not in the future. */
export function isFresh(iso: string | null | undefined, now: number = Date.now()): boolean {
  const t = parse(iso ?? null);
  return t != null && t <= now && now - t <= FEED_MAX_AGE_MS;
}
