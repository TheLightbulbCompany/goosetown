/**
 * Lazy passbook (ledger) fetch for one agent.
 *
 * Clerk-authed ``GET /town/agent/{id}/ledger`` — fired once, the first time
 * ``enabled`` flips true (i.e. when the owner expands the Passbook section
 * on an ApartmentCard). No polling: a passbook is something you open and
 * read, not a live ticker.
 */

import { useEffect, useRef, useState } from 'react';
import { useAuth } from '@clerk/clerk-react';

const API_URL = import.meta.env.VITE_BACKEND_URL ?? 'http://localhost:8000/api/v1';

export interface LedgerEntry {
  type: string;
  amount: number;
  balance_after: number;
  ref_id: string;
  at: string;
}

export interface PassbookData {
  balance: number;
  trust_tier: string;
  trust_score: number;
  entries: LedgerEntry[];
}

export function usePassbook(agentId: string, enabled: boolean): {
  data: PassbookData | null;
  loading: boolean;
  error: string | null;
} {
  const { getToken } = useAuth();
  const [data, setData] = useState<PassbookData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const startedRef = useRef(false);
  // Stable ref so the lazy-fetch effect keys only on ``enabled``/``agentId``.
  const getTokenRef = useRef(getToken);
  useEffect(() => {
    getTokenRef.current = getToken;
  });

  useEffect(() => {
    if (!enabled || startedRef.current) return;
    startedRef.current = true;
    let cancelled = false;

    (async () => {
      setLoading(true);
      try {
        const token = await getTokenRef.current();
        const resp = await fetch(`${API_URL}/town/agent/${agentId}/ledger`, {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const body = (await resp.json()) as PassbookData;
        if (cancelled) return;
        setData(body);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'fetch failed');
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [agentId, enabled]);

  return { data, loading, error };
}
