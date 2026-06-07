import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useConversations, type Conversation } from '../hooks/useConversations.ts';
import { conversationDuration, timeAgo } from '../convTime.ts';

function formatLocation(loc: string | null): string {
  if (!loc) return 'somewhere';
  return loc.replace(/_/g, ' ');
}

function getParticipants(conv: Conversation): string[] {
  // Prefer the canonical ``participants`` array. Fall back to the legacy
  // two-name pair only if the API didn't populate it (older response).
  // Filter empty strings AND the literal "Unknown" placeholder — the
  // backend stopped emitting "Unknown" in this slot but a future API drift
  // shouldn't produce "Unknown & Unknown" in the row.
  if (conv.participants && conv.participants.length > 0) return conv.participants;
  return [conv.participant_a, conv.participant_b].filter(
    (p): p is string => Boolean(p) && p !== 'Unknown',
  );
}

function formatParticipants(
  participants: string[],
  style: 'row' | 'full',
): string {
  if (participants.length === 0) return 'Unknown';
  if (participants.length === 1) return participants[0];
  if (participants.length === 2) return `${participants[0]} & ${participants[1]}`;
  if (style === 'row' && participants.length > 3) {
    // Row is space-constrained — show first two then "+N".
    return `${participants[0]}, ${participants[1]} +${participants.length - 2}`;
  }
  // Full mode (modal header) and 3-participant rows show everyone joined
  // by commas with "&" before the last one.
  const head = participants.slice(0, -1).join(', ');
  const tail = participants[participants.length - 1];
  return `${head} & ${tail}`;
}

// "📍 on map" navigates to the town view focused on the first participant,
// reusing the existing ?focus=<name> camera pipeline in App.tsx. During a
// conversation the participants stand within a few tiles of each other, so
// framing the first one shows the group. The positions map (useTownState)
// keys by lowercased display name; the focus effect lowercases too.
function useLocateOnMap(): (conv: Conversation) => void {
  const navigate = useNavigate();
  return (conv: Conversation) => {
    const participants = getParticipants(conv);
    const target = participants[0];
    if (!target) return;
    navigate(`/town?focus=${encodeURIComponent(target)}`);
  };
}

function ConversationModal({
  conv,
  onClose,
  onLocate,
}: {
  conv: Conversation;
  onClose: () => void;
  onLocate: (conv: Conversation) => void;
}) {
  const isActive = conv.ended_at == null;
  const turns = conv.public_log ?? [];
  const participants = getParticipants(conv);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/90 p-4"
      onClick={onClose}
    >
      <div
        className="bg-clay-800 border border-clay-600 rounded-lg shadow-xl w-full max-w-lg max-h-[80vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-3 p-4 border-b border-clay-700">
          <div className="min-w-0 flex-1">
            <h2 className="font-display text-brown-100 tracking-wider text-lg break-words">
              {formatParticipants(participants, 'full')}
            </h2>
            <p className="font-body text-xs text-clay-400 mt-1">
              at {formatLocation(conv.location)} · {conv.turn_count} turns{' '}
              {participants.length > 2 ? (
                <span className="text-clay-500 ml-1">· {participants.length} people</span>
              ) : null}
              {isActive ? (
                <span className="text-green-400 ml-1">● live</span>
              ) : null}
            </p>
            <p className="font-body text-[11px] text-clay-500 mt-0.5">
              {timeAgo(conv.started_at)} · {conversationDuration(conv.started_at, conv.ended_at)}
            </p>
          </div>
          <div className="flex flex-col items-end gap-1 shrink-0">
            <button
              type="button"
              onClick={onClose}
              className="text-clay-400 hover:text-brown-100 font-body text-xs px-2 py-1"
            >
              Close
            </button>
            <button
              type="button"
              onClick={() => onLocate(conv)}
              className="text-brown-300 hover:text-brown-100 font-body text-xs px-2 py-1 rounded border border-clay-600 hover:border-clay-500"
            >
              📍 Show on map
            </button>
          </div>
        </div>
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

function ConversationRow({
  conv,
  onOpen,
  onLocate,
}: {
  conv: Conversation;
  onOpen: () => void;
  onLocate: (conv: Conversation) => void;
}) {
  const isActive = conv.ended_at == null;
  const turns = conv.public_log ?? [];
  const preview = turns[turns.length - 1];
  const participants = getParticipants(conv);

  return (
    <div className="rounded bg-clay-800 border border-clay-700 hover:border-clay-500 transition-colors text-xs">
      <button
        type="button"
        onClick={onOpen}
        className="w-full text-left p-2"
      >
        <div className="flex items-center justify-between gap-2">
          <span className="font-display text-brown-200 tracking-wider truncate">
            {formatParticipants(participants, 'row')}
          </span>
          {isActive ? (
            <span className="text-[10px] font-body text-green-400 shrink-0">● live</span>
          ) : (
            <span className="text-[10px] font-body text-clay-400 shrink-0">
              {conv.turn_count} turns
            </span>
          )}
        </div>
        <div className="text-clay-400 font-body flex items-center justify-between gap-2">
          <span className="truncate">at {formatLocation(conv.location)}</span>
          <span className="text-clay-500 shrink-0">{timeAgo(conv.started_at)}</span>
        </div>
        {preview && (
          <div className="mt-1 text-clay-200 font-body italic truncate">
            {preview.speaker}: &ldquo;{preview.text}&rdquo;
          </div>
        )}
      </button>
      <div className="border-t border-clay-700/60 px-2 py-1 flex items-center justify-between">
        <span className="text-[10px] font-body text-clay-500">
          {conversationDuration(conv.started_at, conv.ended_at)}
        </span>
        <button
          type="button"
          onClick={() => onLocate(conv)}
          className="text-[10px] font-body text-brown-300 hover:text-brown-100"
          aria-label="Show these agents on the map"
        >
          📍 on map
        </button>
      </div>
    </div>
  );
}

export default function ConversationsLog() {
  const { conversations, loading, error } = useConversations(20);
  const [openId, setOpenId] = useState<string | null>(null);
  const locateOnMap = useLocateOnMap();

  const openConv = openId ? conversations.find((c) => c.id === openId) ?? null : null;

  const handleLocate = (conv: Conversation): void => {
    setOpenId(null); // close the modal so the map is visible
    locateOnMap(conv);
  };

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
      <div className="flex flex-col gap-2 max-h-[40vh] overflow-y-auto pr-1">
        {conversations.map((c) => (
          <ConversationRow
            key={c.id}
            conv={c}
            onOpen={() => setOpenId(c.id)}
            onLocate={handleLocate}
          />
        ))}
      </div>
      {openConv && (
        <ConversationModal conv={openConv} onClose={() => setOpenId(null)} onLocate={handleLocate} />
      )}
    </div>
  );
}
