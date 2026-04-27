/**
 * Hook to fetch recent public conversations for the sidebar log.
 *
 * Polls ``GET /town/conversations`` every ``POLL_INTERVAL`` ms. The endpoint
 * is public — it returns both active and recently-ended conversations with
 * their full publicLog, so the UI can stream ongoing chats and show history.
 */

import { useEffect, useState } from 'react';

const API_URL = import.meta.env.VITE_BACKEND_URL ?? 'http://localhost:8000/api/v1';

const POLL_INTERVAL = 5000;

export interface ConversationTurn {
  speaker: string;
  text: string;
}

export interface Conversation {
  id: string;
  participant_a: string;
  participant_b: string;
  /**
   * Canonical list of participants (display names, deduped, in join order).
   * Optional for compatibility with older API responses; UI should prefer
   * this field over participant_a/participant_b when present.
   */
  participants?: string[];
  location: string | null;
  started_at: string | null;
  ended_at: string | null;
  turn_count: number;
  topic_summary: string | null;
  public_log: ConversationTurn[];
}

interface ConversationsResponse {
  conversations: Conversation[];
}

export function useConversations(limit: number = 20): {
  conversations: Conversation[];
  loading: boolean;
  error: string | null;
} {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    const fetchConvos = async () => {
      try {
        const resp = await fetch(`${API_URL}/town/conversations?limit=${limit}`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data: ConversationsResponse = await resp.json();
        if (cancelled) return;
        setConversations(data.conversations ?? []);
        setError(null);
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    fetchConvos();
    const id = setInterval(fetchConvos, POLL_INTERVAL);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [limit]);

  return { conversations, loading, error };
}
