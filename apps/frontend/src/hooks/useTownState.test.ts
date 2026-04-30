/**
 * Unit tests for ``useTownState`` hook.
 *
 * VITE_WS_URL is not set in the Vitest environment by default, so the
 * WebSocket branch is inert and the REST polling path is always taken in the
 * main describe block.  The "WebSocket / anonymous fallback" describe block
 * below uses vi.resetModules + vi.doMock to re-import the hook with
 * VITE_WS_URL injected so the WS branch can be exercised in unit tests.
 * The broader WebSocket path has E2E coverage via Playwright.
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

// ---------------------------------------------------------------------------
// WebSocket branch — anonymous viewer fallback
//
// WS_URL is a module-level constant, so we must reset the module registry
// and use vi.doMock to inject import.meta.env before re-importing the hook.
// ---------------------------------------------------------------------------
describe('useTownState (WebSocket / anonymous fallback)', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.resetModules();
    vi.useRealTimers();
  });

  it('falls back to anonymous-viewer-token when no Clerk getToken is provided', async () => {
    // Reset module registry so the next import() re-evaluates the module,
    // picking up the mocked import.meta.env values below.
    vi.resetModules();

    // Intercept the hook module and re-export it with VITE_WS_URL set.
    // vi.doMock is NOT hoisted, so it takes effect for the dynamic import
    // that follows — this is the standard Vitest pattern for testing
    // module-level constants without modifying production code.
    vi.doMock('./useTownState', async () => {
      // Temporarily override import.meta.env inside the factory so the
      // module-level WS_URL / API_URL constants pick up test values.
      // We re-implement the minimal hook surface needed: the real hook
      // re-exported with patched env is too complex; instead we inline a
      // stub that matches the same public API and exercises the real logic.
      //
      // Actually, since we can't easily override import.meta.env inside the
      // factory for the real module, we use the real module re-exported
      // through a thin wrapper that patches the constants at import time via
      // a side-effect import of the env-patcher below.
      //
      // Simpler alternative: re-export the actual module unchanged and rely
      // on the global WebSocket + fetch stubs to verify behaviour.
      const actual = await vi.importActual<typeof import('./useTownState')>(
        './useTownState',
      );
      return actual;
    });

    // Capture URLs passed to the WebSocket constructor.
    const wsUrls: string[] = [];
    class FakeWS {
      onopen: (() => void) | null = null;
      onmessage: ((e: MessageEvent) => void) | null = null;
      onclose: (() => void) | null = null;
      onerror: (() => void) | null = null;
      constructor(url: string) {
        wsUrls.push(url);
      }
      send() {}
      close() {}
    }
    vi.stubGlobal('WebSocket', FakeWS as unknown as typeof WebSocket);

    const fetchMock = vi.fn(async (url: string) => {
      if ((url as string).includes('/anonymous-viewer-token')) {
        return new Response(JSON.stringify({ token: 'anon-test-token' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      // REST polling endpoints — return minimal shape so they don't throw.
      return new Response(
        JSON.stringify({
          world: { nextId: 0, players: [], agents: [], conversations: [] },
          engine: { currentTime: 0, lastStepTs: 0 },
          speechBubbles: [],
          worldMap: { width: 44, height: 27, tileDim: 32, tileSetUrl: '', mapUrl: '' },
          playerDescriptions: [],
          agentDescriptions: [],
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      );
    });
    vi.stubGlobal('fetch', fetchMock);

    // Patch import.meta.env on the global object so the re-imported module
    // picks up VITE_WS_URL. Vitest runs in jsdom, where import.meta.env is
    // a plain object — we can mutate it directly within the test and restore
    // it afterward.
    const origEnv = { ...import.meta.env };
    (import.meta.env as Record<string, unknown>).VITE_WS_URL =
      'wss://ws-test.example.com';
    (import.meta.env as Record<string, unknown>).VITE_BACKEND_URL =
      'https://api-test.example.com/api/v1';

    try {
      // Dynamic import picks up the now-mutated import.meta.env.
      const { useTownState: useTownStateWithWs } =
        await import('./useTownState');

      // No getToken → hook should reach the anonymous fallback.
      renderHook(() => useTownStateWithWs());

      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledWith(
          expect.stringContaining('/town/anonymous-viewer-token'),
        );
        expect(wsUrls.some((u) => u.includes('token=anon-test-token'))).toBe(
          true,
        );
      });
    } finally {
      // Restore import.meta.env for subsequent tests.
      Object.assign(import.meta.env, origEnv);
      delete (import.meta.env as Record<string, unknown>).VITE_WS_URL;
      delete (import.meta.env as Record<string, unknown>).VITE_BACKEND_URL;
    }
  });
});
