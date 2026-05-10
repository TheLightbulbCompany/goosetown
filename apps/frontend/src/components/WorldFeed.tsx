/**
 * WorldFeed — public world-events feed for the sidebar.
 *
 * Displays a chronological (newest-first) list of world events emitted by the
 * WorldGM service: weather, ambient happenings, narrative arcs, social
 * announcements.  Personal events are filtered server-side and never appear
 * here.
 */

import { useWorldFeed } from '../hooks/useWorldFeed.ts';
import type { WorldEvent } from '../hooks/useWorldFeed.ts';

// Map tag prefixes to display icons. The wildcard bullet handles unknown prefixes.
const TAG_ICONS: [string, string][] = [
  ['weather.', '🌧'],
  ['ambient.', '💡'],
  ['arc.', '📜'],
  ['social.', '📣'],
  ['system.', 'ℹ️'],
];

export function iconFor(tag: string): string {
  for (const [prefix, icon] of TAG_ICONS) {
    if (tag.startsWith(prefix)) return icon;
  }
  return '•';
}

export function formatRelative(timestamp: string): string {
  const diffMs = Date.now() - new Date(timestamp).getTime();
  const diffMin = Math.max(1, Math.round(diffMs / 60_000));
  if (diffMin < 60) return `${diffMin}m ago`;
  const hours = Math.round(diffMin / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  return `${days}d ago`;
}

function WorldEventRow({ event }: { event: WorldEvent }) {
  return (
    <div className="flex items-start gap-2">
      <span className="flex-shrink-0 text-sm" aria-hidden="true">
        {iconFor(event.tag)}
      </span>
      <span className="flex-grow text-clay-200 font-body italic text-xs">
        {event.summary ?? event.tag}
        {event.location && (
          <span className="text-clay-400 not-italic"> · {event.location}</span>
        )}
      </span>
      <span className="flex-shrink-0 text-clay-500 text-[10px] font-body">
        {formatRelative(event.timestamp)}
      </span>
    </div>
  );
}

export function WorldFeed() {
  const { events, loading, error } = useWorldFeed();

  if (loading && events.length === 0) {
    return (
      <div className="mt-6">
        <h3 className="font-display text-sm text-clay-300 tracking-wider mb-2">World</h3>
        <p className="text-clay-500 text-xs font-body italic">Loading world events…</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="mt-6">
        <h3 className="font-display text-sm text-clay-300 tracking-wider mb-2">World</h3>
        <p className="text-red-400 text-xs font-body">Feed error: {error}</p>
      </div>
    );
  }

  return (
    <div className="mt-6">
      <h3 className="font-display text-sm text-clay-300 tracking-wider mb-2">World</h3>
      {events.length === 0 ? (
        <p className="text-clay-500 text-xs font-body italic">The town is quiet.</p>
      ) : (
        <div className="flex flex-col gap-1 max-h-[30vh] overflow-y-auto pr-1">
          {events.map((e) => (
            <WorldEventRow key={e.eventId} event={e} />
          ))}
        </div>
      )}
    </div>
  );
}

export default WorldFeed;
