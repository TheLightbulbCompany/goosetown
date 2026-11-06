/**
 * Unit tests for ``useConversations`` hook.
 *
 * The hook fetches ``/town/conversations?limit=…`` on mount and every 5s.
 * Tests stub ``global.fetch`` with Vitest mocks and drive the interval with
 * fake timers.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';

import { useConversations } from './useConversations';

function _okResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: () => Promise.resolve(body),
  } as Response;
}

function _errResponse(status: number): Response {
  return {
    ok: false,
    status,
    json: () => Promise.resolve({}),
  } as Response;
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn());
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('useConversations', () => {
  it('starts in loading state with empty list', () => {
    (fetch as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {})); // never resolves

    const { result } = renderHook(() => useConversations());

    expect(result.current.loading).toBe(true);
    expect(result.current.conversations).toEqual([]);
    expect(result.current.error).toBeNull();
  });

  it('populates conversations after a successful fetch', async () => {
    const conversations = [
      {
        id: 'c1',
        participant_a: 'A',
        participant_b: 'B',
        location: 'cafe',
        started_at: new Date(Date.now() - 60_000).toISOString(),
        ended_at: null,
        turn_count: 2,
        topic_summary: null,
        public_log: [{ speaker: 'A', text: 'hi' }],
      },
    ];
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValue(_okResponse({ conversations }));

    const { result } = renderHook(() => useConversations());

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.conversations).toEqual(conversations);
    expect(result.current.error).toBeNull();
  });

  it('passes the ``limit`` param into the URL', async () => {
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValue(_okResponse({ conversations: [] }));

    renderHook(() => useConversations(42));

    await waitFor(() => expect(fetch).toHaveBeenCalled());
    const url = (fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(url).toContain('limit=42');
    expect(url).toContain('/town/conversations');
  });

  it('records an error message when the response is non-2xx', async () => {
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValue(_errResponse(500));

    const { result } = renderHook(() => useConversations());

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBe('HTTP 500');
    expect(result.current.conversations).toEqual([]);
  });

  it('records an error message on network failure', async () => {
    (fetch as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'));

    const { result } = renderHook(() => useConversations());

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBe('boom');
  });

  it('polls every 5 seconds', async () => {
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValue(_okResponse({ conversations: [] }));
    vi.useFakeTimers();

    renderHook(() => useConversations());

    // Initial fetch fires immediately
    expect(fetch).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(fetch).toHaveBeenCalledTimes(2);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(fetch).toHaveBeenCalledTimes(3);
  });

  it('stops polling after unmount', async () => {
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValue(_okResponse({ conversations: [] }));
    vi.useFakeTimers();

    const { unmount } = renderHook(() => useConversations());
    expect(fetch).toHaveBeenCalledTimes(1);

    unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    expect(fetch).toHaveBeenCalledTimes(1); // no additional calls
  });

  it('treats a missing ``conversations`` field as empty list', async () => {
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValue(_okResponse({})); // no key

    const { result } = renderHook(() => useConversations());

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.conversations).toEqual([]);
  });
});
