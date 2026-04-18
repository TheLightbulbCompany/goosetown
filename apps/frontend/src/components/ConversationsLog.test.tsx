import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { Conversation } from '../hooks/useConversations';

// Mock useConversations before importing the component so the import-time
// hook reference is the mocked one.
vi.mock('../hooks/useConversations', () => ({
  useConversations: vi.fn(),
}));

import ConversationsLog from './ConversationsLog';
import { useConversations } from '../hooks/useConversations';

const mockedUse = useConversations as unknown as ReturnType<typeof vi.fn>;

function makeConv(overrides: Partial<Conversation> = {}): Conversation {
  return {
    id: 'c1',
    participant_a: 'Rachel',
    participant_b: 'Ross',
    location: 'cafe',
    started_at: '2026-04-17T00:00:00Z',
    ended_at: '2026-04-17T00:05:00Z',
    turn_count: 3,
    topic_summary: null,
    public_log: [
      { speaker: 'Rachel', text: 'hi' },
      { speaker: 'Ross', text: 'hello' },
      { speaker: 'Rachel', text: 'how are you' },
    ],
    ...overrides,
  };
}

beforeEach(() => {
  mockedUse.mockReset();
});

describe('ConversationsLog', () => {
  it('shows "Loading…" while the first fetch is in flight', () => {
    mockedUse.mockReturnValue({ conversations: [], loading: true, error: null });
    render(<ConversationsLog />);
    expect(screen.getByText(/loading/i)).toBeInTheDocument();
  });

  it('shows empty-state copy when there are no conversations', () => {
    mockedUse.mockReturnValue({ conversations: [], loading: false, error: null });
    render(<ConversationsLog />);
    expect(screen.getByText(/no chats yet/i)).toBeInTheDocument();
  });

  it('shows error copy when the hook errors', () => {
    mockedUse.mockReturnValue({
      conversations: [],
      loading: false,
      error: 'HTTP 500',
    });
    render(<ConversationsLog />);
    expect(screen.getByText(/failed to load/i)).toBeInTheDocument();
  });

  it('renders a row per conversation with participants and location', () => {
    mockedUse.mockReturnValue({
      conversations: [
        makeConv({ id: 'c1', participant_a: 'Rachel', participant_b: 'Ross' }),
        makeConv({ id: 'c2', participant_a: 'Joey', participant_b: 'Phoebe' }),
      ],
      loading: false,
      error: null,
    });
    render(<ConversationsLog />);
    expect(screen.getByText(/Rachel & Ross/i)).toBeInTheDocument();
    expect(screen.getByText(/Joey & Phoebe/i)).toBeInTheDocument();
  });

  it('opens a modal with the full public_log when a row is clicked', async () => {
    mockedUse.mockReturnValue({
      conversations: [makeConv()],
      loading: false,
      error: null,
    });
    render(<ConversationsLog />);
    const row = screen.getByRole('button', { name: /Rachel & Ross/i });
    await userEvent.click(row);

    // Modal shows participants header (header appears in both row and modal
    // after open, so at least 2 matches).
    const headers = screen.getAllByText(/Rachel & Ross/i);
    expect(headers.length).toBeGreaterThanOrEqual(2);

    // "how are you" is the last turn — visible in both the row preview AND
    // the modal. 2+ matches confirms the modal rendered it.
    expect(screen.getAllByText(/how are you/).length).toBeGreaterThanOrEqual(2);
    // "hello" and "hi" only exist in the modal (row shows only last turn).
    expect(screen.getByText(/hello/)).toBeInTheDocument();
    expect(screen.getByText(/^hi$/)).toBeInTheDocument();

    // Close button present
    expect(
      screen.getByRole('button', { name: /close/i }),
    ).toBeInTheDocument();
  });

  it('closes the modal when Close is clicked', async () => {
    mockedUse.mockReturnValue({
      conversations: [makeConv()],
      loading: false,
      error: null,
    });
    render(<ConversationsLog />);
    await userEvent.click(
      screen.getByRole('button', { name: /Rachel & Ross/i }),
    );
    // Modal open — "hello" only appears inside the modal body.
    expect(screen.getByText(/hello/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /close/i }));

    // After close, "hello" (modal-only) is gone; the row preview's "how are
    // you" remains. Assert both: no more Close button, no more modal body.
    expect(screen.queryByRole('button', { name: /close/i })).toBeNull();
    expect(screen.queryByText(/hello/)).toBeNull();
  });

  it('shows a live badge for active conversations (ended_at=null)', () => {
    mockedUse.mockReturnValue({
      conversations: [makeConv({ ended_at: null })],
      loading: false,
      error: null,
    });
    render(<ConversationsLog />);
    expect(screen.getByText(/live/i)).toBeInTheDocument();
  });

  it('renders empty-message placeholder inside modal when conversation has no turns', async () => {
    mockedUse.mockReturnValue({
      conversations: [makeConv({ public_log: [], turn_count: 0 })],
      loading: false,
      error: null,
    });
    render(<ConversationsLog />);
    await userEvent.click(
      screen.getByRole('button', { name: /Rachel & Ross/i }),
    );
    expect(screen.getByText(/no messages yet/i)).toBeInTheDocument();
  });
});
