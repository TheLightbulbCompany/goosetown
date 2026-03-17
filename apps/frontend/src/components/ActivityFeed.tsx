import type { ActivityEvent } from '../hooks/useApartment';

function eventIcon(eventType: string): string {
  switch (eventType) {
    case 'chatted':
      return '[chat]';
    case 'arrived':
      return '[move]';
    case 'sleeping':
      return '[zzz]';
    case 'walking':
      return '[walk]';
    case 'idle':
      return '[idle]';
    default:
      return '[*]';
  }
}

function timeAgo(timestamp: string): string {
  const now = Date.now();
  const then = new Date(timestamp).getTime();
  const diffMs = now - then;
  const diffMin = Math.floor(diffMs / 60_000);

  if (diffMin < 1) return 'just now';
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDay = Math.floor(diffHr / 24);
  return `${diffDay}d ago`;
}

export default function ActivityFeed({ events }: { events: ActivityEvent[] }) {
  if (events.length === 0) {
    return (
      <div className="box bg-brown-800 p-4">
        <h3 className="font-display text-lg text-brown-200 tracking-wider mb-3">Activity</h3>
        <p className="font-body text-sm text-clay-300">No recent activity.</p>
      </div>
    );
  }

  return (
    <div className="box bg-brown-800 p-4">
      <h3 className="font-display text-lg text-brown-200 tracking-wider mb-3">Activity</h3>
      <div className="flex flex-col gap-2 max-h-96 overflow-y-auto">
        {events.map((event, i) => (
          <div key={i} className="flex items-start gap-2 py-1 border-b border-clay-700/50 last:border-0">
            <span className="font-body text-xs text-clay-500 shrink-0 w-12">
              {eventIcon(event.event_type)}
            </span>
            <div className="flex-1 min-w-0">
              <p className="font-body text-xs text-brown-100 leading-relaxed">
                {event.description}
              </p>
              {event.location && (
                <span className="font-body text-xs text-clay-500">@ {event.location}</span>
              )}
            </div>
            <span className="font-body text-xs text-clay-500 shrink-0">
              {timeAgo(event.timestamp)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
