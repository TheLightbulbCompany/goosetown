import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';

import { useOwnerChat } from './useOwnerChat';

describe('useOwnerChat', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it('fetches history on mount', async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url.includes('/owner-chat')) {
        return new Response(JSON.stringify({
          messages: [
            { id: 'm1', text: 'hi', direction: 'owner_to_agent', createdAt: 1 },
            { id: 'm2', text: 'yo', direction: 'agent_to_owner', createdAt: 2 },
          ],
        }), { status: 200 });
      }
      return new Response('{}', { status: 200 });
    });
    vi.stubGlobal('fetch', fetchMock);

    const { result } = renderHook(() =>
      useOwnerChat({ agentId: 'agent-1', getToken: async () => 'fake' }),
    );
    await waitFor(() => {
      expect(result.current.messages.length).toBe(2);
    });
    expect(result.current.messages[0].id).toBe('m1');
    expect(result.current.messages[1].id).toBe('m2');
  });

  it('appends an owner message optimistically when send is called', async () => {
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.includes('/owner-chat') && (init?.method ?? 'GET') === 'GET') {
        return new Response(JSON.stringify({ messages: [] }), { status: 200 });
      }
      if (url.includes('/owner-message')) {
        return new Response(JSON.stringify({ id: 'persisted-1', createdAt: 999 }), { status: 200 });
      }
      return new Response('{}', { status: 200 });
    });
    vi.stubGlobal('fetch', fetchMock);

    const { result } = renderHook(() =>
      useOwnerChat({ agentId: 'agent-1', getToken: async () => 'fake' }),
    );
    await waitFor(() => expect(result.current.messages.length).toBe(0));

    await act(async () => {
      await result.current.send('hello');
    });

    expect(result.current.messages.length).toBe(1);
    expect(result.current.messages[0].text).toBe('hello');
    expect(result.current.messages[0].direction).toBe('owner_to_agent');
  });

  it('appends an agent reply when the WS bridge dispatches owner_reply', async () => {
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ messages: [] }), { status: 200 }),
    );
    vi.stubGlobal('fetch', fetchMock);

    const { result } = renderHook(() =>
      useOwnerChat({ agentId: 'agent-1', getToken: async () => 'fake' }),
    );
    await waitFor(() => expect(result.current.messages.length).toBe(0));

    act(() => {
      window.dispatchEvent(new CustomEvent('gt_owner_chat_event', {
        detail: {
          event: 'owner_reply',
          agent_id: 'agent-1',
          text: 'OK!',
          message_id: 'reply-1',
          timestamp: 9999,
        },
      }));
    });

    expect(result.current.messages.length).toBe(1);
    expect(result.current.messages[0].text).toBe('OK!');
    expect(result.current.messages[0].direction).toBe('agent_to_owner');
  });

  it('ignores owner_reply events for a different agent', async () => {
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ messages: [] }), { status: 200 }),
    );
    vi.stubGlobal('fetch', fetchMock);

    const { result } = renderHook(() =>
      useOwnerChat({ agentId: 'agent-1', getToken: async () => 'fake' }),
    );
    await waitFor(() => expect(result.current.messages.length).toBe(0));

    act(() => {
      window.dispatchEvent(new CustomEvent('gt_owner_chat_event', {
        detail: {
          event: 'owner_reply',
          agent_id: 'OTHER-agent',
          text: 'wrong',
          message_id: 'reply-X',
          timestamp: 9999,
        },
      }));
    });

    expect(result.current.messages.length).toBe(0);
  });
});
