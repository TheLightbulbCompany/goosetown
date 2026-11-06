import type { TownGameState } from '../types/town';

/** REST agents are already town-only; WS snapshots also include interiors. */
export function townAgentCount(game: TownGameState | undefined): number | undefined {
  return game?.world.agents.filter(agent => (agent.context ?? 'town') === 'town').length;
}
