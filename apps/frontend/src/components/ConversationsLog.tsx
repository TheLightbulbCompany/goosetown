import { useState } from 'react';
import { useConversations, type Conversation } from '../hooks/useConversations.ts';

function formatLocation(loc: string | null): string {
  if (!loc) return 'somewhere';
  return loc.replace(/_/g, ' ');
}

/**
 * Render the participant title for a conversation row or modal header.
 *
 * Prefers the canonical ``participants`` array (supports group chats of
 * 3+ agents); falls back to the legacy ``participant_a`` / ``participant_b``
 * pair for older response shapes or offline test fixtures that haven't
 * migrated. Long lists of 4+ names collapse to "A, B, C, +2" in row
 * context so the sidebar doesn't wrap.
 */
function formatParticipants(conv: Conversation, style: 'row' | 'full'): string {
  const list = conv.participants && conv.participants.length > 0
    ? conv.participants
    : [conv.participant_a, conv.participant_b].filter(Boolean);
  if (list.length === 0) return 'Unknown';
  if (list.length === 1) return list[0];
  if (list.length === 2) return `${list[0]} & ${list[1]}`;
  if (style === 'row' && list.length > 3) {
    return `${list.slice(0, 2).join(', ')}, +${list.length - 2}`;
  }
  return list.slice(0, -1).join(', ') + ' & ' + list[list.length - 1];
}

function ConversationModal({ conv, onClose }: { conv: Conversation; onClose: () => void }) {
  const isActive = conv.ended_at == null;
  const turns = conv.public_log ?? [];

  return (
    <div
      // Near-opaque backdrop so the town isn't distracting through the
      // modal. Previous ``bg-black/60`` was light enough that the canvas
      // animation kept drawing attention behind the log.
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/90 p-4"
      onClick={onClose}
    >
      <div
        className="bg-clay-800 border border-clay-600 rounded-lg shadow-xl w-full max-w-lg h-[80vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-3 p-4 border-b border-clay-700 shrink-0">
          <div className="min-w-0 flex-1">
            <h2 className="font-display text-brown-100 tracking-wider text-lg truncate">
              {formatParticipants(conv, 'full')}
            </h2>
            <p className="font-body text-xs text-clay-400 mt-1">
              at {formatLocation(conv.location)} · {conv.turn_count} turns{' '}
              {isActive ? (
                <span className="text-green-400 ml-1">● live</span>
              ) : null}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="text-clay-400 hover:text-brown-100 font-body text-xs px-2 py-1"
          >
            Close
          </button>
        </div>
        {/* ``min-h-0`` is the flex-scroll gotcha — without it, a ``flex-1``
            child will expand to fit its content instead of respecting the
            parent's height constraint, and ``overflow-y-auto`` never kicks
            in for long logs. */}
        <div className="flex-1 min-h-0 overflow-y-auto p-4 space-y-2">
          {turns.length === 0 ? (
            <div className="text-clay-500 font-body italic">(no messages yet)</div>
          ) : (
            turns.map((turn, i) => (
              <div key={i} className="font-body text-sm">
                <div className="text-brown-300 text-xs">{turn.speaker}</div>
                <div className="text-clay-100 whitespace-pre-wrap break-words">{turn.text}</div>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}

function ConversationRow({ conv, onClick }: { conv: Conversation; onClick: () => void }) {
  const isActive = conv.ended_at == null;
  const turns = conv.public_log ?? [];
  const preview = turns[turns.length - 1];

  return (
    <button
      type="button"
      onClick={onClick}
      className="w-full text-left rounded bg-clay-800 border border-clay-700 hover:border-clay-500 transition-colors p-2 text-xs"
    >
      <div className="flex items-center justify-between gap-2">
        <span className="font-display text-brown-200 tracking-wider truncate">
          {formatParticipants(conv, 'row')}
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
      {preview && (
        <div className="mt-1 text-clay-200 font-body italic truncate">
          {preview.speaker}: &ldquo;{preview.text}&rdquo;
        </div>
      )}
    </button>
  );
}

export default function ConversationsLog() {
  const { conversations, loading, error } = useConversations(20);
  const [openId, setOpenId] = useState<string | null>(null);

  const openConv = openId ? conversations.find((c) => c.id === openId) ?? null : null;

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
      {/* Cap the sidebar list height so a busy town doesn't push the rest
          of the agent sidebar off-screen. ``overflow-y-auto`` gives the
          log its own scroll region. */}
      <div className="flex flex-col gap-2 max-h-[40vh] overflow-y-auto pr-1">
        {conversations.map((c) => (
          <ConversationRow key={c.id} conv={c} onClick={() => setOpenId(c.id)} />
        ))}
      </div>
      {openConv && <ConversationModal conv={openConv} onClose={() => setOpenId(null)} />}
    </div>
  );
}
