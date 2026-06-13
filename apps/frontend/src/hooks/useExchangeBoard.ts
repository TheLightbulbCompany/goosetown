/**
 * Hook to fetch the public Exchange board for the sidebar notice board.
 *
 * Polls ``GET /town/exchange/jobs`` every ``pollIntervalMs`` ms (default 5s).
 * The endpoint is public — no auth required. The payload is the sealed-bid
 * safe view: no deliverables, no bid amounts, just the notices a passer-by
 * could read off the board outside the bank.
 */

import { useEffect, useState } from 'react';

const API_URL = import.meta.env.VITE_BACKEND_URL ?? 'http://localhost:8000/api/v1';

export interface ExchangeJob {
  job_id: string;
  title: string;
  description: string;
  max_price: number;
  agreed_price: number;
  status: string;
  consumer_name: string;
  provider_name: string;
  created_at: string;
  bid_count: number;
  /** Optional — when the API includes it, the OPEN notice shows the
   * draining town-clock strip. Epoch seconds or ISO string. */
  bid_window_ends_at?: string | number;
}

interface UseExchangeBoardOptions {
  pollIntervalMs?: number;
}

export function useExchangeBoard({ pollIntervalMs = 5_000 }: UseExchangeBoardOptions = {}): {
  jobs: ExchangeJob[];
  loading: boolean;
  error: string | null;
} {
  const [jobs, setJobs] = useState<ExchangeJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    const fetchJobs = async () => {
      try {
        const resp = await fetch(`${API_URL}/town/exchange/jobs`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = (await resp.json()) as { jobs: ExchangeJob[] };
        if (cancelled) return;
        setJobs(data.jobs ?? []);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'fetch failed');
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    fetchJobs();
    const id = setInterval(fetchJobs, pollIntervalMs);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [pollIntervalMs]);

  return { jobs, loading, error };
}
