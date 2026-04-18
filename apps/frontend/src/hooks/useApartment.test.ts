/**
 * Unit tests for ``useApartment`` hook.
 *
 * Covers the auth/fetch state machine plus the lerp interpolation helpers
 * that drive smooth agent movement between 2s poll ticks.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';

import { useApartment, type ApartmentAgent, type ApartmentData } from './useApartment';

// Mock Clerk's useAuth — each test overrides the returned values.
const mockUseAuth = vi.fn();
vi.mock('@clerk/clerk-react', () => ({
  useAuth: () => mockUseAuth(),
}));

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

function _makeAgent(overrides: Partial<ApartmentAgent> = {}): ApartmentAgent {
  return {
    agent_id: 'a1',
    agent_name: 'lucky',
    display_name: 'Lucky',
    character: null,
    location_context: 'apartment',
    current_location: 'desk',
    current_activity: null,
    mood: 'neutral',
    energy: 80,
    status_message: null,
    position_x: 100,
    position_y: 200,
    speed: 0,
    facing_x: 0,
    facing_y: 1,
    current_spot: null,
    is_active: true,
    sprite_url: 'https://cdn/lucky.png',
    ...overrides,
  };
}

function _signedIn(): void {
  mockUseAuth.mockReturnValue({
    isLoaded: true,
    isSignedIn: true,
    getToken: vi.fn().mockResolvedValue('test-jwt'),
  });
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn());
  mockUseAuth.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('useApartment', () => {
  it('stays loading while Clerk is still initialising', async () => {
    mockUseAuth.mockReturnValue({
      isLoaded: false,
      isSignedIn: false,
      getToken: vi.fn(),
    });

    const { result } = renderHook(() => useApartment());

    // Give microtasks a chance — hook should still be loading
    await act(async () => {});
    expect(result.current.loading).toBe(true);
    expect(result.current.data).toBeNull();
    expect(fetch).not.toHaveBeenCalled();
  });

  it('clears data and exits loading when signed out', async () => {
    mockUseAuth.mockReturnValue({
      isLoaded: true,
      isSignedIn: false,
      getToken: vi.fn(),
    });

    const { result } = renderHook(() => useApartment());

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toBeNull();
    expect(fetch).not.toHaveBeenCalled();
  });

  it('fetches apartment data with bearer token when signed in', async () => {
    _signedIn();
    const data: ApartmentData = {
      agents: [_makeAgent()],
      activity: [],
    };
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValue(_okResponse(data));

    const { result } = renderHook(() => useApartment());

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toEqual(data);
    expect(result.current.error).toBeNull();

    const call = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(call[0]).toContain('/town/apartment');
    expect(call[1].headers.Authorization).toBe('Bearer test-jwt');
  });

  it('records a friendly 401 error', async () => {
    _signedIn();
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValue(_errResponse(401));

    const { result } = renderHook(() => useApartment());

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBe('Not authenticated');
    expect(result.current.data).toBeNull();
  });

  it('records a status-specific error for other HTTP failures', async () => {
    _signedIn();
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValue(_errResponse(500));

    const { result } = renderHook(() => useApartment());

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBe('Failed to fetch apartment data (500)');
  });

  it('records a connection error on network failure', async () => {
    _signedIn();
    (fetch as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('offline'));

    const { result } = renderHook(() => useApartment());

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBe('Failed to connect to server');
    expect(result.current.data).toBeNull();
  });

  it('polls every 2 seconds while signed in', async () => {
    _signedIn();
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      _okResponse({ agents: [], activity: [] }),
    );
    vi.useFakeTimers();

    renderHook(() => useApartment());

    // Initial call fires on mount
    await act(async () => {});
    expect(fetch).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it('refresh() triggers a manual fetch', async () => {
    _signedIn();
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      _okResponse({ agents: [], activity: [] }),
    );

    const { result } = renderHook(() => useApartment());
    await waitFor(() => expect(result.current.loading).toBe(false));
    const initialCalls = (fetch as ReturnType<typeof vi.fn>).mock.calls.length;

    await act(async () => {
      await result.current.refresh();
    });
    expect((fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBe(initialCalls + 1);
  });

  it('lerpAgents returns the target position on the very first tick', async () => {
    _signedIn();
    const agent = _makeAgent({ position_x: 500, position_y: 600 });
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      _okResponse({ agents: [agent], activity: [] }),
    );

    const { result } = renderHook(() => useApartment());
    await waitFor(() => expect(result.current.data?.agents.length).toBe(1));

    const lerped = result.current.lerpAgents();
    expect(lerped).toHaveLength(1);
    // No prior state → prev and target both = current → lerped position = target.
    expect(lerped[0].position_x).toBe(500);
    expect(lerped[0].position_y).toBe(600);
  });

  it('lerpAgents interpolates between the previous and target positions', async () => {
    _signedIn();
    const agent1 = _makeAgent({ position_x: 0, position_y: 0 });
    const agent2 = _makeAgent({ position_x: 200, position_y: 400 });

    const fetchMock = fetch as ReturnType<typeof vi.fn>;
    // First poll returns agent1, second poll returns agent2 — hook should
    // lerp between the two positions on the second tick.
    fetchMock.mockResolvedValueOnce(_okResponse({ agents: [agent1], activity: [] }));
    fetchMock.mockResolvedValueOnce(_okResponse({ agents: [agent2], activity: [] }));

    let nowVal = 0;
    vi.spyOn(performance, 'now').mockImplementation(() => nowVal);

    const { result } = renderHook(() => useApartment());
    await waitFor(() => expect(result.current.data?.agents.length).toBe(1));

    // Advance fake time and trigger the refresh manually to get the second payload
    nowVal = 1_000;
    await act(async () => {
      await result.current.refresh();
    });

    // Halfway through the 2s lerp window → midpoint
    nowVal = 2_000;
    const midway = result.current.lerpAgents();
    expect(midway[0].position_x).toBeCloseTo(100, 1);
    expect(midway[0].position_y).toBeCloseTo(200, 1);

    // Past the lerp window → clamped to target
    nowVal = 10_000;
    const done = result.current.lerpAgents();
    expect(done[0].position_x).toBe(200);
    expect(done[0].position_y).toBe(400);
  });
});
