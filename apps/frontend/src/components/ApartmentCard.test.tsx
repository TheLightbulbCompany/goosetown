import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

// ApartmentCard now calls usePassbook → useAuth; mock Clerk so it doesn't
// throw outside a ClerkProvider (same pattern as OwnerChatPanel.test.tsx).
vi.mock('@clerk/clerk-react', () => ({
  useAuth: () => ({ getToken: vi.fn().mockResolvedValue('test-token') }),
}));

import ApartmentCard from './ApartmentCard';
import type { ApartmentAgent } from '../hooks/useApartment';

function makeAgent(overrides: Partial<ApartmentAgent> = {}): ApartmentAgent {
  return {
    agent_id: 'agent-1',
    agent_name: 'lucky',
    display_name: 'Lucky',
    character: null,
    location_context: 'town',
    current_location: 'plaza',
    current_activity: 'idle',
    mood: 'happy',
    energy: 42,
    status_message: null,
    position_x: 0,
    position_y: 0,
    speed: 0,
    facing_x: 0,
    facing_y: 1,
    current_spot: 'plaza',
    is_active: true,
    sprite_url: 'https://cdn.example/sprites/lucky/walk.png?v=1',
    sprite_ready: true,
    created_at: null,
    currentGoal: null,
    ...overrides,
  };
}

function renderCard(agent: ApartmentAgent) {
  return render(
    <MemoryRouter>
      <ApartmentCard agent={agent} />
    </MemoryRouter>,
  );
}

describe('ApartmentCard', () => {
  it('keeps the placeholder usable when avatar generation is queued', () => {
    renderCard(makeAgent({ sprite_url: null, sprite_ready: false, avatar_state: 'queued' }));
    expect(screen.getByText(/Avatar coming later/)).toBeInTheDocument();
    expect(screen.queryByText(/Drawing your agent/)).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Lucky' })).toBeInTheDocument();
  });

  it('renders sprite <img> when sprite_url is set', () => {
    renderCard(makeAgent());
    const img = screen.getByRole('img', { name: /lucky/i });
    expect(img).toHaveAttribute(
      'src',
      'https://cdn.example/sprites/lucky/walk.png?v=1',
    );
  });

  it('renders "?" fallback when sprite_url is empty', () => {
    const { container } = renderCard(makeAgent({ sprite_url: '' }));
    expect(screen.queryByRole('img', { name: /lucky/i })).toBeNull();
    // The fallback literal "?" lives in the sprite slot.
    expect(container.textContent).toContain('?');
  });

  it('renders "?" fallback when sprite_url is null', () => {
    const { container } = renderCard(
      makeAgent({ sprite_url: null as unknown as string }),
    );
    expect(screen.queryByRole('img', { name: /lucky/i })).toBeNull();
    expect(container.textContent).toContain('?');
  });

  it('shows "View in town" link when agent is in town context and no link for unknown context', () => {
    const { rerender } = render(
      <MemoryRouter>
        <ApartmentCard agent={makeAgent({ location_context: 'town' })} />
      </MemoryRouter>,
    );
    expect(screen.getByText(/view in town/i)).toBeInTheDocument();

    rerender(
      <MemoryRouter>
        <ApartmentCard agent={makeAgent({ location_context: 'unknown_scene' })} />
      </MemoryRouter>,
    );
    expect(screen.queryByText(/view in/i)).toBeNull();
  });

  it('view-in-town link URL-encodes the display name into ?focus=', () => {
    renderCard(makeAgent({ display_name: 'Rachel Green' }));
    const link = screen.getByRole('link', { name: /view in town/i });
    expect(link).toHaveAttribute('href', '/?focus=Rachel%20Green');
  });

  it('renders mood label, energy percent, and current location', () => {
    renderCard(
      makeAgent({ mood: 'happy', energy: 42, current_location: 'library' }),
    );
    expect(screen.getByText(/happy/i)).toBeInTheDocument();
    expect(screen.getByText(/42%/)).toBeInTheDocument();
    expect(screen.getByText(/library/i)).toBeInTheDocument();
  });

  it('adds inactive styling class when is_active is false', () => {
    const { container } = renderCard(makeAgent({ is_active: false }));
    const card = container.firstElementChild as HTMLElement;
    expect(card.className).toContain('opacity-50');
  });

  it('renders status message when provided', () => {
    renderCard(makeAgent({ status_message: 'thinking about coffee' }));
    expect(screen.getByText(/thinking about coffee/)).toBeInTheDocument();
  });

  it('does not render status message block when null', () => {
    renderCard(makeAgent({ status_message: null }));
    // A status-message render would have italic styled quotes; absence is fine.
    expect(screen.queryByText(/thinking about/)).toBeNull();
  });

  it('renders "View in library →" link when location_context is library', () => {
    renderCard(makeAgent({ location_context: 'library', current_location: 'reading_armchair' }));
    const link = screen.getByText(/View in library/i);
    expect(link).toBeInTheDocument();
    expect(link.closest('a')?.getAttribute('href')).toBe('/library');
  });

  it('renders "View in town →" link with ?focus= when location_context is town', () => {
    renderCard(makeAgent({ location_context: 'town', display_name: 'Lucky' }));
    const link = screen.getByText(/View in town/i);
    expect(link.closest('a')?.getAttribute('href')).toBe('/?focus=Lucky');
  });

  it('renders "View in apartment →" link when location_context is apartment', () => {
    renderCard(makeAgent({ location_context: 'apartment', current_location: 'bed' }));
    const link = screen.getByText(/View in apartment/i);
    expect(link.closest('a')?.getAttribute('href')).toBe('/apartment');
  });

  it('renders a Chat button when onChatClick is provided', () => {
    const onChatClick = vi.fn();
    const agent = makeAgent({ agent_id: 'agent-1', display_name: 'Dwight' });
    render(
      <MemoryRouter>
        <ApartmentCard agent={agent} onChatClick={onChatClick} />
      </MemoryRouter>,
    );
    const btn = screen.getByText(/^Chat/i);
    fireEvent.click(btn);
    expect(onChatClick).toHaveBeenCalledWith('agent-1', 'Dwight');
  });

  it('renders currentGoal when present', () => {
    renderCard(makeAgent({ currentGoal: 'make a friend' }));
    expect(screen.getByText(/make a friend/)).toBeInTheDocument();
    expect(screen.getByText(/Goal:/)).toBeInTheDocument();
  });

  it('does not render goal section when currentGoal is null', () => {
    renderCard(makeAgent({ currentGoal: null }));
    expect(screen.queryByText(/Goal:/)).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Passbook + CrumbAmount integration
// ---------------------------------------------------------------------------

describe('ApartmentCard passbook', () => {
  const LEDGER = {
    balance: 18,
    trust_tier: 'fledgling',
    trust_score: 0.42,
    entries: [
      {
        type: 'job_payout',
        amount: 8,
        balance_after: 18,
        ref_id: 'r1',
        at: '2026-06-10T09:30:00Z',
      },
      {
        type: 'job_escrow',
        amount: -5,
        balance_after: 10,
        ref_id: 'r2',
        at: '2026-06-09T15:00:00Z',
      },
    ],
  };

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('does not render the Passbook section without a wallet', () => {
    renderCard(makeAgent()); // wallet_balance undefined
    expect(screen.queryByText(/Passbook/)).toBeNull();
  });

  it('renders the crumbs row via CrumbAmount (glyph, no bare coin emoji)', () => {
    const { container } = renderCard(
      makeAgent({ wallet_balance: 12, trust_tier: 'fledgling' }),
    );
    expect(screen.getByText('12')).toBeInTheDocument();
    expect(screen.getByTestId('crumb-glyph')).toBeInTheDocument();
    expect(container.textContent).not.toContain('🪙');
    // Tier still displayed alongside the balance.
    expect(screen.getByText('(fledgling)')).toBeInTheDocument();
  });

  it('lazy-fetches the ledger on first expand and renders entries with +/− coloring', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve(LEDGER),
    } as unknown as Response);
    vi.stubGlobal('fetch', fetchMock);

    renderCard(makeAgent({ wallet_balance: 18, trust_tier: 'fledgling' }));
    // Lazy: nothing fetched until the owner opens the passbook.
    expect(fetchMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByText(/Passbook/));
    expect(await screen.findByText('job_payout')).toBeInTheDocument();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toContain('/town/agent/agent-1/ledger');
    expect((fetchMock.mock.calls[0][1] as RequestInit).headers).toMatchObject({
      Authorization: 'Bearer test-token',
    });

    // Credit in gold, debit in clay.
    expect(screen.getByText('+8').parentElement?.className).toContain('text-gold-300');
    expect(screen.getByText('−5').parentElement?.className).toContain('text-clay-300');
    // Trust stamp chip shows the tier from the ledger payload.
    expect(screen.getByText('fledgling')).toBeInTheDocument();
    // Second entry's running balance renders too.
    expect(screen.getByText('job_escrow')).toBeInTheDocument();
  });

  it('does not refetch on re-expand after a successful fetch', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve(LEDGER),
    } as unknown as Response);
    vi.stubGlobal('fetch', fetchMock);

    renderCard(makeAgent({ wallet_balance: 18 }));
    const toggle = screen.getByText(/Passbook/);
    fireEvent.click(toggle);
    expect(await screen.findByText('job_payout')).toBeInTheDocument();
    fireEvent.click(toggle); // collapse
    expect(screen.queryByText('job_payout')).toBeNull();
    fireEvent.click(toggle); // re-expand — cached, no second fetch
    expect(await screen.findByText('job_payout')).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('retries the fetch on re-expand after a failed fetch', async () => {
    const failResponse = {
      ok: false,
      status: 500,
      json: () => Promise.resolve({}),
    } as unknown as Response;
    const successResponse = {
      ok: true,
      status: 200,
      json: () => Promise.resolve(LEDGER),
    } as unknown as Response;
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(failResponse)
      .mockResolvedValueOnce(successResponse);
    vi.stubGlobal('fetch', fetchMock);

    renderCard(makeAgent({ wallet_balance: 18 }));
    const toggle = screen.getByText(/Passbook/);
    fireEvent.click(toggle); // first expand → fetch fails
    expect(await screen.findByText(/Passbook error/)).toBeInTheDocument();
    fireEvent.click(toggle); // collapse
    fireEvent.click(toggle); // re-expand → startedRef reset, retries
    expect(await screen.findByText('job_payout')).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('shows a passbook error state when the ledger fetch fails', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 403,
      json: () => Promise.resolve({}),
    } as unknown as Response);
    vi.stubGlobal('fetch', fetchMock);

    renderCard(makeAgent({ wallet_balance: 18 }));
    fireEvent.click(screen.getByText(/Passbook/));
    expect(await screen.findByText(/Passbook error/)).toBeInTheDocument();
  });
});
