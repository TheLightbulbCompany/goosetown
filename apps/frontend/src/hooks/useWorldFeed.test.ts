/**
 * Unit tests for ``useWorldFeed`` hook.
 *
 * The hook fetches ``/town/world-events?limit=…`` on mount and every 10s.
 * Tests stub ``global.fetch`` with Vitest mocks and drive the interval with
 * fake timers.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';

import { useWorldFeed } from './useWorldFeed';

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

const SAMPLE_EVENTS = [
  {
    eventId: 'e1',
    timestamp: new Date(Date.now() - 60 * 60_000).toISOString(),
    tag: 'weather.storm',
    summary: 'rain begins',
    location: 'plaza',
  },
  {
    eventId: 'e2',
    timestamp: new Date(Date.now() - 2 * 60 * 60_000).toISOString(),
    tag: 'ambient.cat',
    summary: 'cat at cafe',
    location: null,
  },
];

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn());
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('useWorldFeed', () => {
  it('starts in loading state with empty events list', () => {
    (fetch as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {})); // never resolves

    const { result } = renderHook(() => useWorldFeed());

    expect(result.current.loading).toBe(true);
    expect(result.current.events).toEqual([]);
    expect(result.current.error).toBeNull();
  });

  it('populates events after a successful fetch', async () => {
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValue(_okResponse(SAMPLE_EVENTS));

    const { result } = renderHook(() => useWorldFeed());

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.events).toEqual(SAMPLE_EVENTS);
    expect(result.current.error).toBeNull();
  });

  it('drops events older than 48 h, in the future, or with bad timestamps', async () => {
    const hoursAgo = (h: number) => new Date(Date.now() - h * 60 * 60_000).toISOString();
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      _okResponse([
        { ...SAMPLE_EVENTS[0], eventId: 'fresh', timestamp: hoursAgo(47) },
        { ...SAMPLE_EVENTS[0], eventId: 'old', timestamp: hoursAgo(24 * 108) },
        { ...SAMPLE_EVENTS[0], eventId: 'future', timestamp: hoursAgo(-1) },
        { ...SAMPLE_EVENTS[0], eventId: 'bad', timestamp: 'nope' },
      ]),
    );

    const { result } = renderHook(() => useWorldFeed());

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.events.map((e) => e.eventId)).toEqual(['fresh']);
  });

  it('passes the limit param into the URL', async () => {
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValue(_okResponse([]));

    renderHook(() => useWorldFeed({ limit: 25 }));

    await waitFor(() => expect(fetch).toHaveBeenCalled());
    const url = (fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(url).toContain('limit=25');
    expect(url).toContain('/town/world-events');
  });

  it('records an error message when the response is non-2xx', async () => {
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValue(_errResponse(503));

    const { result } = renderHook(() => useWorldFeed());

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBe('HTTP 503');
    expect(result.current.events).toEqual([]);
  });

  it('records an error message on network failure', async () => {
    (fetch as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('network gone'));

    const { result } = renderHook(() => useWorldFeed());

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBe('network gone');
  });

  it('polls at the configured interval', async () => {
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValue(_okResponse([]));
    vi.useFakeTimers();

    renderHook(() => useWorldFeed({ pollIntervalMs: 10_000 }));

    // Initial fetch fires immediately
    expect(fetch).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(fetch).toHaveBeenCalledTimes(2);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(fetch).toHaveBeenCalledTimes(3);
  });

  it('stops polling after unmount', async () => {
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValue(_okResponse([]));
    vi.useFakeTimers();

    const { unmount } = renderHook(() => useWorldFeed({ pollIntervalMs: 10_000 }));
    expect(fetch).toHaveBeenCalledTimes(1);

    unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(fetch).toHaveBeenCalledTimes(1); // no additional calls
  });

  it('clears error on successful retry (real timers)', async () => {
    (fetch as ReturnType<typeof vi.fn>)
      .mockRejectedValueOnce(new Error('first fail'))
      .mockResolvedValue(_okResponse(SAMPLE_EVENTS));

    // Poll interval must be comfortably longer than waitFor's ~50ms check
    // granularity, otherwise the retry clears the error before waitFor can ever
    // observe the 'first fail' state and the assertion flakes ("expected null to
    // be 'first fail'"). 500ms gives the error state a stable, observable window.
    const { result } = renderHook(() => useWorldFeed({ pollIntervalMs: 500 }));

    // First fetch fails — error is set and observable before the retry fires.
    await waitFor(() => expect(result.current.error).toBe('first fail'));

    // After the next poll, error clears and events populate.
    await waitFor(() => expect(result.current.error).toBeNull(), { timeout: 3000 });
    expect(result.current.events).toEqual(SAMPLE_EVENTS);
  });
});
