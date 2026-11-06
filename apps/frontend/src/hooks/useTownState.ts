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
import { type EconomyTickerEntry, toTickerEntry, capEvents } from '../lib/economyTicker';

const API_URL = import.meta.env.VITE_BACKEND_URL ?? 'http://localhost:8000/api/v1';

const WS_URL: string | null = import.meta.env.VITE_WS_URL ?? null;

const POLL_INTERVAL = 2000;

// ---------------------------------------------------------------------------
// Anonymous-viewer-token cache (in-memory, per page load)
//
// This token is public (grants WS subscription only), so it is safe to cache
// at module scope and reuse across multiple hook instances or re-renders. The
// cache is intentionally module-level: it survives React re-renders and is
// shared across all hook consumers on the same page. It is NOT persisted to
// sessionStorage on purpose — the token can expire, and a fresh page load
// should re-fetch rather than risk reusing a stale token across navigations.
//
// The prefetch promise is stored separately so concurrent callers awaiting the
// first fetch coalesce onto the single in-flight request rather than issuing N
// redundant fetches.
// ---------------------------------------------------------------------------
let _anonTokenCache: string | null = null;
let _anonTokenInflight: Promise<string | null> | null = null;

function prefetchAnonViewerToken(): Promise<string | null> {
  if (_anonTokenCache) return Promise.resolve(_anonTokenCache);
  if (_anonTokenInflight) return _anonTokenInflight;
  _anonTokenInflight = fetch(`${API_URL}/town/anonymous-viewer-token`)
    .then((r) => (r.ok ? (r.json() as Promise<{ token?: string }>) : Promise.resolve<{ token?: string }>({})))
    .then((body) => {
      const t = body.token ?? null;
      if (t) _anonTokenCache = t;
      _anonTokenInflight = null;
      return t;
    })
    .catch(() => {
      _anonTokenInflight = null;
      return null;
    });
  return _anonTokenInflight;
}

export function useTownState(getToken?: () => Promise<string | null>, userId?: string | null): {
  game: TownGameState | undefined;
  economyEvents: EconomyTickerEntry[];
} {
  const [game, setGame] = useState<TownGameState>();
  const [economyEvents, setEconomyEvents] = useState<EconomyTickerEntry[]>([]);
  const economyIdRef = useRef(0);
  const wsConnectedRef = useRef(false);

  // Kick off the anonymous-viewer-token fetch immediately on mount so the token
  // is ready (or already in-flight) by the time the WS connect() logic needs
  // it. Authenticated users skip this path (getToken wins), but the prefetch is
  // cheap — it hits the cache synchronously if the tab was already used this
  // session. No cleanup needed: the module-level cache persists intentionally.
  useEffect(() => {
    if (!WS_URL) return; // no WS configured — REST-only mode, token unused
    if (!getToken) {
      // Definitely an anonymous viewer — start the fetch right away.
      void prefetchAnonViewerToken();
    } else {
      // Signed-in user: Clerk getToken is primary, but prefetch the anon token
      // as a fallback in case Clerk fails (fire-and-forget, errors swallowed
      // inside prefetchAnonViewerToken).
      void prefetchAnonViewerToken();
    }
  }, []); // mount only — intentionally ignores getToken reference changes

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
    if (!WS_URL) return;

    let ws: WebSocket | null = null;
    let cancelled = false;
    let reconnectDelay = 1000;

    const connect = async () => {
      if (cancelled) return;
      let token: string | null = null;

      // Try Clerk-signed viewer token first (signed-in users).
      if (getToken) {
        try {
          token = await getToken();
        } catch {
          /* fall through to anonymous */
        }
      }

      // Fall back to anonymous viewer token (signed-out users, or Clerk
      // ``getToken`` failure). The endpoint is public; the resulting token
      // grants WS subscription only — no act / chat / register paths.
      // We reuse the module-level prefetch that was kicked off at mount, so
      // this await is typically instantaneous (cache hit) rather than a serial
      // round-trip that would add latency after the WASM download finishes.
      if (!token && !cancelled) {
        try {
          token = await prefetchAnonViewerToken();
        } catch (err) {
          console.warn('[GooseTown] anonymous-viewer-token fetch failed:', err);
          /* retry on next reconnect */
        }
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
                  if (a.id) positions[a.id.toLowerCase()] = { x: a.x, y: a.y, display };
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

          if (msg.event === 'economy_event') {
            try {
              const w = window as unknown as Record<string, unknown>;
              if (w.godotEconomyEvent) {
                (w.godotEconomyEvent as (s: string) => void)(JSON.stringify(msg));
              }
            } catch { /* Godot not loaded */ }
            economyIdRef.current += 1;
            const entry = toTickerEntry(msg, `e:${economyIdRef.current}`);
            setEconomyEvents((prev) => capEvents(prev, entry));
            return;
          }

          // Agent switched context (town <-> apartment). Just log it — DO NOT
          // touch Godot's scene or React's route. The WS broadcast goes to
          // every viewer, so reacting to it would force every viewer's
          // camera to reset to player_spawn and their URL to flip every
          // time any agent (theirs or someone else's) crossed the
          // apartment/town boundary. Scene + route are user-driven via
          // ``GodotCanvas`` watching the ``scene`` prop and React Router
          // controlling the ``/`` vs ``/apartment`` paths. The cross-context
          // visibility filter is already done in ``agent_manager.gd`` via
          // the per-viewer context filter on world_state, so the agents
          // appear/disappear correctly without a scene swap.
          if (msg.event === 'agent_context_changed') {
            console.log('[GooseTown] Context changed:', msg.context, 'for agent', msg.agent_id);
            return;
          }

          // Owner-agent DM events — forward to useOwnerChat via CustomEvent.
          if (msg.event === 'owner_reply') {
            window.dispatchEvent(new CustomEvent('gt_owner_chat_event', { detail: msg }));
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
  //
  // Improvements:
  //   1. Pauses while the tab is hidden — avoids wasted network requests when
  //      the user has backgrounded the tab.
  //   2. Immediately re-polls when the tab becomes visible — catches up without
  //      waiting up to 2 s for the next scheduled tick.
  //   3. Uses a single timer ref to prevent double-scheduling if the
  //      visibilitychange handler fires while a scheduled tick is pending.
  useEffect(() => {
    let cancelled = false;
    const timerRef: { id: ReturnType<typeof setTimeout> | null } = { id: null };

    const schedule = (delay = POLL_INTERVAL) => {
      if (timerRef.id !== null) clearTimeout(timerRef.id);
      timerRef.id = setTimeout(() => {
        timerRef.id = null;
        void poll();
      }, delay);
    };

    const poll = async () => {
      if (cancelled) return;

      // Pause while the tab is hidden — skip the fetch and reschedule so we
      // resume automatically on the next tick (the visibilitychange handler
      // will also trigger an immediate refetch when the tab is shown again).
      if (document.visibilityState === 'hidden') {
        schedule();
        return;
      }

      // Skip REST polling when WebSocket is actively delivering state updates.
      if (wsConnectedRef.current) {
        schedule();
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

      if (!cancelled) schedule();
    };

    const onVisibilityChange = () => {
      if (document.visibilityState !== 'visible') return;
      if (cancelled) return;
      // Cancel any pending scheduled tick and poll immediately so the UI
      // catches up the moment the user returns to the tab.
      if (timerRef.id !== null) {
        clearTimeout(timerRef.id);
        timerRef.id = null;
      }
      void poll();
    };

    document.addEventListener('visibilitychange', onVisibilityChange);

    void poll();

    return () => {
      cancelled = true;
      if (timerRef.id !== null) clearTimeout(timerRef.id);
      document.removeEventListener('visibilitychange', onVisibilityChange);
    };
  }, [processState]);

  return { game, economyEvents };
}
