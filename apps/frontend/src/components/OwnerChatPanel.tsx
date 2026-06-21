import { useEffect, useRef, useState, FormEvent } from 'react';
import { useAuth } from '@clerk/clerk-react';
import clsx from 'clsx';

import { useOwnerChat } from '../hooks/useOwnerChat';

interface OwnerChatPanelProps {
  agentId: string;
  agentName: string;
  onClose: () => void;
}

export default function OwnerChatPanel({ agentId, agentName, onClose }: OwnerChatPanelProps) {
  const { getToken } = useAuth();
  // Wrap so the hook receives a stable function shape (Promise<string | null>).
  const getTokenFn = async () => {
    const t = await getToken();
    return t ?? null;
  };
  const { messages, loading, pending, send } = useOwnerChat({ agentId, getToken: getTokenFn });

  const [input, setInput] = useState('');
  const scrollRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to bottom on new messages or pending change.
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages.length, pending]);

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    const text = input.trim();
    if (!text) return;
    setInput('');
    void send(text);
  };

  return (
    /* z-[45]: above the mobile agents drawer (z-40, where Chat is launched
       from) but below JoinTownModal (z-50). md:z-30 = original desktop z. */
    <div className="fixed z-[45] md:z-30 bg-clay-900 flex flex-col shadow-2xl inset-x-0 bottom-0 top-[30%] rounded-t-xl border-t border-clay-700 pb-[env(safe-area-inset-bottom)] md:inset-x-auto md:bottom-auto md:top-0 md:right-0 md:h-full md:w-[360px] md:rounded-none md:border-t-0 md:border-l md:pb-0">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-clay-700 bg-clay-800">
        <h3 className="font-display text-lg text-brown-100 tracking-wider">
          {agentName}
        </h3>
        <button
          aria-label="Close"
          onClick={onClose}
          className="text-clay-300 hover:text-brown-200 text-2xl leading-none px-2"
        >
          ×
        </button>
      </div>

      {/* Messages */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-3 space-y-2">
        {loading && (
          <p className="font-body text-xs text-clay-300">Loading…</p>
        )}
        {!loading && messages.length === 0 && (
          <p className="font-body text-xs text-clay-300">
            No messages yet. Say something to {agentName}.
          </p>
        )}
        {messages.map((m) => (
          <div
            key={m.id}
            className={clsx(
              'font-body text-sm rounded p-2 max-w-[80%] break-words',
              m.direction === 'owner_to_agent'
                ? 'bg-brown-700 text-brown-100 ml-auto'
                : 'bg-clay-700 text-brown-200',
            )}
          >
            {m.text}
          </div>
        ))}
        {pending && (
          <p className="font-body text-xs text-clay-300 italic">
            {agentName} is thinking…
          </p>
        )}
      </div>

      {/* Footer: input */}
      <form onSubmit={onSubmit} className="border-t border-clay-700 bg-clay-800 px-4 py-3 flex gap-2">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={`Message ${agentName}…`}
          className="flex-1 bg-clay-900 text-brown-100 font-body text-sm px-3 py-2 rounded border border-clay-700 focus:outline-none focus:border-brown-500"
        />
        <button
          type="submit"
          className="px-3 py-2 bg-brown-600 hover:bg-brown-500 text-brown-100 rounded font-body text-sm transition-colors"
        >
          Send
        </button>
      </form>
    </div>
  );
}
