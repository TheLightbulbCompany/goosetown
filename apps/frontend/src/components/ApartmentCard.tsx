import { Link } from 'react-router-dom';
import clsx from 'clsx';
import type { ApartmentAgent } from '../hooks/useApartment';

function statusLabel(activity: string | null): string {
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

function statusColor(activity: string | null): string {
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
      {/* Header: character sprite placeholder + name */}
      <div className="flex items-center gap-3">
        <div className="w-12 h-12 bg-clay-700 rounded flex items-center justify-center text-brown-200 font-display text-lg shrink-0">
          {agent.character ? agent.character.toUpperCase() : '?'}
        </div>
        <div className="flex-1 min-w-0">
          <h3 className="font-display text-lg text-brown-100 truncate tracking-wider">
            {agent.display_name}
          </h3>
          <p className="font-body text-xs text-clay-300 truncate">{agent.agent_name}</p>
        </div>
        {/* Status indicator */}
        <div className="flex items-center gap-1.5">
          <span className={clsx('w-2 h-2 rounded-full', statusColor(agent.current_activity))} />
          <span className="font-body text-xs text-clay-100">
            {statusLabel(agent.current_activity)}
          </span>
        </div>
      </div>

      {/* Location */}
      {agent.current_location && (
        <div className="font-body text-xs text-clay-300">
          Location: <span className="text-brown-200">
            {agent.location_context === 'town' ? 'Town - ' : ''}{agent.current_location}
          </span>
          {agent.current_spot && (
            <span className="text-clay-400 ml-1">({agent.current_spot})</span>
          )}
        </div>
      )}

      {/* Status message */}
      {agent.status_message && (
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
