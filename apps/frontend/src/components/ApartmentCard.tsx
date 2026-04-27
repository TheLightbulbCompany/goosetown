import { Link } from 'react-router-dom';
import clsx from 'clsx';
import type { ApartmentAgent } from '../hooks/useApartment';

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

export default function ApartmentCard({ agent }: { agent: ApartmentAgent }) {
  const energyPercent = Math.max(0, Math.min(100, agent.energy));

  return (
    <div
      className={clsx(
        'box bg-brown-800 p-4 flex flex-col gap-3',
        !agent.is_active && 'opacity-50',
      )}
    >
      {/* Header: character sprite + name */}
      <div className="flex items-center gap-3">
        <div className="w-12 h-12 bg-clay-700 rounded flex items-center justify-center text-brown-200 font-display text-lg shrink-0 overflow-hidden">
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
        </div>
        <div className="flex-1 min-w-0">
          <h3 className="font-display text-lg text-brown-100 truncate tracking-wider">
            {agent.display_name}
          </h3>
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

      {/* Location */}
      {agent.current_location && (
        <div className="font-body text-xs text-clay-300">
          Location: <span className="text-brown-200">
            {agent.location_context === 'town' ? 'Town - ' : ''}{agent.current_location}
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

      {/* Mood + Energy */}
      <div className="flex items-center gap-4">
        <div className="font-body text-xs text-clay-300">
          Mood: <span className="text-brown-200">{moodEmoji(agent.mood)} {agent.mood || 'neutral'}</span>
        </div>
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

      {/* Action: view in town */}
      {agent.location_context === 'town' && (
        <div className="flex justify-end mt-1">
          <Link
            to={`/?focus=${encodeURIComponent(agent.display_name)}`}
            className="font-body text-xs text-clay-300 hover:text-brown-200 transition-colors"
          >
            View in town &rarr;
          </Link>
        </div>
      )}
    </div>
  );
}
