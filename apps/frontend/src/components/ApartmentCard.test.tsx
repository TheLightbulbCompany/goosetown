import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
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

  it('shows "View in town" link only when agent is in town context', () => {
    const { rerender } = render(
      <MemoryRouter>
        <ApartmentCard agent={makeAgent({ location_context: 'town' })} />
      </MemoryRouter>,
    );
    expect(screen.getByText(/view in town/i)).toBeInTheDocument();

    rerender(
      <MemoryRouter>
        <ApartmentCard agent={makeAgent({ location_context: 'apartment' })} />
      </MemoryRouter>,
    );
    expect(screen.queryByText(/view in town/i)).toBeNull();
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
});
