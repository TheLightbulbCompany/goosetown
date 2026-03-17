/**
 * Hook to fetch and subscribe to GooseTown state.
 *
 * Data sources (in priority order):
 * 1. WebSocket push (town_state messages) — real-time, ~2s tick
 * 2. REST polling fallback — every 2s for unauthenticated observers
 *
 * Godot handles visual interpolation, so this hook provides raw server state only.
 */

import { useCallback, useEffect, useState } from 'react';
import type {
  TownGameState,
  TownStateResponse,
  TownDescriptionsResponse,
  TownWsMessage,
  PlayerDescription,
  AgentDescription,
  SpeechBubble,
} from '../types/town';

const API_URL =
  (typeof import.meta !== 'undefined' && (import.meta as any).env?.VITE_BACKEND_URL) ??
  'http://localhost:8000/api/v1';

const WS_URL: string | null =
  (typeof import.meta !== 'undefined' && (import.meta as any).env?.VITE_WS_URL) ?? null;

const POLL_INTERVAL = 2000;

export function useTownState(getToken?: () => Promise<string | null>): {
  game: TownGameState | undefined;
} {
  const [game, setGame] = useState<TownGameState>();

  // Process incoming state data (from REST or WS)
  const processState = useCallback(
    (
      stateResp: TownStateResponse,
      descResp: TownDescriptionsResponse,
      speechBubbles?: SpeechBubble[],
    ) => {
      const playerDescs = new Map<string, PlayerDescription>();
      for (const pd of descResp.playerDescriptions) {
        playerDescs.set(pd.playerId, pd);
      }
      const agentDescs = new Map<string, AgentDescription>();
      for (const ad of descResp.agentDescriptions) {
        agentDescs.set(ad.agentId, ad);
      }

      setGame({
        world: stateResp.world,
        engine: stateResp.engine,
        worldMap: descResp.worldMap,
        playerDescriptions: playerDescs,
        agentDescriptions: agentDescs,
        speechBubbles: speechBubbles ?? stateResp.speechBubbles ?? [],
      });
    },
    [],
  );

  // -- WebSocket subscription -------------------------------------------------
  useEffect(() => {
    if (!WS_URL || !getToken) return;

    let ws: WebSocket | null = null;
    let cancelled = false;
    let reconnectDelay = 1000;

    const connect = async () => {
      if (cancelled) return;
      let token: string | null = null;
      try {
        token = await getToken();
      } catch {
        /* retry */
      }
      if (cancelled) return;
      if (!token) {
        setTimeout(connect, reconnectDelay);
        return;
      }

      ws = new WebSocket(`${WS_URL}?token=${token}`);

      ws.onopen = () => {
        if (cancelled) {
          ws?.close();
          return;
        }
        reconnectDelay = 1000;
        ws!.send(JSON.stringify({ type: 'town_subscribe' }));
      };

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data) as TownWsMessage;
          if (msg.type === 'town_state' && msg.worldState && msg.gameDescriptions) {
            processState(
              {
                world: msg.worldState.world,
                engine: msg.worldState.engine,
                speechBubbles: msg.speechBubbles ?? [],
              },
              msg.gameDescriptions,
              msg.speechBubbles,
            );
          }
        } catch {
          /* malformed message */
        }
      };

      ws.onclose = () => {
        if (cancelled) return;
        ws = null;
        setTimeout(connect, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 1.5, 10000);
      };

      ws.onerror = () => {
        /* onclose fires after onerror */
      };
    };

    void connect();

    return () => {
      cancelled = true;
      if (ws) {
        ws.onclose = null;
        ws.onerror = null;
        ws.onmessage = null;
        ws.close();
      }
    };
  }, [getToken, processState]);

  // -- REST polling fallback --------------------------------------------------
  useEffect(() => {
    let cancelled = false;

    const poll = async () => {
      if (cancelled) return;
      try {
        const [stateRes, descRes] = await Promise.all([
          fetch(`${API_URL}/town/state`),
          fetch(`${API_URL}/town/descriptions`),
        ]);
        if (cancelled) return;
        if (stateRes.ok && descRes.ok) {
          const stateData: TownStateResponse = await stateRes.json();
          const descData: TownDescriptionsResponse = await descRes.json();
          processState(stateData, descData);
        }
      } catch {
        /* retry on next interval */
      }
      if (!cancelled) setTimeout(poll, POLL_INTERVAL);
    };

    void poll();
    return () => {
      cancelled = true;
    };
  }, [processState]);

  return { game };
}
