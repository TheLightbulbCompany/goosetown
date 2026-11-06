/**
 * Hook to fetch the public world-events feed for the sidebar.
 *
 * Polls ``GET /town/world-events`` every ``pollIntervalMs`` ms (default 10s).
 * The endpoint is public — no auth required.  Personal events are filtered
 * server-side, so the returned list is always safe to display to any viewer.
 */

import { useEffect, useState } from 'react';

const API_URL = import.meta.env.VITE_BACKEND_URL ?? 'http://localhost:8000/api/v1';

export interface WorldEvent {
  eventId: string;
  timestamp: string;
  tag: string;
  summary: string | null;
  location: string | null;
}

interface UseWorldFeedOptions {
  pollIntervalMs?: number;
  limit?: number;
}

export function useWorldFeed({ pollIntervalMs = 10_000, limit = 50 }: UseWorldFeedOptions = {}): {
  events: WorldEvent[];
  loading: boolean;
  error: string | null;
} {
  const [events, setEvents] = useState<WorldEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    const fetchEvents = async () => {
      try {
        const resp = await fetch(`${API_URL}/town/world-events?limit=${limit}`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = (await resp.json()) as WorldEvent[];
        if (cancelled) return;
        setEvents(data);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'fetch failed');
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    fetchEvents();
    const id = setInterval(fetchEvents, pollIntervalMs);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [limit, pollIntervalMs]);

  return { events, loading, error };
}
