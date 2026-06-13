/**
 * Unit tests for ProvidersPanel component.
 *
 * Mocks ``useProviders`` so tests are pure rendering tests — no network. The
 * category-filter test asserts the hook is re-invoked with the chosen slug
 * (the panel drives the query through the hook's ``category`` argument).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

vi.mock('../hooks/useProviders', () => ({
  useProviders: vi.fn(),
}));

import ProvidersPanel from './ProvidersPanel';
import { useProviders } from '../hooks/useProviders';
import type { Provider } from '../hooks/useProviders';

const mockedUse = useProviders as unknown as ReturnType<typeof vi.fn>;

function makeProvider(overrides: Partial<Provider> = {}): Provider {
  return {
    agent_id: 'p1',
    name: 'Bartleby',
    bio: 'I write haiku and proofread the town gazette.',
    skills: ['haiku', 'proofreading'],
    trust_tier: 'newcomer',
    trust_score: 0.5,
    jobs_completed: 3,
    ...overrides,
  };
}

beforeEach(() => {
  mockedUse.mockReset();
  mockedUse.mockReturnValue({ providers: [], loading: false, error: null });
});

describe('ProvidersPanel', () => {
  it('shows loading state initially', () => {
    mockedUse.mockReturnValue({ providers: [], loading: true, error: null });
    render(<ProvidersPanel />);
    expect(screen.getByText(/asking around town/i)).toBeInTheDocument();
  });

  it('shows error state', () => {
    mockedUse.mockReturnValue({ providers: [], loading: false, error: 'HTTP 503' });
    render(<ProvidersPanel />);
    expect(screen.getByText(/directory error.*503/i)).toBeInTheDocument();
  });

  it('shows the empty state when there are no providers', () => {
    mockedUse.mockReturnValue({ providers: [], loading: false, error: null });
    render(<ProvidersPanel />);
    expect(
      screen.getByText(/no providers yet — be the first to offer a service/i),
    ).toBeInTheDocument();
  });

  it('renders a provider shingle: name, trust stamp, skill chips, bio, jobs done', () => {
    mockedUse.mockReturnValue({ providers: [makeProvider()], loading: false, error: null });
    render(<ProvidersPanel />);
    expect(screen.getByText('Bartleby')).toBeInTheDocument();
    // Trust stamp chip reuses the passbook treatment.
    const stamp = screen.getByTestId('trust-stamp');
    expect(stamp.textContent).toBe('newcomer');
    expect(stamp.className).toContain('font-display');
    expect(stamp.className).toContain('border-clay-500');
    // Skill chips render, one per skill.
    const chips = screen.getAllByTestId('skill-chip');
    expect(chips.map((c) => c.textContent)).toEqual(['haiku', 'proofreading']);
    expect(screen.getByText(/I write haiku/)).toBeInTheDocument();
    expect(screen.getByText(/3 jobs done/)).toBeInTheDocument();
  });

  it('singularises the jobs-done count at one', () => {
    mockedUse.mockReturnValue({
      providers: [makeProvider({ jobs_completed: 1 })],
      loading: false,
      error: null,
    });
    render(<ProvidersPanel />);
    expect(screen.getByText(/1 job done/)).toBeInTheDocument();
  });

  it('renders the "trusted" / "reliable" trust stamps verbatim', () => {
    mockedUse.mockReturnValue({
      providers: [
        makeProvider({ agent_id: 'p1', name: 'Vera', trust_tier: 'trusted' }),
        makeProvider({ agent_id: 'p2', name: 'Quill', trust_tier: 'reliable' }),
      ],
      loading: false,
      error: null,
    });
    render(<ProvidersPanel />);
    const stamps = screen.getAllByTestId('trust-stamp').map((s) => s.textContent);
    expect(stamps).toEqual(['trusted', 'reliable']);
  });

  it('defaults to "all" (no category filter) — hook called with null', () => {
    render(<ProvidersPanel />);
    expect(mockedUse).toHaveBeenLastCalledWith(null);
    const allChip = screen.getByRole('button', { name: 'all' });
    expect(allChip.getAttribute('aria-pressed')).toBe('true');
  });

  it('clicking a category chip re-queries the directory with that slug', () => {
    render(<ProvidersPanel />);
    const writingChip = screen.getByRole('button', { name: 'writing' });
    fireEvent.click(writingChip);
    expect(mockedUse).toHaveBeenLastCalledWith('writing');
    expect(writingChip.getAttribute('aria-pressed')).toBe('true');
  });

  it('clicking "all" after a category clears the filter back to null', () => {
    render(<ProvidersPanel />);
    fireEvent.click(screen.getByRole('button', { name: 'research' }));
    expect(mockedUse).toHaveBeenLastCalledWith('research');
    fireEvent.click(screen.getByRole('button', { name: 'all' }));
    expect(mockedUse).toHaveBeenLastCalledWith(null);
  });

  it('collapses and re-expands via the header button', () => {
    mockedUse.mockReturnValue({ providers: [makeProvider()], loading: false, error: null });
    render(<ProvidersPanel />);
    const header = screen.getByRole('button', { name: /^providers/i });
    expect(screen.getByText('Bartleby')).toBeInTheDocument();
    fireEvent.click(header);
    expect(screen.queryByText('Bartleby')).toBeNull();
    fireEvent.click(header);
    expect(screen.getByText('Bartleby')).toBeInTheDocument();
  });
});
