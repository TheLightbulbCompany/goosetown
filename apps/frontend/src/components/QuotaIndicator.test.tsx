import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';

// Mock useQuota before importing the component so the import-time hook
// reference is the mocked one.
vi.mock('../hooks/useQuota', () => ({
  useQuota: vi.fn(),
}));

import { QuotaIndicator } from './QuotaIndicator';
import { useQuota } from '../hooks/useQuota';

const mockedUseQuota = useQuota as ReturnType<typeof vi.fn>;

beforeEach(() => {
  mockedUseQuota.mockReset();
});

describe('QuotaIndicator', () => {
  it('renders nothing when data is null (still loading)', () => {
    mockedUseQuota.mockReturnValue(null);
    const { container } = render(<QuotaIndicator />);
    expect(container.firstChild).toBeNull();
  });

  it('shows agents and sprite-gen counts when data is available', () => {
    mockedUseQuota.mockReturnValue({
      agents: { used: 3, limit: 5 },
      sprite_gen: { used_24h: 4, limit: 10 },
    });
    render(<QuotaIndicator />);
    expect(screen.getByText(/3.*\/.*5.*agents/i)).toBeInTheDocument();
    expect(screen.getByText(/4.*\/.*10.*sprite/i)).toBeInTheDocument();
  });

  it('highlights agents in red when at limit', () => {
    mockedUseQuota.mockReturnValue({
      agents: { used: 5, limit: 5 },
      sprite_gen: { used_24h: 4, limit: 10 },
    });
    render(<QuotaIndicator />);
    const agents = screen.getByText(/5.*\/.*5.*agents/i);
    expect(agents.className).toMatch(/red/);
  });

  it('highlights sprite-gen in red when at limit', () => {
    mockedUseQuota.mockReturnValue({
      agents: { used: 3, limit: 5 },
      sprite_gen: { used_24h: 10, limit: 10 },
    });
    render(<QuotaIndicator />);
    const sprite = screen.getByText(/10.*\/.*10.*sprite/i);
    expect(sprite.className).toMatch(/red/);
  });
});
