/**
 * Unit tests for ExchangeBoard component.
 *
 * Mocks ``useExchangeBoard`` so tests are pure rendering tests — no network.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

vi.mock('../hooks/useExchangeBoard', () => ({
  useExchangeBoard: vi.fn(),
}));

import ExchangeBoard, { clockRemainingFraction, stampFor } from './ExchangeBoard';
import { useExchangeBoard } from '../hooks/useExchangeBoard';
import type { ExchangeJob } from '../hooks/useExchangeBoard';

const mockedUse = useExchangeBoard as unknown as ReturnType<typeof vi.fn>;

function makeJob(overrides: Partial<ExchangeJob> = {}): ExchangeJob {
  return {
    job_id: 'j1',
    title: 'Paint the plaza fence',
    description: 'A fresh coat of red on the plaza fence before the festival starts this weekend, please.',
    max_price: 40,
    agreed_price: 0,
    status: 'OPEN',
    consumer_name: 'Marge',
    provider_name: '',
    created_at: '2026-06-12T10:00:00Z',
    bid_count: 3,
    ...overrides,
  };
}

beforeEach(() => {
  mockedUse.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('ExchangeBoard', () => {
  it('shows loading state initially', () => {
    mockedUse.mockReturnValue({ jobs: [], loading: true, error: null });
    render(<ExchangeBoard />);
    expect(screen.getByText(/reading the board/i)).toBeInTheDocument();
  });

  it('shows error state', () => {
    mockedUse.mockReturnValue({ jobs: [], loading: false, error: 'HTTP 503' });
    render(<ExchangeBoard />);
    expect(screen.getByText(/board error.*503/i)).toBeInTheDocument();
  });

  it('shows the bare-board empty notice when there are no jobs', () => {
    mockedUse.mockReturnValue({ jobs: [], loading: false, error: null });
    render(<ExchangeBoard />);
    expect(screen.getByText(/nothing pinned — the board is bare/i)).toBeInTheDocument();
  });

  it('renders an OPEN job as a pinned notice: title, posted-by, price, sealed bids', () => {
    mockedUse.mockReturnValue({ jobs: [makeJob()], loading: false, error: null });
    render(<ExchangeBoard />);
    expect(screen.getByText('Pinned')).toBeInTheDocument();
    expect(screen.getByText('Paint the plaza fence')).toBeInTheDocument();
    expect(screen.getByText(/posted by Marge/)).toBeInTheDocument();
    // up to <CrumbAmount n=40 /> — amount + glyph, never a bare number
    expect(screen.getByText(/up to/)).toBeInTheDocument();
    expect(screen.getByText('40')).toBeInTheDocument();
    expect(screen.getByTestId('crumb-glyph')).toBeInTheDocument();
    expect(screen.getByText(/3 sealed bids/)).toBeInTheDocument();
  });

  it('truncates long descriptions to 100 chars with an ellipsis', () => {
    const longDesc = 'x'.repeat(150);
    mockedUse.mockReturnValue({
      jobs: [makeJob({ description: longDesc })],
      loading: false,
      error: null,
    });
    render(<ExchangeBoard />);
    expect(screen.getByText(`${'x'.repeat(100)}…`)).toBeInTheDocument();
  });

  it('hides the town clock when bid_window_ends_at is absent', () => {
    mockedUse.mockReturnValue({ jobs: [makeJob()], loading: false, error: null });
    render(<ExchangeBoard />);
    expect(screen.queryByTestId('town-clock')).toBeNull();
  });

  it('renders the town clock with width = remaining fraction of the bid window', () => {
    const now = new Date('2026-06-12T10:05:00Z').getTime();
    vi.useFakeTimers();
    vi.setSystemTime(now);
    mockedUse.mockReturnValue({
      jobs: [
        makeJob({
          created_at: '2026-06-12T10:00:00Z',
          // epoch SECONDS, 10 minutes after created — 5 of 10 min remain
          bid_window_ends_at: new Date('2026-06-12T10:10:00Z').getTime() / 1000,
        }),
      ],
      loading: false,
      error: null,
    });
    render(<ExchangeBoard />);
    const fill = screen.getByTestId('town-clock-fill');
    expect(fill.style.width).toBe('50%');
  });

  it('stamps settled notices: PAID in ink, FAILED/EXPIRED in wax, VOID for cancelled', () => {
    mockedUse.mockReturnValue({
      jobs: [
        makeJob({ job_id: 'c1', status: 'COMPLETED', agreed_price: 25, title: 'Done job' }),
        makeJob({ job_id: 'f1', status: 'FAILED', title: 'Failed job' }),
        makeJob({ job_id: 'e1', status: 'EXPIRED', title: 'Expired job' }),
        makeJob({ job_id: 'x1', status: 'CANCELLED', title: 'Cancelled job' }),
      ],
      loading: false,
      error: null,
    });
    render(<ExchangeBoard />);
    expect(screen.getByText('Settled')).toBeInTheDocument();
    const paid = screen.getByText('PAID');
    expect(paid.className).toContain('text-brown-700');
    expect(paid.className).toContain('-rotate-6');
    const failed = screen.getByText('FAILED');
    expect(failed.className).toContain('text-wax');
    expect(screen.getByText('EXPIRED').className).toContain('text-wax');
    expect(screen.getByText('VOID').className).toContain('text-brown-700');
    // Settled price renders through CrumbAmount.
    expect(screen.getByText('25')).toBeInTheDocument();
  });

  it('collapses and re-expands via the header button', () => {
    mockedUse.mockReturnValue({ jobs: [makeJob()], loading: false, error: null });
    render(<ExchangeBoard />);
    const header = screen.getByRole('button', { name: /exchange board/i });
    expect(screen.getByText('Paint the plaza fence')).toBeInTheDocument();
    fireEvent.click(header);
    expect(screen.queryByText('Paint the plaza fence')).toBeNull();
    fireEvent.click(header);
    expect(screen.getByText('Paint the plaza fence')).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// clockRemainingFraction unit tests — town-clock width math
// ---------------------------------------------------------------------------

describe('clockRemainingFraction', () => {
  const start = new Date('2026-06-12T10:00:00Z').getTime();
  const end = new Date('2026-06-12T10:10:00Z').getTime();

  it('is 1 at window open and 0 at window close', () => {
    expect(clockRemainingFraction(start, end, start)).toBe(1);
    expect(clockRemainingFraction(start, end, end)).toBe(0);
  });

  it('is 0.5 halfway through the window', () => {
    expect(clockRemainingFraction(start, end, (start + end) / 2)).toBe(0.5);
  });

  it('clamps past the close to 0 and before the open to 1', () => {
    expect(clockRemainingFraction(start, end, end + 60_000)).toBe(0);
    expect(clockRemainingFraction(start, end, start - 60_000)).toBe(1);
  });

  it('accepts ISO strings and epoch seconds interchangeably', () => {
    expect(
      clockRemainingFraction('2026-06-12T10:00:00Z', end / 1000, (start + end) / 2),
    ).toBe(0.5);
  });

  it('returns null for missing or degenerate windows', () => {
    expect(clockRemainingFraction(start, undefined)).toBeNull();
    expect(clockRemainingFraction(undefined, end)).toBeNull();
    expect(clockRemainingFraction(start, start)).toBeNull();
    expect(clockRemainingFraction(start, 'not-a-date')).toBeNull();
  });
});

describe('stampFor', () => {
  it('maps statuses to stamp labels and tones', () => {
    expect(stampFor('COMPLETED')).toEqual({ label: 'PAID', tone: 'ink' });
    expect(stampFor('FAILED')).toEqual({ label: 'FAILED', tone: 'wax' });
    expect(stampFor('EXPIRED')).toEqual({ label: 'EXPIRED', tone: 'wax' });
    expect(stampFor('CANCELLED')).toEqual({ label: 'VOID', tone: 'ink' });
  });
});
