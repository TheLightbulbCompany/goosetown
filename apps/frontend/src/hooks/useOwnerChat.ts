import { useCallback, useEffect, useRef, useState } from 'react';

const API_URL = import.meta.env.VITE_BACKEND_URL ?? 'http://localhost:8000/api/v1';

export interface OwnerChatMessage {
  id: string;
  agentId?: string;
  ownerId?: string;
  direction: 'owner_to_agent' | 'agent_to_owner';
  text: string;
  createdAt: number;
}

interface UseOwnerChatArgs {
  agentId: string;
  getToken?: () => Promise<string | null>;
}

interface UseOwnerChatResult {
  messages: OwnerChatMessage[];
  loading: boolean;
  pending: boolean;
  send: (text: string) => Promise<void>;
}

/**
 * Owner-agent DM chat panel hook.
 *
 * - Fetches REST history on mount (or when ``agentId`` changes).
 * - Listens on the ``window`` for ``gt_owner_chat_event`` CustomEvents that
 *   ``useTownState`` dispatches when the WS forwards ``owner_reply`` /
 *   ``owner_chat_action``. Filters by ``agentId``.
 * - ``send(text)`` posts to /owner-message and optimistically appends the
 *   typed message before the response arrives.
 * - 60s patience timer clears the ``pending`` indicator if no reply lands.
 */
export function useOwnerChat({ agentId, getToken }: UseOwnerChatArgs): UseOwnerChatResult {
  const [messages, setMessages] = useState<OwnerChatMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState(false);
  const lastSentAtRef = useRef<number>(0);
  // Stable ref so the fetch effect doesn't re-fire every render when the
  // caller passes an inline arrow function for getToken.
  const getTokenRef = useRef(getToken);
  useEffect(() => {
    getTokenRef.current = getToken;
  });

  // Fetch history on mount / agentId change.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const token = getTokenRef.current ? await getTokenRef.current() : null;
      const headers: Record<string, string> = { 'Content-Type': 'application/json' };
      if (token) headers.Authorization = `Bearer ${token}`;
      try {
        const r = await fetch(`${API_URL}/town/agent/${agentId}/owner-chat`, { headers });
        if (!r.ok) {
          if (!cancelled) setLoading(false);
          return;
        }
        const body = (await r.json()) as { messages: OwnerChatMessage[] };
        if (!cancelled) {
          setMessages(body.messages);
          setLoading(false);
        }
      } catch {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [agentId]); // intentionally excludes getToken — see getTokenRef above

  // Subscribe to WS bridge events.
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail as {
        event: string;
        agent_id: string;
        text?: string;
        message_id?: string;
        timestamp?: number;
      };
      if (detail.agent_id !== agentId) return;

      if (detail.event === 'owner_reply') {
        const reply: OwnerChatMessage = {
          id: detail.message_id ?? `reply-${Date.now()}`,
          direction: 'agent_to_owner',
          text: detail.text ?? '',
          createdAt: detail.timestamp ?? Date.now(),
        };
        setMessages((prev) => [...prev, reply]);
        setPending(false);
      }
    };
    window.addEventListener('gt_owner_chat_event', handler);
    return () => {
      window.removeEventListener('gt_owner_chat_event', handler);
    };
  }, [agentId]);

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed) return;

      // Optimistic append — replaced with the persisted record on success,
      // dropped on failure.
      const optimisticId = `optimistic-${Date.now()}`;
      const optimistic: OwnerChatMessage = {
        id: optimisticId,
        direction: 'owner_to_agent',
        text: trimmed,
        createdAt: Date.now(),
      };
      setMessages((prev) => [...prev, optimistic]);
      setPending(true);
      lastSentAtRef.current = Date.now();

      const token = getTokenRef.current ? await getTokenRef.current() : null;
      const headers: Record<string, string> = { 'Content-Type': 'application/json' };
      if (token) headers.Authorization = `Bearer ${token}`;

      try {
        const r = await fetch(`${API_URL}/town/agent/${agentId}/owner-message`, {
          method: 'POST',
          headers,
          body: JSON.stringify({ text: trimmed }),
        });
        if (!r.ok) {
          setMessages((prev) => prev.filter((m) => m.id !== optimisticId));
          setPending(false);
          return;
        }
        const body = (await r.json()) as { id: string; createdAt: number };
        setMessages((prev) =>
          prev.map((m) =>
            m.id === optimisticId
              ? { ...m, id: body.id, createdAt: body.createdAt }
              : m,
          ),
        );
        // Patience timer: clear pending if no reply arrives within 60s.
        const sentAt = Date.now();
        setTimeout(() => {
          if (sentAt === lastSentAtRef.current) {
            setPending(false);
          }
        }, 60_000);
      } catch {
        setMessages((prev) => prev.filter((m) => m.id !== optimisticId));
        setPending(false);
      }
    },
    [agentId], // getToken accessed via getTokenRef — stable, no re-creation on token change
  );

  return { messages, loading, pending, send };
}
