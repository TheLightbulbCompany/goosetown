import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import OwnerChatPanel from './OwnerChatPanel';

// Mock Clerk so useAuth() doesn't throw outside a ClerkProvider.
vi.mock('@clerk/clerk-react', () => ({
  useAuth: () => ({ getToken: vi.fn().mockResolvedValue('test-token') }),
}));

vi.mock('../hooks/useOwnerChat', () => ({
  useOwnerChat: vi.fn(),
}));
import { useOwnerChat } from '../hooks/useOwnerChat';

const mockUseOwnerChat = useOwnerChat as ReturnType<typeof vi.fn>;

function renderPanel(props: { agentId: string; agentName: string; onClose: () => void }) {
  return render(
    <MemoryRouter>
      <OwnerChatPanel {...props} />
    </MemoryRouter>,
  );
}

describe('OwnerChatPanel', () => {
  it('renders messages from the hook', () => {
    mockUseOwnerChat.mockReturnValue({
      messages: [
        { id: 'm1', direction: 'owner_to_agent', text: 'hi', createdAt: 1 },
        { id: 'm2', direction: 'agent_to_owner', text: 'yo', createdAt: 2 },
      ],
      loading: false,
      pending: false,
      send: vi.fn(),
    });
    renderPanel({ agentId: 'a1', agentName: 'Dwight', onClose: vi.fn() });
    expect(screen.getByText('hi')).toBeInTheDocument();
    expect(screen.getByText('yo')).toBeInTheDocument();
  });

  it('shows "thinking" indicator when pending', () => {
    mockUseOwnerChat.mockReturnValue({
      messages: [],
      loading: false,
      pending: true,
      send: vi.fn(),
    });
    renderPanel({ agentId: 'a1', agentName: 'Dwight', onClose: vi.fn() });
    expect(screen.getByText(/dwight is thinking/i)).toBeInTheDocument();
  });

  it('calls send when user submits the input', () => {
    const send = vi.fn();
    mockUseOwnerChat.mockReturnValue({
      messages: [],
      loading: false,
      pending: false,
      send,
    });
    renderPanel({ agentId: 'a1', agentName: 'Dwight', onClose: vi.fn() });
    const input = screen.getByPlaceholderText(/message dwight/i);
    fireEvent.change(input, { target: { value: 'go to library' } });
    fireEvent.click(screen.getByRole('button', { name: /^send$/i }));
    expect(send).toHaveBeenCalledWith('go to library');
  });

  it('calls onClose when close button clicked', () => {
    const onClose = vi.fn();
    mockUseOwnerChat.mockReturnValue({
      messages: [], loading: false, pending: false, send: vi.fn(),
    });
    renderPanel({ agentId: 'a1', agentName: 'Dwight', onClose });
    fireEvent.click(screen.getByRole('button', { name: /close/i }));
    expect(onClose).toHaveBeenCalled();
  });
});
