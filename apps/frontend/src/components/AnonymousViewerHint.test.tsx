import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

const mockGame = vi.fn();
vi.mock('./TownProvider.tsx', () => ({ useTownGame: () => ({ game: mockGame() }) }));
import { AnonymousViewerHint } from './AnonymousViewerHint';

function renderWithAgents(count: number | undefined) {
  mockGame.mockReturnValue(count === undefined ? undefined : { world: { agents: Array.from({ length: count }) } });
  render(<MemoryRouter><AnonymousViewerHint /></MemoryRouter>);
}

describe('AnonymousViewerHint', () => {
  it('is neutral while state loads', () => {
    renderWithAgents(undefined);
    expect(screen.getByText(/checking town activity/i)).toBeInTheDocument();
    expect(screen.queryByText(/watching live/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/no agents in town/i)).not.toBeInTheDocument();
  });
  it('shows quiet copy and the sign-up link for an empty town', () => {
    renderWithAgents(0);
    expect(screen.getByText(/no agents in town right now/i)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /bring your own agent/i })).toHaveAttribute('href', '/sign-up');
    expect(screen.queryByText(/watching live/i)).not.toBeInTheDocument();
  });
  it('shows the current count when agents are present', () => {
    renderWithAgents(3);
    expect(screen.getByText(/watching live/i)).toBeInTheDocument();
    expect(screen.getByText(/3 agents are in town/i)).toBeInTheDocument();
  });
});
