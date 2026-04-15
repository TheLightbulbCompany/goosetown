/**
 * Hook to fetch and subscribe to GooseTown state.
 *
 * Data sources (in priority order):
 * 1. WebSocket push (world_state or town_state messages) — real-time, ~2s tick
 * 2. REST polling fallback — every 2s for unauthenticated observers
 *
 * Godot handles visual interpolation, so this hook provides raw server state only.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type {
  TownGameState,
  TownStateResponse,
  TownDescriptionsResponse,
  TownWsMessage,
  GameServerWorldState,
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

export function useTownState(getToken?: () => Promise<string | null>, userId?: string | null): {
  game: TownGameState | undefined;
} {
  const [game, setGame] = useState<TownGameState>();
  const wsConnectedRef = useRef(false);

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
        wsConnectedRef.current = true;
        ws!.send(JSON.stringify({ type: 'town_subscribe' }));
      };

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);

          // Game server world_state event (Godot headless server)
          // Forward to Godot WebGL for rendering agents on the map
          if (msg.event === 'world_state' && Array.isArray(msg.agents)) {
            console.log('[GooseTown] world_state:', msg.agents.length, 'agents');
            try {
              const w = window as unknown as Record<string, unknown>;
              if (w.godotUpdateWorldState) {
                // Inject userId so Godot can filter apartment agents by owner
                const payload = { ...msg, _userId: userId ?? '' };
                (w.godotUpdateWorldState as (data: string) => void)(JSON.stringify(payload));
                console.log('[GooseTown] Forwarded to Godot');
              }
              // Expose town-context agent positions for camera-focus lookups
              const positions: Record<string, { x: number; y: number; display: string }> = {};
              for (const a of msg.agents) {
                if (a.context === 'town' || !a.context) {
                  const key = (a.name || '').toLowerCase();
                  const display = a.display_name || a.name || '';
                  if (key) positions[key] = { x: a.x, y: a.y, display };
                  if (display) positions[display.toLowerCase()] = { x: a.x, y: a.y, display };
                }
              }
              w._goosetown_town_agents = positions;
            } catch { /* Godot not loaded yet */ }
            const gsMsg = msg as GameServerWorldState;
            const now = Date.now();
            const players = gsMsg.agents.map((a, i) => ({
              id: `p:${i}`,
              position: { x: a.x, y: a.y },
              facing: {
                dx: a.facing === 'east' ? 1 : a.facing === 'west' ? -1 : 0,
                dy: a.facing === 'south' ? 1 : a.facing === 'north' ? -1 : 0,
              },
              speed: a.is_moving ? 1.2 : 0,
              lastInput: now,
            }));
            const agents = gsMsg.agents.map((a, i) => ({
              id: `a:${i}`,
              playerId: `p:${i}`,
            }));
            const playerDescs: PlayerDescription[] = gsMsg.agents.map((a, i) => ({
              playerId: `p:${i}`,
              name: a.name,
              description: '',
              character: a.spriteUrl || 'c1',
            }));
            const agentDescs: AgentDescription[] = gsMsg.agents.map((a, i) => ({
              agentId: `a:${i}`,
              identity: a.name,
              plan: '',
            }));
            processState(
              {
                world: { nextId: gsMsg.agents.length, players, agents, conversations: [] },
                engine: { currentTime: now, lastStepTs: now },
                speechBubbles: [],
              },
              {
                worldMap: { width: 44, height: 27, tileDim: 32, tileSetUrl: '', mapUrl: '' },
                playerDescriptions: playerDescs,
                agentDescriptions: agentDescs,
              },
            );
            return;
          }

          // Agent switched context (town <-> apartment) — tell Godot to switch scene
          if (msg.event === 'agent_context_changed') {
            console.log('[GooseTown] Context changed:', msg.context, 'for agent', msg.agent_id);
            try {
              const w = window as unknown as Record<string, unknown>;
              const bridge = w._godotBridge as { switchScene?: (name: string) => void } | undefined;
              if (bridge?.switchScene) {
                const newScene = msg.context === 'apartment' ? 'apartment' : 'town';
                bridge.switchScene(newScene);
                window.dispatchEvent(new CustomEvent('goosetown_scene_changed', {
                  detail: { scene: newScene },
                }));
              }
            } catch { /* Godot not loaded yet */ }
            return;
          }

          // Legacy backend town_state message
          const legacyMsg = msg as TownWsMessage;
          if (legacyMsg.type === 'town_state' && legacyMsg.worldState && legacyMsg.gameDescriptions) {
            processState(
              {
                world: legacyMsg.worldState.world,
                engine: legacyMsg.worldState.engine,
                speechBubbles: legacyMsg.speechBubbles ?? [],
              },
              legacyMsg.gameDescriptions,
              legacyMsg.speechBubbles,
            );
          }
        } catch {
          /* malformed message */
        }
      };

      ws.onclose = () => {
        wsConnectedRef.current = false;
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
      wsConnectedRef.current = false;
      if (ws) {
        ws.onclose = null;
        ws.onerror = null;
        ws.onmessage = null;
        ws.close();
      }
    };
  }, [getToken, processState, userId]);

  // -- REST polling fallback --------------------------------------------------
  useEffect(() => {
    let cancelled = false;

    const poll = async () => {
      if (cancelled) return;
      // Skip REST polling when WebSocket is actively delivering state updates
      if (wsConnectedRef.current) {
        if (!cancelled) setTimeout(poll, POLL_INTERVAL);
        return;
      }
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
