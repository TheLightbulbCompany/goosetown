import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('./TownProvider', () => ({ useTownGame: vi.fn() }));

import ExchangeTicker, { formatTickerLine } from './ExchangeTicker';
import { useTownGame } from './TownProvider';
import type { EconomyTickerEntry } from '../lib/economyTicker';

const mockUse = useTownGame as unknown as ReturnType<typeof vi.fn>;
const e = (o: Partial<EconomyTickerEntry>): EconomyTickerEntry => ({
  id: 'x', type: 'posted', actorName: 'Carol', counterpartyName: null, amount: null, title: 'Haiku', ...o,
});

describe('formatTickerLine', () => {
  it('phrases each event type', () => {
    expect(formatTickerLine(e({ type: 'posted', actorName: 'Carol', amount: 30 })).text).toContain('Carol posted');
    expect(formatTickerLine(e({ type: 'awarded', actorName: 'Pat' })).text).toContain('Pat won');
    expect(formatTickerLine(e({ type: 'payout', actorName: 'Pat', amount: 18 })).text).toContain('Pat paid');
    expect(formatTickerLine(e({ type: 'payout', amount: 18 })).amount).toBe(18);
  });
});

describe('ExchangeTicker', () => {
  it('renders a line per economy event', () => {
    mockUse.mockReturnValue({ game: undefined, economyEvents: [
      e({ id: '1', type: 'payout', actorName: 'Pat', amount: 18, title: 'Haiku' }),
      e({ id: '2', type: 'posted', actorName: 'Carol', amount: 30, title: 'Mural' }),
    ]});
    render(<ExchangeTicker />);
    expect(screen.getByText(/Pat paid/)).toBeInTheDocument();
    expect(screen.getByText(/Carol posted/)).toBeInTheDocument();
  });

  it('shows an empty-state line when there are no events', () => {
    mockUse.mockReturnValue({ game: undefined, economyEvents: [] });
    render(<ExchangeTicker />);
    expect(screen.getByText(/quiet at the bank/i)).toBeInTheDocument();
  });
});
