import { useState } from 'react';
import { Link } from 'react-router-dom';
import clsx from 'clsx';
import type { ApartmentAgent } from '../hooks/useApartment';
import { usePassbook } from '../hooks/usePassbook';
import { sceneById } from '../scenes';
import { SpriteWaitOverlay } from './SpriteWaitOverlay';
import CrumbAmount from './CrumbAmount';

// A ``status_message`` like "Learning to cook…" is set by the backend's
// ``_generate_custom_animation`` task while the agent is waiting on a
// PixelLab custom-animation build (1-3 min per animation). Detecting it
// here lets the card show a distinct "Learning" indicator rather than
// the default grey-dot / Idle fallback that would otherwise leave the
// viewer wondering why the agent is standing still.
function isLearning(statusMessage: string | null): boolean {
  return !!statusMessage && statusMessage.startsWith('Learning');
}

function statusLabel(activity: string | null, statusMessage: string | null): string {
  if (isLearning(statusMessage)) return 'Learning';
  if (!activity) return 'Idle';
  switch (activity) {
    case 'sleeping':
      return 'Sleeping';
    case 'idle':
      return 'Idle';
    case 'walking':
      return 'Walking';
    case 'chatting':
      return 'Chatting';
    case 'thinking':
      return 'Thinking';
    default:
      return activity.charAt(0).toUpperCase() + activity.slice(1);
  }
}

function statusColor(activity: string | null, statusMessage: string | null): string {
  if (isLearning(statusMessage)) return 'bg-purple-500';
  switch (activity) {
    case 'sleeping':
      return 'bg-clay-500';
    case 'chatting':
      return 'bg-green-500';
    case 'walking':
      return 'bg-yellow-500';
    default:
      return 'bg-clay-300';
  }
}

// Short passbook timestamp: "MM-DD HH:mm". Falls back to the raw string
// when the backend hands us something unparsable.
function shortAt(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (x: number) => String(x).padStart(2, '0');
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function moodEmoji(mood: string | null): string {
  switch (mood) {
    case 'happy':
      return ':)';
    case 'sad':
      return ':(';
    case 'excited':
      return ':D';
    case 'tired':
      return '-_-';
    case 'neutral':
      return ':|';
    default:
      return ':|';
  }
}

interface ApartmentCardProps {
  agent: ApartmentAgent;
  onChatClick?: (agentId: string, agentName: string) => void;
  /** When provided and agent is active, renders a "Remove" link that
   * soft-deletes the agent (frees a quota slot; preserves history). */
  onRemove?: (agentId: string, agentName: string) => void;
  /** When provided and agent is inactive, renders a "Reactivate" link. */
  onReactivate?: (agentId: string, agentName: string) => void;
}

export default function ApartmentCard({
  agent,
  onChatClick,
  onRemove,
  onReactivate,
}: ApartmentCardProps) {
  const energyPercent = Math.max(0, Math.min(100, agent.energy));
  const hasWallet = typeof agent.wallet_balance === 'number';
  // Passbook is lazy: the ledger fetch only fires on first expand.
  const [passbookOpen, setPassbookOpen] = useState(false);
  const passbook = usePassbook(agent.agent_id, passbookOpen && hasWallet);

  return (
    <div
      className={clsx(
        'box bg-brown-800 p-4 flex flex-col gap-3',
        !agent.is_active && 'opacity-50',
      )}
    >
      {/* Header: character sprite + name */}
      <div className="flex items-center gap-3">
        <div className="relative w-12 h-12 bg-clay-700 rounded flex items-center justify-center text-brown-200 font-display text-lg shrink-0 overflow-hidden">
          {agent.sprite_url ? (
            <img
              src={agent.sprite_url}
              alt={agent.display_name}
              className="w-12 h-12 object-none [image-rendering:pixelated]"
              style={{ objectPosition: '-24px 0' }}
            />
          ) : (
            '?'
          )}
          {!agent.sprite_ready && !agent.avatar_state && agent.created_at != null && (
            <SpriteWaitOverlay createdAt={agent.created_at} />
          )}
        </div>
        <div className="flex-1 min-w-0">
          <h3 className="font-display text-lg text-brown-100 truncate tracking-wider">
            {agent.display_name}
          </h3>
          {agent.avatar_state === 'queued' && (
            <p className="font-body text-xs text-clay-100">Avatar coming later. You can still use the town.</p>
          )}
        </div>
        {/* Status indicator */}
        <div className="flex items-center gap-1.5">
          <span
            className={clsx(
              'w-2 h-2 rounded-full',
              statusColor(agent.current_activity, agent.status_message),
            )}
          />
          <span className="font-body text-xs text-clay-100">
            {statusLabel(agent.current_activity, agent.status_message)}
          </span>
        </div>
      </div>

      {/* Location — town locations (plaza, library, cafe) get a "Town - " prefix
          because their names overlap with interior scene ids; interiors use
          specific spot names (bed, reading_armchair) that read unambiguously. */}
      {agent.current_location && (
        <div className="font-body text-xs text-clay-300">
          Location: <span className="text-brown-200">
            {(() => {
              const def = sceneById(agent.location_context ?? '');
              return def?.id === 'town' ? `${def.displayName} - ` : '';
            })()}
            {agent.current_location}
          </span>
          {agent.current_spot && agent.current_spot !== agent.current_location && (
            <span className="text-clay-400 ml-1">({agent.current_spot})</span>
          )}
        </div>
      )}

      {/* Status message — rendered as agent speech (quoted) by default.
          Learning messages are system-set, not agent-spoken, so they render
          without quotes and with a distinct tone. */}
      {agent.status_message && isLearning(agent.status_message) && (
        <p className="font-body text-xs text-purple-300">{agent.status_message}</p>
      )}
      {agent.status_message && !isLearning(agent.status_message) && (
        <p className="font-body text-xs text-clay-100 italic">"{agent.status_message}"</p>
      )}

      {/* Current goal */}
      {agent.currentGoal && (
        <div className="font-body text-xs text-clay-300 italic truncate">
          Goal: <span className="text-brown-200">{agent.currentGoal}</span>
        </div>
      )}

      {/* Mood + Energy */}
      <div className="flex items-center gap-4">
        <div className="font-body text-xs text-clay-300">
          Mood: <span className="text-brown-200">{moodEmoji(agent.mood)} {agent.mood || 'neutral'}</span>
        </div>
        {hasWallet && (
          <div className="font-body text-xs text-clay-300">
            Crumbs:{' '}
            <CrumbAmount n={agent.wallet_balance!} className="text-brown-200" />
            {agent.trust_tier && <span className="text-clay-400 ml-1">({agent.trust_tier})</span>}
          </div>
        )}
        <div className="flex-1 flex items-center gap-2">
          <span className="font-body text-xs text-clay-300">Energy:</span>
          <div className="flex-1 h-2 bg-clay-700 rounded-full overflow-hidden">
            <div
              className={clsx(
                'h-full rounded-full transition-all',
                energyPercent > 60 ? 'bg-green-500' : energyPercent > 30 ? 'bg-yellow-500' : 'bg-red-500',
              )}
              style={{ width: `${energyPercent}%` }}
            />
          </div>
          <span className="font-body text-xs text-clay-300 w-8 text-right">{energyPercent}%</span>
        </div>
      </div>

      {/* Passbook — collapsible ledger view, only for agents with a wallet.
          The ledger fetch is lazy (first expand) via usePassbook. */}
      {hasWallet && (
        <div>
          <button
            type="button"
            onClick={() => setPassbookOpen((o) => !o)}
            aria-expanded={passbookOpen}
            className="font-body text-xs text-clay-300 hover:text-brown-200 transition-colors flex items-center gap-1"
          >
            <span aria-hidden="true">{passbookOpen ? '▾' : '▸'}</span>
            Passbook
          </button>
          {passbookOpen && (
            <div className="mt-2 border border-clay-700 p-2 flex flex-col gap-1.5">
              {passbook.loading && (
                <p className="font-body text-[11px] text-clay-500 italic">Opening passbook…</p>
              )}
              {passbook.error && (
                <p className="font-body text-[11px] text-red-400">
                  Passbook error: {passbook.error}
                </p>
              )}
              {passbook.data && (
                <>
                  <div className="flex items-center gap-2">
                    <CrumbAmount
                      n={passbook.data.balance}
                      className="text-xs text-brown-200"
                    />
                    {/* Trust stamp: tier in a bordered chip */}
                    <span className="font-display lowercase text-[10px] tracking-wider border border-clay-500 text-clay-100 px-1">
                      {passbook.data.trust_tier}
                    </span>
                  </div>
                  {passbook.data.entries.length === 0 ? (
                    <p className="font-body text-[11px] text-clay-500 italic">
                      No entries yet — the first page is blank.
                    </p>
                  ) : (
                    passbook.data.entries.slice(0, 15).map((e, i) => (
                      <div
                        key={`${e.ref_id}-${i}`}
                        className="font-body text-[11px] text-clay-300 flex items-center gap-1"
                      >
                        <span className="text-clay-500 shrink-0">{shortAt(e.at)}</span>
                        <span aria-hidden="true" className="text-clay-500">·</span>
                        <span className="truncate">{e.type}</span>
                        <span aria-hidden="true" className="text-clay-500">·</span>
                        <CrumbAmount
                          n={e.amount}
                          signed
                          className={clsx(
                            'shrink-0',
                            e.amount >= 0 ? 'text-gold-300' : 'text-clay-300',
                          )}
                        />
                        <span aria-hidden="true" className="text-clay-500">·</span>
                        <CrumbAmount
                          n={e.balance_after}
                          className="text-clay-500 shrink-0 ml-auto"
                        />
                      </div>
                    ))
                  )}
                </>
              )}
            </div>
          )}
        </div>
      )}

      {/* Action row: Chat button + view in <scene> link + Remove/Reactivate.
       * All right-aligned. Active agents see Chat/View/Remove; inactive
       * agents see only Reactivate (chat/view make no sense for despawned
       * agents). */}
      {(() => {
        const def = sceneById(agent.location_context ?? '');
        const showViewLink = def && def.showInNav !== 'never' && agent.is_active;
        const href = showViewLink
          ? def.id === 'town'
            ? `/?focus=${encodeURIComponent(agent.display_name)}`
            : def.route
          : null;
        const showChat = onChatClick && agent.is_active;
        const showRemove = onRemove && agent.is_active;
        const showReactivate = onReactivate && !agent.is_active;
        if (!showChat && !showViewLink && !showRemove && !showReactivate) return null;
        return (
          <div className="flex flex-wrap justify-end items-center gap-1 mt-1">
            {showChat && (
              <button
                onClick={() => onChatClick!(agent.agent_id, agent.display_name)}
                className="font-body text-xs text-clay-300 hover:text-brown-200 active:text-brown-200 active:bg-clay-700/40 transition-colors px-2 py-2 -my-1 rounded"
              >
                Chat &rarr;
              </button>
            )}
            {showViewLink && href && (
              <Link
                to={href}
                className="font-body text-xs text-clay-300 hover:text-brown-200 active:text-brown-200 active:bg-clay-700/40 transition-colors px-2 py-2 -my-1 rounded"
              >
                View in {def!.displayName.toLowerCase()} &rarr;
              </Link>
            )}
            {showRemove && (
              <button
                onClick={() => onRemove!(agent.agent_id, agent.display_name)}
                className="font-body text-xs text-red-400/70 hover:text-red-300 active:text-red-300 active:bg-clay-700/40 transition-colors px-2 py-2 -my-1 rounded"
                title="Soft-delete: removes from town, preserves diary and history"
              >
                Remove
              </button>
            )}
            {showReactivate && (
              <button
                onClick={() => onReactivate!(agent.agent_id, agent.display_name)}
                className="font-body text-xs text-green-400/70 hover:text-green-300 active:text-green-300 active:bg-clay-700/40 transition-colors px-2 py-2 -my-1 rounded"
                title="Bring this agent back into town (uses a quota slot)"
              >
                Reactivate
              </button>
            )}
          </div>
        );
      })()}
    </div>
  );
}
