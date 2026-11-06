import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { LiveTownPanel } from './LiveTownPanels';
import { useTownGame } from './TownProvider';

vi.mock('./TownProvider', () => ({ useTownGame: vi.fn() }));
vi.mock('./WorldFeed', () => ({ WorldFeed: () => <div>World feed fixture</div> }));
vi.mock('./ConversationsLog', () => ({ default: () => <div>Conversations fixture</div> }));

const mockTown = vi.mocked(useTownGame);
const renderPanels = () => render(<><LiveTownPanel panel="world" /><LiveTownPanel panel="conversations" /></>);

describe('LiveTownPanel', () => {
  beforeEach(() => { vi.resetAllMocks(); });
  it('shows a neutral loading state before town data arrives', () => {
    mockTown.mockReturnValue({ game: undefined, economyEvents: [] });
    renderPanels();
    expect(screen.getByText('Checking town activity…')).toBeInTheDocument();
    expect(screen.queryByText('The town is quiet right now.')).not.toBeInTheDocument();
    expect(screen.queryByText('World feed fixture')).not.toBeInTheDocument();
    expect(screen.queryByText('Conversations fixture')).not.toBeInTheDocument();
  });
  it('hides both panels and describes a confirmed empty town', () => {
    mockTown.mockReturnValue({ game: { world: { agents: [] } } as never, economyEvents: [] });
    renderPanels();
    expect(screen.getByText('The town is quiet right now.')).toBeInTheDocument();
    expect(screen.queryByText('World feed fixture')).not.toBeInTheDocument();
    expect(screen.queryByText('Conversations fixture')).not.toBeInTheDocument();
  });
  it('keeps both existing panels when an agent is present', () => {
    mockTown.mockReturnValue({ game: { world: { agents: [{ id: 'synthetic' }] } } as never, economyEvents: [] });
    renderPanels();
    expect(screen.getByText('World feed fixture')).toBeInTheDocument();
    expect(screen.getByText('Conversations fixture')).toBeInTheDocument();
  });
});
