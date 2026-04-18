/**
 * Unit tests for ``useTownState`` hook.
 *
 * VITE_WS_URL is not set in the Vitest environment, so the WebSocket branch
 * is inert and the REST polling path is always taken.  Tests cover that
 * path and the ``processState`` Map-building logic it drives.  The
 * WebSocket branch has its own coverage via Playwright E2E.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';

import { useTownState } from './useTownState';
import type {
  TownStateResponse,
  TownDescriptionsResponse,
} from '../types/town';

function _ok(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: () => Promise.resolve(body),
  } as Response;
}

const _emptyState: TownStateResponse = {
  world: { nextId: 0, players: [], agents: [], conversations: [] },
  engine: { currentTime: 0, lastStepTs: 0 },
  speechBubbles: [],
};

const _emptyDescs: TownDescriptionsResponse = {
  worldMap: { width: 44, height: 27, tileDim: 32, tileSetUrl: '', mapUrl: '' },
  playerDescriptions: [],
  agentDescriptions: [],
};

function _mockFetchForRestPolling(
  state: TownStateResponse,
  descs: TownDescriptionsResponse,
): void {
  // The REST branch fires both ``/state`` and ``/descriptions`` in parallel.
  // Route the mocked fetch based on URL.
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) => {
      if (url.includes('/descriptions')) return Promise.resolve(_ok(descs));
      if (url.includes('/state')) return Promise.resolve(_ok(state));
      return Promise.reject(new Error(`unexpected URL ${url}`));
    }),
  );
}

beforeEach(() => {
  // Default: both endpoints return empty payloads.
  _mockFetchForRestPolling(_emptyState, _emptyDescs);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('useTownState (REST path)', () => {
  it('starts with undefined game state', () => {
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})));

    const { result } = renderHook(() => useTownState());

    expect(result.current.game).toBeUndefined();
  });

  it('populates game state after the REST endpoints respond', async () => {
    const state: TownStateResponse = {
      world: {
        nextId: 1,
        players: [
          {
            id: 'p:0',
            position: { x: 5, y: 10 },
            facing: { dx: 1, dy: 0 },
            speed: 0,
            lastInput: 0,
          },
        ],
        agents: [{ id: 'a:0', playerId: 'p:0' }],
        conversations: [],
      },
      engine: { currentTime: 123, lastStepTs: 100 },
      speechBubbles: [{ speaker: 'Lucky', text: 'hi', conversation_id: 'c1' }],
    };
    const descs: TownDescriptionsResponse = {
      worldMap: { width: 44, height: 27, tileDim: 32, tileSetUrl: '', mapUrl: '' },
      playerDescriptions: [
        { playerId: 'p:0', name: 'Lucky', description: '', character: 'c1' },
      ],
      agentDescriptions: [{ agentId: 'a:0', identity: 'Lucky', plan: '' }],
    };
    _mockFetchForRestPolling(state, descs);

    const { result } = renderHook(() => useTownState());

    await waitFor(() => expect(result.current.game).toBeDefined());
    expect(result.current.game!.world.players).toHaveLength(1);
    expect(result.current.game!.engine.currentTime).toBe(123);
    expect(result.current.game!.speechBubbles).toHaveLength(1);
  });

  it('builds playerDescriptions / agentDescriptions as Maps keyed by id', async () => {
    const descs: TownDescriptionsResponse = {
      worldMap: { width: 44, height: 27, tileDim: 32, tileSetUrl: '', mapUrl: '' },
      playerDescriptions: [
        { playerId: 'p:0', name: 'Alice', description: '', character: 'c1' },
        { playerId: 'p:1', name: 'Bob', description: '', character: 'c2' },
      ],
      agentDescriptions: [
        { agentId: 'a:0', identity: 'Alice', plan: '' },
        { agentId: 'a:1', identity: 'Bob', plan: '' },
      ],
    };
    _mockFetchForRestPolling(_emptyState, descs);

    const { result } = renderHook(() => useTownState());

    await waitFor(() => expect(result.current.game).toBeDefined());
    const g = result.current.game!;
    expect(g.playerDescriptions).toBeInstanceOf(Map);
    expect(g.playerDescriptions.get('p:0')?.name).toBe('Alice');
    expect(g.playerDescriptions.get('p:1')?.name).toBe('Bob');
    expect(g.agentDescriptions).toBeInstanceOf(Map);
    expect(g.agentDescriptions.get('a:0')?.identity).toBe('Alice');
    expect(g.agentDescriptions.get('a:1')?.identity).toBe('Bob');
  });

  it('stays defined-less and does not crash when fetch rejects', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('net down'))),
    );

    const { result } = renderHook(() => useTownState());

    // Give the rejected promise chain a tick to settle; hook's catch should
    // swallow the error rather than blowing up the test.
    await act(async () => {});
    expect(result.current.game).toBeUndefined();
  });

  it('stops polling after unmount', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(() => new Promise(() => {}));
    vi.stubGlobal('fetch', fetchMock);

    const { unmount } = renderHook(() => useTownState());

    // Let initial poll fire
    await act(async () => {});

    unmount();
    const countAtUnmount = fetchMock.mock.calls.length;

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    // No additional fetches after unmount
    expect(fetchMock.mock.calls.length).toBe(countAtUnmount);
  });
});
