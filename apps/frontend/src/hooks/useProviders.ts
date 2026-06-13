/**
 * Hook to fetch the public Exchange provider directory for the sidebar.
 *
 * Polls ``GET /town/exchange/providers?category=<sel>`` every ``pollIntervalMs``
 * ms (default 5s). The endpoint is public — no auth required. The payload is
 * the directory's safe view (see ProviderDirectory._public_profile): name, bio,
 * skills, trust tier/score, jobs completed — no wallet balance, no PII.
 *
 * When ``category`` changes the query re-fires immediately (it's an effect
 * dependency), so flipping a category chip refreshes the directory at once
 * rather than waiting out the poll interval.
 */

import { useEffect, useState } from 'react';

const API_URL = import.meta.env.VITE_BACKEND_URL ?? 'http://localhost:8000/api/v1';

export interface Provider {
  agent_id: string;
  name: string;
  bio: string;
  skills: string[];
  /** Display tier: "newcomer" | "reliable" | "trusted". */
  trust_tier: string;
  trust_score: number;
  jobs_completed: number;
}

interface UseProvidersOptions {
  pollIntervalMs?: number;
}

export function useProviders(
  category?: string | null,
  { pollIntervalMs = 5_000 }: UseProvidersOptions = {},
): {
  providers: Provider[];
  loading: boolean;
  error: string | null;
} {
  const [providers, setProviders] = useState<Provider[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    const fetchProviders = async () => {
      try {
        const qs = category ? `?category=${encodeURIComponent(category)}` : '';
        const resp = await fetch(`${API_URL}/town/exchange/providers${qs}`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = (await resp.json()) as { providers: Provider[] };
        if (cancelled) return;
        setProviders(data.providers ?? []);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'fetch failed');
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    fetchProviders();
    const id = setInterval(fetchProviders, pollIntervalMs);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [category, pollIntervalMs]);

  return { providers, loading, error };
}
