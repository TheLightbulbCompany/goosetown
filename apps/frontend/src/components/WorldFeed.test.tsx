/**
 * Unit tests for WorldFeed component.
 *
 * Mocks ``useWorldFeed`` so tests are pure rendering tests — no network.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('../hooks/useWorldFeed', () => ({
  useWorldFeed: vi.fn(),
}));

import WorldFeed, { iconFor, formatRelative } from './WorldFeed';
import { useWorldFeed } from '../hooks/useWorldFeed';

const mockedUse = useWorldFeed as unknown as ReturnType<typeof vi.fn>;

function makeEvent(overrides: Partial<{
  eventId: string;
  timestamp: string;
  tag: string;
  summary: string | null;
  location: string | null;
}> = {}) {
  return {
    eventId: 'e1',
    timestamp: new Date(Date.now() - 5 * 60_000).toISOString(), // 5 min ago
    tag: 'weather.storm',
    summary: 'A storm rolls in',
    location: 'plaza',
    ...overrides,
  };
}

beforeEach(() => {
  mockedUse.mockReset();
});

describe('WorldFeed', () => {
  it('shows loading state initially', () => {
    mockedUse.mockReturnValue({ events: [], loading: true, error: null });
    render(<WorldFeed />);
    expect(screen.getByText(/loading world events/i)).toBeInTheDocument();
  });

  it('shows empty state when no events', () => {
    mockedUse.mockReturnValue({ events: [], loading: false, error: null });
    render(<WorldFeed />);
    expect(screen.getByText(/nothing has happened in the last 48 hours/i)).toBeInTheDocument();
  });

  it('renders events with summaries', () => {
    mockedUse.mockReturnValue({
      events: [
        makeEvent({ tag: 'weather.storm', summary: 'A storm rolls in' }),
        makeEvent({ eventId: 'e2', tag: 'ambient.cat', summary: 'cat at cafe', location: null }),
      ],
      loading: false,
      error: null,
    });
    render(<WorldFeed />);
    expect(screen.getByText('A storm rolls in')).toBeInTheDocument();
    expect(screen.getByText('cat at cafe')).toBeInTheDocument();
  });

  it('renders tag string as fallback when summary is null', () => {
    mockedUse.mockReturnValue({
      events: [makeEvent({ summary: null, tag: 'system.tick' })],
      loading: false,
      error: null,
    });
    render(<WorldFeed />);
    expect(screen.getByText('system.tick')).toBeInTheDocument();
  });

  it('renders location when present', () => {
    mockedUse.mockReturnValue({
      events: [makeEvent({ summary: 'thunder', location: 'plaza' })],
      loading: false,
      error: null,
    });
    render(<WorldFeed />);
    expect(screen.getByText(/plaza/)).toBeInTheDocument();
  });

  it('shows error message when fetch fails', () => {
    mockedUse.mockReturnValue({ events: [], loading: false, error: 'HTTP 503' });
    render(<WorldFeed />);
    expect(screen.getByText(/feed error.*503/i)).toBeInTheDocument();
  });

  it('shows "World" heading', () => {
    mockedUse.mockReturnValue({ events: [], loading: false, error: null });
    render(<WorldFeed />);
    expect(screen.getByText('World')).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// iconFor unit tests
// ---------------------------------------------------------------------------

describe('iconFor', () => {
  it('returns weather icon for weather.* tags', () => {
    expect(iconFor('weather.storm')).toBe('🌧');
    expect(iconFor('weather.clear')).toBe('🌧');
  });

  it('returns ambient icon for ambient.* tags', () => {
    expect(iconFor('ambient.cat')).toBe('💡');
  });

  it('returns arc icon for arc.* tags', () => {
    expect(iconFor('arc.chapter_open')).toBe('📜');
  });

  it('returns social icon for social.* tags', () => {
    expect(iconFor('social.festival')).toBe('📣');
  });

  it('returns system icon for system.* tags', () => {
    expect(iconFor('system.tick')).toBe('ℹ️');
  });

  it('returns bullet for unknown prefix', () => {
    expect(iconFor('unknown.foo')).toBe('•');
    expect(iconFor('')).toBe('•');
  });
});

// ---------------------------------------------------------------------------
// formatRelative unit tests
// ---------------------------------------------------------------------------

describe('formatRelative', () => {
  it('shows minutes for recent timestamps', () => {
    const ts = new Date(Date.now() - 3 * 60_000).toISOString();
    expect(formatRelative(ts)).toBe('3m ago');
  });

  it('shows at least 1m ago even for very recent timestamps', () => {
    const ts = new Date(Date.now() - 1000).toISOString();
    expect(formatRelative(ts)).toBe('1m ago');
  });

  it('shows hours for timestamps > 60 minutes', () => {
    const ts = new Date(Date.now() - 3 * 60 * 60_000).toISOString();
    expect(formatRelative(ts)).toBe('3h ago');
  });

  it('shows days for timestamps > 24 hours', () => {
    const ts = new Date(Date.now() - 2 * 24 * 60 * 60_000).toISOString();
    expect(formatRelative(ts)).toBe('2d ago');
  });
});
