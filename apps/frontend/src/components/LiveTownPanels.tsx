import ConversationsLog from './ConversationsLog';
import { WorldFeed } from './WorldFeed';
import { useTownGame } from './TownProvider';
import { townAgentCount } from '../lib/townActivity';

/** Do not present historical stories as current activity when town is empty. */
export function LiveTownPanel({ panel }: { panel: 'world' | 'conversations' }) {
  const { game } = useTownGame();

  if (!game) return panel === 'world' ? <p className="font-body text-clay-300 text-sm p-2">Checking town activity…</p> : null;
  if (townAgentCount(game) === 0) {
    return panel === 'world' ? <p className="font-body text-clay-300 text-sm p-2">The town is quiet right now.</p> : null;
  }
  return panel === 'world' ? <WorldFeed /> : <ConversationsLog />;
}
