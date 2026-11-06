import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

const mockGame = vi.fn();
vi.mock('./TownProvider.tsx', () => ({ useAgentCount: () => mockGame()?.world.agents.length ?? 0 }));

import { AnonymousViewerHint } from './AnonymousViewerHint';

function renderWithAgents(count: number | null) {
  mockGame.mockReturnValue(count == null ? undefined : { world: { agents: Array.from({ length: count }) } });
  render(
    <MemoryRouter>
      <AnonymousViewerHint />
    </MemoryRouter>,
  );
}

describe('AnonymousViewerHint', () => {
  it('does not claim live activity with 0 agents or before state loads', () => {
    renderWithAgents(0);
    expect(screen.queryByText(/watching live/i)).toBeNull();
    expect(screen.getByText(/no agents in town right now/i)).toBeInTheDocument();
  });

  it('says watching live with the agent count when agents are in town', () => {
    renderWithAgents(3);
    expect(screen.getByText(/watching live/i)).toBeInTheDocument();
    expect(screen.getByText(/3 agents are in town/i)).toBeInTheDocument();
  });
});
