import { useState } from 'react';
import { useConversations, type Conversation } from '../hooks/useConversations.ts';

function formatLocation(loc: string | null): string {
  if (!loc) return 'somewhere';
  return loc.replace(/_/g, ' ');
}

function ConversationCard({ conv }: { conv: Conversation }) {
  const [expanded, setExpanded] = useState(false);
  const isActive = conv.ended_at == null;
  const turns = conv.public_log ?? [];
  const preview = turns[turns.length - 1];

  return (
    <div className="rounded bg-clay-800 border border-clay-700 p-2 text-xs">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="w-full text-left"
      >
        <div className="flex items-center justify-between gap-2">
          <span className="font-display text-brown-200 tracking-wider truncate">
            {conv.participant_a} & {conv.participant_b}
          </span>
          {isActive ? (
            <span className="text-[10px] font-body text-green-400 shrink-0">● live</span>
          ) : (
            <span className="text-[10px] font-body text-clay-400 shrink-0">
              {conv.turn_count} turns
            </span>
          )}
        </div>
        <div className="text-clay-400 font-body">at {formatLocation(conv.location)}</div>
        {preview && !expanded && (
          <div className="mt-1 text-clay-200 font-body italic truncate">
            {preview.speaker}: "{preview.text}"
          </div>
        )}
      </button>

      {expanded && (
        <div className="mt-2 space-y-1 max-h-56 overflow-y-auto pr-1">
          {turns.length === 0 ? (
            <div className="text-clay-500 font-body italic">(no messages yet)</div>
          ) : (
            turns.map((turn, i) => (
              <div key={i} className="font-body">
                <span className="text-brown-300">{turn.speaker}:</span>{' '}
                <span className="text-clay-200">{turn.text}</span>
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}

export default function ConversationsLog() {
  const { conversations, loading, error } = useConversations(20);

  return (
    <div className="mt-6">
      <h3 className="font-display text-sm text-clay-300 tracking-wider mb-2">
        Conversations
      </h3>
      {loading && conversations.length === 0 && (
        <p className="text-clay-500 text-xs font-body">Loading…</p>
      )}
      {error && !loading && (
        <p className="text-red-400 text-xs font-body">Failed to load: {error}</p>
      )}
      {!loading && conversations.length === 0 && !error && (
        <p className="text-clay-500 text-xs font-body italic">No chats yet.</p>
      )}
      <div className="flex flex-col gap-2">
        {conversations.map((c) => (
          <ConversationCard key={c.id} conv={c} />
        ))}
      </div>
    </div>
  );
}
