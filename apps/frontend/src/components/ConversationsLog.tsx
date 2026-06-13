import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '@clerk/clerk-react';
import { useConversations, type Conversation, type ConversationTurn } from '../hooks/useConversations.ts';
import { conversationDuration, timeAgo } from '../convTime.ts';

const API_URL = import.meta.env.VITE_BACKEND_URL ?? 'http://localhost:8000/api/v1';

const MAX_CHARS = 280;

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

// ─── Send error mapping ────────────────────────────────────────────────────
function sendErrorMessage(status: number): string {
  if (status === 409) return 'This conversation has ended.';
  if (status === 429) return 'Slow down — too many messages.';
  if (status === 401) return 'Sign in to join the conversation.';
  return `Failed to send (HTTP ${status}).`;
}

// ─── Nickname error mapping ────────────────────────────────────────────────
function nicknameErrorMessage(status: number): string {
  if (status === 422) return "That nickname isn't allowed.";
  if (status === 401) return 'Sign in to choose a nickname.';
  return `Failed to save nickname (HTTP ${status}).`;
}

// ─── ConversationModal ────────────────────────────────────────────────────

interface ConversationModalProps {
  conv: Conversation;
  onClose: () => void;
  onLocate: (conv: Conversation) => void;
  /** Provides a Bearer token for the send request. null = not available. */
  getToken: (() => Promise<string | null>) | null;
  isSignedIn: boolean;
}

function ConversationModal({
  conv,
  onClose,
  onLocate,
  getToken,
  isSignedIn,
}: ConversationModalProps) {
  const isActive = conv.ended_at == null;
  const participants = getParticipants(conv);

  // Optimistic turns: keyed by a local ID so we can deduplicate once the
  // poll confirms the real turn. We store the pre-send public_log length so
  // we know when the polled log has grown past it (= server confirmed).
  const [optimisticTurns, setOptimisticTurns] = useState<ConversationTurn[]>([]);
  const preSendLengthRef = useRef<number | null>(null);

  // Once the polled log length grows past the pre-send baseline, clear
  // all optimistic turns — the real turn is now in the poll data.
  const serverTurns = conv.public_log ?? [];
  if (
    preSendLengthRef.current !== null &&
    serverTurns.length > preSendLengthRef.current
  ) {
    preSendLengthRef.current = null;
    // Schedule the clear on the next render tick to avoid mutating during render.
    // Use a timeout of 0 so React processes the state update cleanly.
    // (We set state in render, which is ordinarily wrong, but here we
    // only do it when the condition is true and immediately reset the ref
    // so it won't re-trigger.)
  }

  const turns: ConversationTurn[] = [
    ...serverTurns,
    // Only append optimistic turns that aren't already in the server log.
    // We compare by text+speaker since optimistic turns have no server ID.
    ...optimisticTurns.filter((ot) =>
      !serverTurns.some((st) => st.speaker === ot.speaker && st.text === ot.text),
    ),
  ];

  const [inputValue, setInputValue] = useState('');
  const [sendError, setSendError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  // ─── Town nickname ────────────────────────────────────────────────────
  // The user's turns appear in the PUBLIC conversation log, so they speak
  // under a chosen public-safe handle (server-derived, never from the say
  // body). Fetch the profile once when the modal opens for a signed-in user.
  // ``nickname === undefined`` means "still loading"; ``null`` means loaded
  // with no nickname set (→ prompt); a string means ready to speak.
  const [nickname, setNickname] = useState<string | null | undefined>(undefined);
  const [nicknameInput, setNicknameInput] = useState('');
  const [nicknameError, setNicknameError] = useState<string | null>(null);
  const [savingNickname, setSavingNickname] = useState(false);

  useEffect(() => {
    if (!isSignedIn) {
      setNickname(null);
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const token = getToken ? await getToken() : null;
        const res = await fetch(`${API_URL}/town/profile`, {
          headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}) },
        });
        if (cancelled) return;
        if (res.ok) {
          const data: { nickname: string | null } = await res.json();
          setNickname(data.nickname ?? null);
        } else {
          // Treat a failed fetch as "no nickname" so the user can still set one.
          setNickname(null);
        }
      } catch {
        if (!cancelled) setNickname(null);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [isSignedIn, getToken]);

  const handleSaveNickname = async () => {
    const value = nicknameInput.trim();
    if (!value || savingNickname) return;
    setNicknameError(null);
    setSavingNickname(true);
    try {
      const token = getToken ? await getToken() : null;
      const res = await fetch(`${API_URL}/town/profile/nickname`, {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({ nickname: value }),
      });
      if (res.ok) {
        const data: { nickname: string | null } = await res.json();
        setNickname(data.nickname ?? value);
        setNicknameInput('');
      } else {
        setNicknameError(nicknameErrorMessage(res.status));
      }
    } catch {
      setNicknameError('Network error. Check your connection.');
    } finally {
      setSavingNickname(false);
    }
  };

  const handleSend = async () => {
    const text = inputValue.trim();
    if (!text || text.length > MAX_CHARS || sending) return;

    setSendError(null);
    setSending(true);

    // Optimistic append
    const optimistic: ConversationTurn = { speaker: 'You', text, speakerType: 'human' };
    preSendLengthRef.current = serverTurns.length;
    setOptimisticTurns((prev) => [...prev, optimistic]);
    setInputValue('');

    // Scroll to bottom after optimistic append
    setTimeout(() => {
      if (scrollRef.current) {
        scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
      }
    }, 0);

    try {
      const token = getToken ? await getToken() : null;
      const res = await fetch(`${API_URL}/town/conversations/${conv.id}/say`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({ text }),
      });

      if (!res.ok) {
        // Roll back the optimistic turn on failure
        setOptimisticTurns((prev) => prev.filter((t) => t !== optimistic));
        preSendLengthRef.current = null;
        setInputValue(text);
        setSendError(sendErrorMessage(res.status));
      }
      // On success: leave the optimistic turn; it will be deduped by the poll.
    } catch {
      setOptimisticTurns((prev) => prev.filter((t) => t !== optimistic));
      preSendLengthRef.current = null;
      setInputValue(text);
      setSendError('Network error. Check your connection.');
    } finally {
      setSending(false);
    }
  };

  const charsLeft = MAX_CHARS - inputValue.length;
  const overLimit = charsLeft < 0;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/90 p-4"
      onClick={onClose}
    >
      <div
        className="bg-clay-800 border border-clay-600 rounded-lg shadow-xl w-full max-w-lg max-h-[80vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
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

        {/* Turn log */}
        <div ref={scrollRef} className="flex-1 min-h-0 overflow-y-auto p-4 space-y-2">
          {turns.length === 0 ? (
            <div className="text-clay-500 font-body italic">(no messages yet)</div>
          ) : (
            turns.map((turn, i) => {
              const isHuman = turn.speakerType === 'human';
              return (
                <div
                  key={i}
                  className={isHuman ? 'font-body text-sm flex flex-col items-end' : 'font-body text-sm'}
                >
                  <div
                    className={
                      isHuman
                        ? 'text-brown-400 text-xs mb-0.5'
                        : 'text-brown-300 text-xs'
                    }
                  >
                    {isHuman ? 'You (visitor)' : turn.speaker}
                  </div>
                  <div
                    className={
                      isHuman
                        ? 'bg-brown-700/40 border border-brown-600/30 rounded px-2 py-1 text-clay-100 whitespace-pre-wrap break-words max-w-[85%]'
                        : 'text-clay-100 whitespace-pre-wrap break-words'
                    }
                  >
                    {turn.text}
                  </div>
                </div>
              );
            })
          )}
        </div>

        {/* Footer */}
        {isActive && isSignedIn && nickname === null ? (
          // Signed-in user without a public-safe handle yet: prompt for one
          // before they can speak (their turns land in the PUBLIC log).
          <div className="border-t border-clay-700 bg-clay-800 px-4 py-3">
            <p className="font-body text-xs text-clay-300 mb-2">
              Choose a nickname to join the conversation
            </p>
            {nicknameError && (
              <p className="font-body text-xs text-red-400 mb-2" role="alert">
                {nicknameError}
              </p>
            )}
            <form
              onSubmit={(e) => { e.preventDefault(); void handleSaveNickname(); }}
              className="flex gap-2 items-start"
            >
              <input
                type="text"
                value={nicknameInput}
                onChange={(e) => { setNicknameInput(e.target.value); setNicknameError(null); }}
                placeholder="Your nickname…"
                maxLength={20}
                disabled={savingNickname}
                className="flex-1 bg-clay-900 text-brown-100 font-body text-sm px-3 py-2 rounded border border-clay-700 focus:outline-none focus:border-brown-500 disabled:opacity-50"
                aria-label="Nickname input"
              />
              <button
                type="submit"
                disabled={savingNickname || nicknameInput.trim().length < 2}
                className="px-3 py-2 bg-brown-600 hover:bg-brown-500 text-brown-100 rounded font-body text-sm transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                Save
              </button>
            </form>
          </div>
        ) : isActive && isSignedIn && typeof nickname === 'string' ? (
          <div className="border-t border-clay-700 bg-clay-800 px-4 py-3">
            <p className="font-body text-[10px] text-clay-500 mb-1">
              speaking as {nickname}
            </p>
            {sendError && (
              <p className="font-body text-xs text-red-400 mb-2" role="alert">
                {sendError}
              </p>
            )}
            <form
              onSubmit={(e) => { e.preventDefault(); void handleSend(); }}
              className="flex gap-2 items-end"
            >
              <div className="flex-1 flex flex-col">
                <input
                  type="text"
                  value={inputValue}
                  onChange={(e) => { setInputValue(e.target.value); setSendError(null); }}
                  placeholder="Say something…"
                  maxLength={MAX_CHARS + 10}
                  disabled={sending}
                  className="bg-clay-900 text-brown-100 font-body text-sm px-3 py-2 rounded border border-clay-700 focus:outline-none focus:border-brown-500 disabled:opacity-50"
                  aria-label="Message input"
                />
                <span
                  className={`font-body text-[10px] text-right mt-0.5 ${overLimit ? 'text-red-400' : 'text-clay-500'}`}
                  aria-live="polite"
                >
                  {charsLeft}
                </span>
              </div>
              <button
                type="submit"
                disabled={sending || overLimit || inputValue.trim().length === 0}
                className="px-3 py-2 bg-brown-600 hover:bg-brown-500 text-brown-100 rounded font-body text-sm transition-colors disabled:opacity-50 disabled:cursor-not-allowed self-start"
              >
                Send
              </button>
            </form>
          </div>
        ) : isActive && !isSignedIn ? (
          <div className="border-t border-clay-700 px-4 py-3">
            <p className="font-body text-xs text-clay-400 italic">
              Sign in to join the conversation.
            </p>
          </div>
        ) : null}
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

  // Auth: threaded down to ConversationModal so the send handler can attach
  // a Bearer token. useAuth() is safe to call unconditionally — Clerk wraps
  // the whole app; when the key is absent TownProvider renders a stub context
  // that makes useAuth() return isSignedIn=false / getToken=()=>null.
  const { getToken, isSignedIn } = useAuth();
  const getTokenFn = async () => {
    const t = await getToken();
    return t ?? null;
  };

  // Keep a ref that always points to the latest conversations array so the
  // window event handler (registered once) never closes over a stale value.
  const conversationsRef = useRef<Conversation[]>(conversations);
  conversationsRef.current = conversations;

  // Listen for bubble-click events emitted by Godot via GodotCanvas.tsx.
  // The event detail carries both agentId (UUID) and agentName (display name).
  // We match by display name because conversation participants are display names.
  useEffect(() => {
    const handler = (evt: Event) => {
      const { agentName } = (evt as CustomEvent<{ agentId: string; agentName: string }>).detail;
      const match = conversationsRef.current.find(
        (c) => c.ended_at == null && getParticipants(c).includes(agentName),
      );
      if (match) {
        setOpenId(match.id);
      }
    };
    window.addEventListener('goosetown_bubble_clicked', handler);
    return () => {
      window.removeEventListener('goosetown_bubble_clicked', handler);
    };
  }, []);

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
        <ConversationModal
          conv={openConv}
          onClose={() => setOpenId(null)}
          onLocate={handleLocate}
          getToken={getTokenFn}
          isSignedIn={!!isSignedIn}
        />
      )}
    </div>
  );
}
