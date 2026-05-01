import { useEffect, useState } from 'react';
import { useAuth } from '@clerk/clerk-react';

export interface QuotaData {
  agents: { used: number; limit: number };
  sprite_gen: { used_24h: number; limit: number };
}

const API_URL = import.meta.env.VITE_BACKEND_URL ?? 'http://localhost:8000/api/v1';
const POLL_INTERVAL_MS = 30_000; // 30 seconds — quota changes rarely

export function useQuota(): QuotaData | null {
  const { getToken, isSignedIn } = useAuth();
  const [data, setData] = useState<QuotaData | null>(null);

  useEffect(() => {
    if (!isSignedIn) return;
    let cancelled = false;

    async function fetchQuota() {
      try {
        const token = await getToken();
        if (!token) return;
        const response = await fetch(`${API_URL}/town/quota`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!response.ok) return;
        const body = (await response.json()) as QuotaData;
        if (!cancelled) setData(body);
      } catch {
        // Silent — quota is informational, not blocking.
      }
    }

    void fetchQuota();
    const id = setInterval(fetchQuota, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [getToken, isSignedIn]);

  return data;
}
