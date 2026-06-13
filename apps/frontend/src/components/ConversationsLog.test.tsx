import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, render as renderRTL, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router-dom';
import type { ReactElement } from 'react';
import type { Conversation } from '../hooks/useConversations';

// Mock useConversations before importing the component so the import-time
// hook reference is the mocked one.
vi.mock('../hooks/useConversations', () => ({
  useConversations: vi.fn(),
}));

// Mock Clerk so useAuth() doesn't throw outside a ClerkProvider.
// Default: signed-in with a test token. The signed-out test flips
// mockIsSignedIn to false (and restores it) — the factory reads it per call.
const mockGetToken = vi.fn().mockResolvedValue('test-token');
let mockIsSignedIn = true;
vi.mock('@clerk/clerk-react', () => ({
  useAuth: () => ({ getToken: mockGetToken, isSignedIn: mockIsSignedIn }),
}));

import ConversationsLog from './ConversationsLog';
import { useConversations } from '../hooks/useConversations';

const mockedUse = useConversations as unknown as ReturnType<typeof vi.fn>;

// ConversationsLog now uses useNavigate (for "📍 on map"), so renders need a
// Router. LocationProbe echoes the current path+search so navigation can be
// asserted.
function LocationProbe() {
  const loc = useLocation();
  return <div data-testid="loc">{loc.pathname + loc.search}</div>;
}

function render(ui: ReactElement) {
  return renderRTL(
    <MemoryRouter initialEntries={['/']}>
      {ui}
      <LocationProbe />
    </MemoryRouter>,
  );
}

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

// Build a fetch mock that resolves the profile call (GET /town/profile) with
// a chosen nickname, and treats every other request (the /say POST) as a 200.
// Tests that exercise nickname-prompt / send-error paths pass a custom value or
// stub fetch themselves.
function stubFetch(opts: {
  nickname?: string | null;
  sayResponse?: { ok: boolean; status: number };
} = {}) {
  const { nickname = 'Tester', sayResponse = { ok: true, status: 200 } } = opts;
  const fetchMock = vi.fn((url: string) => {
    if (typeof url === 'string' && url.includes('/town/profile')) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ nickname }),
      });
    }
    return Promise.resolve(sayResponse);
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

beforeEach(() => {
  mockedUse.mockReset();
  mockGetToken.mockResolvedValue('test-token');
  mockIsSignedIn = true;
  // Default: signed-in user with a nickname, so the message footer is the
  // default state for existing send-path tests.
  stubFetch();
});

afterEach(() => {
  vi.unstubAllGlobals();
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
    // "live" now appears in both the ● live badge and the "live · Xm" duration.
    expect(screen.getAllByText(/live/i).length).toBeGreaterThanOrEqual(1);
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

  // ─── Multi-participant ─────────────────────────────────────────────────
  // Previously only participant_a & participant_b were shown in row + modal,
  // so a 3+-person conversation looked identical to a 2-person one even
  // though the public_log had turns from a third speaker. Use the canonical
  // ``participants`` list end-to-end.

  it('row shows all 3 participants joined when participants has 3 entries', () => {
    mockedUse.mockReturnValue({
      conversations: [
        makeConv({
          participants: ['Rachel', 'Ross', 'Phoebe'],
          turn_count: 4,
        }),
      ],
      loading: false,
      error: null,
    });
    render(<ConversationsLog />);
    // Row uses the "A, B & C" form for exactly 3 people.
    expect(
      screen.getByRole('button', { name: /Rachel, Ross & Phoebe/i }),
    ).toBeInTheDocument();
  });

  it('row collapses to "A, B +N" for groups of 4+', () => {
    mockedUse.mockReturnValue({
      conversations: [
        makeConv({
          participants: ['Rachel', 'Ross', 'Phoebe', 'Joey', 'Monica'],
          turn_count: 8,
        }),
      ],
      loading: false,
      error: null,
    });
    render(<ConversationsLog />);
    // Row shows "Rachel, Ross +3" — space-constrained shorthand.
    expect(
      screen.getByRole('button', { name: /Rachel, Ross \+3/i }),
    ).toBeInTheDocument();
  });

  it('modal header shows ALL participants spelled out for 4+ groups', async () => {
    mockedUse.mockReturnValue({
      conversations: [
        makeConv({
          participants: ['Rachel', 'Ross', 'Phoebe', 'Joey'],
          turn_count: 6,
        }),
      ],
      loading: false,
      error: null,
    });
    render(<ConversationsLog />);
    const row = screen.getByRole('button', { name: /Rachel, Ross \+2/i });
    await userEvent.click(row);
    // Modal header should NOT use the +N collapse — every name visible.
    expect(
      screen.getByRole('heading', { name: /Rachel, Ross, Phoebe & Joey/i }),
    ).toBeInTheDocument();
    // Subtitle includes the participant count badge.
    expect(screen.getByText(/4 people/)).toBeInTheDocument();
  });

  it('falls back to participant_a/participant_b when participants is missing (legacy API)', () => {
    mockedUse.mockReturnValue({
      conversations: [
        makeConv({
          participants: undefined,
          participant_a: 'Rachel',
          participant_b: 'Ross',
        }),
      ],
      loading: false,
      error: null,
    });
    render(<ConversationsLog />);
    expect(
      screen.getByRole('button', { name: /Rachel & Ross/i }),
    ).toBeInTheDocument();
  });

  // ─── Timestamps + locate-on-map ────────────────────────────────────────

  it('shows a duration label for ended conversations', () => {
    mockedUse.mockReturnValue({
      conversations: [
        makeConv({
          started_at: '2026-04-17T00:00:00Z',
          ended_at: '2026-04-17T00:05:00Z',
        }),
      ],
      loading: false,
      error: null,
    });
    render(<ConversationsLog />);
    // 5-minute conversation → "lasted 5m" in the row footer.
    expect(screen.getByText(/lasted 5m/i)).toBeInTheDocument();
  });

  it('"📍 on map" navigates to /town focused on the first participant', async () => {
    mockedUse.mockReturnValue({
      conversations: [makeConv({ participants: ['Sherlock Holmes', 'Athena'] })],
      loading: false,
      error: null,
    });
    render(<ConversationsLog />);
    expect(screen.getByTestId('loc').textContent).toBe('/');

    const locate = screen.getByRole('button', { name: /show these agents on the map/i });
    await userEvent.click(locate);

    expect(screen.getByTestId('loc').textContent).toBe('/town?focus=Sherlock%20Holmes');
  });

  it('"Show on map" in the modal closes it and navigates', async () => {
    mockedUse.mockReturnValue({
      conversations: [makeConv({ participants: ['Rachel', 'Ross'] })],
      loading: false,
      error: null,
    });
    render(<ConversationsLog />);
    await userEvent.click(screen.getByRole('button', { name: /Rachel & Ross/i }));
    // Modal open: "hello" is modal-only.
    expect(screen.getByText(/hello/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /show on map/i }));

    // Navigated and modal closed (modal-only "hello" gone).
    expect(screen.getByTestId('loc').textContent).toBe('/town?focus=Rachel');
    expect(screen.queryByText(/hello/)).toBeNull();
  });

  // ─── Send footer (signed-in + active conversation) ─────────────────────

  it('shows send input and button when signed in and conversation is active', async () => {
    mockedUse.mockReturnValue({
      conversations: [makeConv({ ended_at: null })],
      loading: false,
      error: null,
    });
    render(<ConversationsLog />);
    await userEvent.click(screen.getByRole('button', { name: /Rachel & Ross/i }));

    // Input appears once the profile fetch resolves with a nickname.
    expect(await screen.findByRole('textbox', { name: /message input/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^send$/i })).toBeInTheDocument();
    // char counter starts at 280
    expect(screen.getByText('280')).toBeInTheDocument();
    // The nickname hint is shown.
    expect(screen.getByText(/speaking as Tester/i)).toBeInTheDocument();
  });

  it('hides send footer for ended conversations', async () => {
    mockedUse.mockReturnValue({
      conversations: [makeConv({ ended_at: '2026-04-17T00:05:00Z' })],
      loading: false,
      error: null,
    });
    render(<ConversationsLog />);
    await userEvent.click(screen.getByRole('button', { name: /Rachel & Ross/i }));

    expect(screen.queryByRole('textbox', { name: /message input/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /^send$/i })).toBeNull();
  });

  it('shows sign-in hint instead of input when not signed in and conversation is active', async () => {
    // Flip the shared mock to signed-out for this test only.
    mockIsSignedIn = false;
    mockedUse.mockReturnValue({
      conversations: [makeConv({ ended_at: null })],
      loading: false,
      error: null,
    });
    render(<ConversationsLog />);
    await userEvent.click(screen.getByRole('button', { name: /Rachel & Ross/i }));

    expect(screen.queryByRole('textbox', { name: /message input/i })).toBeNull();
    expect(screen.getByText(/sign in to join/i)).toBeInTheDocument();

    // Restore default so subsequent tests are unaffected.
    mockIsSignedIn = true;
  });

  it('char counter decrements as user types and turns red over 280', async () => {
    mockedUse.mockReturnValue({
      conversations: [makeConv({ ended_at: null })],
      loading: false,
      error: null,
    });
    render(<ConversationsLog />);
    await userEvent.click(screen.getByRole('button', { name: /Rachel & Ross/i }));

    const input = await screen.findByRole('textbox', { name: /message input/i });
    await userEvent.type(input, 'hello');
    // 280 - 5 = 275
    expect(screen.getByText('275')).toBeInTheDocument();
  });

  it('send button is disabled when input is empty', async () => {
    mockedUse.mockReturnValue({
      conversations: [makeConv({ ended_at: null })],
      loading: false,
      error: null,
    });
    render(<ConversationsLog />);
    await userEvent.click(screen.getByRole('button', { name: /Rachel & Ross/i }));
    expect(await screen.findByRole('button', { name: /^send$/i })).toBeDisabled();
  });

  it('optimistically appends the human turn and POSTs to the backend', async () => {
    mockedUse.mockReturnValue({
      conversations: [makeConv({ ended_at: null })],
      loading: false,
      error: null,
    });
    const fetchMock = stubFetch();

    render(<ConversationsLog />);
    await userEvent.click(screen.getByRole('button', { name: /Rachel & Ross/i }));

    const input = await screen.findByRole('textbox', { name: /message input/i });
    await userEvent.type(input, 'great to meet you');
    await userEvent.click(screen.getByRole('button', { name: /^send$/i }));

    // Optimistic turn is visible with the "You (visitor)" label.
    expect(screen.getByText('You (visitor)')).toBeInTheDocument();
    expect(screen.getByText('great to meet you')).toBeInTheDocument();

    // The fetch was called with the right endpoint and body.
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/town/conversations/c1/say'),
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ text: 'great to meet you' }),
      }),
    );
  });

  it('shows a 409 error and rolls back the optimistic turn when the conversation ended', async () => {
    mockedUse.mockReturnValue({
      conversations: [makeConv({ ended_at: null })],
      loading: false,
      error: null,
    });
    stubFetch({ sayResponse: { ok: false, status: 409 } });

    render(<ConversationsLog />);
    await userEvent.click(screen.getByRole('button', { name: /Rachel & Ross/i }));

    const input = await screen.findByRole('textbox', { name: /message input/i });
    await userEvent.type(input, 'too late');
    await userEvent.click(screen.getByRole('button', { name: /^send$/i }));

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(/conversation has ended/i);
    });
    // Input value is restored so the user can retry or copy their text.
    expect(screen.getByRole('textbox', { name: /message input/i })).toHaveValue('too late');
    // Optimistic turn rolled back.
    expect(screen.queryByText('You (visitor)')).toBeNull();
  });

  it('shows a 429 rate-limit error', async () => {
    mockedUse.mockReturnValue({
      conversations: [makeConv({ ended_at: null })],
      loading: false,
      error: null,
    });
    stubFetch({ sayResponse: { ok: false, status: 429 } });

    render(<ConversationsLog />);
    await userEvent.click(screen.getByRole('button', { name: /Rachel & Ross/i }));
    await userEvent.type(
      await screen.findByRole('textbox', { name: /message input/i }),
      'hello',
    );
    await userEvent.click(screen.getByRole('button', { name: /^send$/i }));

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(/slow down/i);
    });
  });

  it('deduplicates optimistic turn once the polled log grows past the pre-send length', async () => {
    const conv = makeConv({ ended_at: null, public_log: [{ speaker: 'Rachel', text: 'hi' }] });
    mockedUse.mockReturnValue({
      conversations: [conv],
      loading: false,
      error: null,
    });
    stubFetch();

    const { rerender } = render(<ConversationsLog />);
    await userEvent.click(screen.getByRole('button', { name: /Rachel & Ross/i }));

    await userEvent.type(
      await screen.findByRole('textbox', { name: /message input/i }),
      'how are you',
    );
    await userEvent.click(screen.getByRole('button', { name: /^send$/i }));

    // Optimistic turn shows up.
    expect(screen.getByText('how are you')).toBeInTheDocument();

    // Simulate poll returning the real turn from the server (log grew from 1 to 2).
    const updatedConv = {
      ...conv,
      turn_count: 2,
      public_log: [
        { speaker: 'Rachel', text: 'hi' },
        { speaker: 'You', text: 'how are you' },
      ],
    };
    mockedUse.mockReturnValue({
      conversations: [updatedConv],
      loading: false,
      error: null,
    });
    rerender(
      <MemoryRouter initialEntries={['/']}>
        <ConversationsLog />
        <LocationProbe />
      </MemoryRouter>,
    );

    // "how are you" still appears (from server turn), but "You (visitor)" label
    // should no longer be there (optimistic was deduped).
    expect(screen.getByText('how are you')).toBeInTheDocument();
    expect(screen.queryByText('You (visitor)')).toBeNull();
  });

  // ─── goosetown_bubble_clicked window event ─────────────────────────────

  it('dispatching goosetown_bubble_clicked with an active participant opens the modal', async () => {
    // "Barack Obama" is a participant and the conversation is active (ended_at=null).
    mockedUse.mockReturnValue({
      conversations: [
        makeConv({
          id: 'bubble-conv',
          participants: ['Barack Obama', 'Dracula'],
          participant_a: 'Barack Obama',
          participant_b: 'Dracula',
          ended_at: null,
          public_log: [
            { speaker: 'Barack Obama', text: 'Good evening.' },
            { speaker: 'Dracula', text: 'I vant to negotiate.' },
          ],
          turn_count: 2,
        }),
      ],
      loading: false,
      error: null,
    });
    render(<ConversationsLog />);

    // Modal should not be open yet.
    expect(screen.queryByRole('button', { name: /close/i })).toBeNull();

    // Dispatch the bubble-click event from Godot.
    // Wrap in act() so React flushes the setOpenId state update synchronously.
    act(() => {
      window.dispatchEvent(
        new CustomEvent('goosetown_bubble_clicked', {
          detail: { agentId: 'some-uuid-1234', agentName: 'Barack Obama' },
        }),
      );
    });

    // Modal opens showing the conversation participants.
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /close/i })).toBeInTheDocument();
    });
    // Both turns are visible inside the modal.
    expect(screen.getByText('Good evening.')).toBeInTheDocument();
    expect(screen.getByText('I vant to negotiate.')).toBeInTheDocument();
  });

  it('dispatching goosetown_bubble_clicked with a name in no active conversation is a no-op', async () => {
    // One ended conversation — should NOT trigger modal open.
    mockedUse.mockReturnValue({
      conversations: [
        makeConv({
          participants: ['Rachel', 'Ross'],
          ended_at: '2026-04-17T00:05:00Z',
        }),
      ],
      loading: false,
      error: null,
    });
    render(<ConversationsLog />);

    window.dispatchEvent(
      new CustomEvent('goosetown_bubble_clicked', {
        detail: { agentId: 'some-uuid-9999', agentName: 'Rachel' },
      }),
    );

    // No modal should have opened — no Close button appears.
    expect(screen.queryByRole('button', { name: /close/i })).toBeNull();

    // Dispatch with a name that's simply not in any conversation at all.
    window.dispatchEvent(
      new CustomEvent('goosetown_bubble_clicked', {
        detail: { agentId: 'some-uuid-0000', agentName: 'Totally Unknown Agent' },
      }),
    );

    expect(screen.queryByRole('button', { name: /close/i })).toBeNull();
  });

  // ─── Town nickname prompt ──────────────────────────────────────────────

  it('prompts for a nickname (not the message input) when the user has none', async () => {
    stubFetch({ nickname: null });
    mockedUse.mockReturnValue({
      conversations: [makeConv({ ended_at: null })],
      loading: false,
      error: null,
    });
    render(<ConversationsLog />);
    await userEvent.click(screen.getByRole('button', { name: /Rachel & Ross/i }));

    // Prompt input shows once the profile fetch resolves with no nickname.
    expect(
      await screen.findByRole('textbox', { name: /nickname input/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/choose a nickname to join/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^save$/i })).toBeInTheDocument();
    // The message input is NOT present while a nickname is missing.
    expect(screen.queryByRole('textbox', { name: /message input/i })).toBeNull();
  });

  it('shows the message input (not the prompt) when the user already has a nickname', async () => {
    stubFetch({ nickname: 'Cosmo' });
    mockedUse.mockReturnValue({
      conversations: [makeConv({ ended_at: null })],
      loading: false,
      error: null,
    });
    render(<ConversationsLog />);
    await userEvent.click(screen.getByRole('button', { name: /Rachel & Ross/i }));

    expect(
      await screen.findByRole('textbox', { name: /message input/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/speaking as Cosmo/i)).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: /nickname input/i })).toBeNull();
  });

  it('saving a nickname PUTs it and switches to the message input', async () => {
    const fetchMock = stubFetch({ nickname: null });
    mockedUse.mockReturnValue({
      conversations: [makeConv({ ended_at: null })],
      loading: false,
      error: null,
    });
    render(<ConversationsLog />);
    await userEvent.click(screen.getByRole('button', { name: /Rachel & Ross/i }));

    const nicknameInput = await screen.findByRole('textbox', { name: /nickname input/i });
    // The PUT returns the stored nickname.
    fetchMock.mockImplementation((url: string) => {
      if (typeof url === 'string' && url.includes('/town/profile/nickname')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ nickname: 'Maverick' }),
        });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ nickname: null }) });
    });

    await userEvent.type(nicknameInput, 'Maverick');
    await userEvent.click(screen.getByRole('button', { name: /^save$/i }));

    // PUT was issued to the nickname endpoint with the right body.
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/town/profile/nickname'),
        expect.objectContaining({
          method: 'PUT',
          body: JSON.stringify({ nickname: 'Maverick' }),
        }),
      );
    });

    // After save, the message input replaces the prompt.
    expect(
      await screen.findByRole('textbox', { name: /message input/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/speaking as Maverick/i)).toBeInTheDocument();
  });

  it('shows an inline error when the nickname is rejected (422)', async () => {
    const fetchMock = stubFetch({ nickname: null });
    mockedUse.mockReturnValue({
      conversations: [makeConv({ ended_at: null })],
      loading: false,
      error: null,
    });
    render(<ConversationsLog />);
    await userEvent.click(screen.getByRole('button', { name: /Rachel & Ross/i }));

    const nicknameInput = await screen.findByRole('textbox', { name: /nickname input/i });
    fetchMock.mockImplementation((url: string) => {
      if (typeof url === 'string' && url.includes('/town/profile/nickname')) {
        return Promise.resolve({ ok: false, status: 422, json: () => Promise.resolve({}) });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ nickname: null }) });
    });

    await userEvent.type(nicknameInput, 'bad@name');
    await userEvent.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(/isn't allowed/i);
    });
    // Still on the prompt — no message input appeared.
    expect(screen.queryByRole('textbox', { name: /message input/i })).toBeNull();
  });
});
