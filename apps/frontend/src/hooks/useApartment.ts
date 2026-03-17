import { useCallback, useEffect, useRef, useState } from 'react';
import { useAuth } from '@clerk/clerk-react';

export interface ApartmentAgent {
  agent_id: string;
  agent_name: string;
  display_name: string;
  character: string | null;
  location_context: string | null;
  current_location: string | null;
  current_activity: string | null;
  mood: string | null;
  energy: number;
  status_message: string | null;
  position_x: number;
  position_y: number;
  speed: number;
  facing_x: number;
  facing_y: number;
  current_spot: string | null;
  is_active: boolean;
  sprite_url: string | null;
}

export interface ActivityEvent {
  agent_name: string;
  display_name: string;
  event_type: string;
  description: string;
  location: string | null;
  timestamp: string;
}

export interface ApartmentData {
  agents: ApartmentAgent[];
  activity: ActivityEvent[];
}

const API_URL =
  (typeof import.meta !== 'undefined' && (import.meta as any).env?.VITE_BACKEND_URL) ??
  'http://localhost:8000/api/v1';

const POLL_INTERVAL = 2000;

// Lerp interpolation for smooth movement between poll ticks
interface LerpState {
  prev: { x: number; y: number };
  target: { x: number; y: number };
  startTime: number;
  duration: number;
}

function lerpPosition(lerp: LerpState, now: number): { x: number; y: number } {
  const elapsed = now - lerp.startTime;
  const t = Math.min(1, elapsed / lerp.duration);
  return {
    x: lerp.prev.x + (lerp.target.x - lerp.prev.x) * t,
    y: lerp.prev.y + (lerp.target.y - lerp.prev.y) * t,
  };
}

export function useApartment() {
  const { getToken, isSignedIn, isLoaded } = useAuth();
  const [data, setData] = useState<ApartmentData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const lerpStates = useRef<Map<string, LerpState>>(new Map());
  const latestAgents = useRef<ApartmentAgent[]>([]);

  const updateLerpStates = useCallback((agents: ApartmentAgent[]) => {
    const now = performance.now();
    const newMap = new Map<string, LerpState>();
    for (const a of agents) {
      const existing = lerpStates.current.get(a.agent_id);
      if (existing) {
        const currentPos = lerpPosition(existing, now);
        newMap.set(a.agent_id, {
          prev: currentPos,
          target: { x: a.position_x, y: a.position_y },
          startTime: now,
          duration: POLL_INTERVAL,
        });
      } else {
        newMap.set(a.agent_id, {
          prev: { x: a.position_x, y: a.position_y },
          target: { x: a.position_x, y: a.position_y },
          startTime: now,
          duration: POLL_INTERVAL,
        });
      }
    }
    lerpStates.current = newMap;
    latestAgents.current = agents;
  }, []);

  const lerpAgents = useCallback((): ApartmentAgent[] => {
    const now = performance.now();
    return latestAgents.current.map((a) => {
      const lerp = lerpStates.current.get(a.agent_id);
      if (!lerp) return a;
      const pos = lerpPosition(lerp, now);
      return { ...a, position_x: pos.x, position_y: pos.y };
    });
  }, []);

  const fetchApartment = useCallback(async () => {
    if (!isLoaded) {
      return; // Clerk still initializing, keep loading state
    }
    if (!isSignedIn) {
      setData(null);
      setLoading(false);
      return;
    }

    try {
      const token = await getToken();
      const res = await fetch(`${API_URL}/town/apartment`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });

      if (!res.ok) {
        if (res.status === 401) {
          setError('Not authenticated');
        } else {
          setError(`Failed to fetch apartment data (${res.status})`);
        }
        setData(null);
        setLoading(false);
        return;
      }

      const json: ApartmentData = await res.json();
      updateLerpStates(json.agents);
      setData(json);
      setError(null);
    } catch (err) {
      setError('Failed to connect to server');
      setData(null);
    } finally {
      setLoading(false);
    }
  }, [getToken, isSignedIn, isLoaded, updateLerpStates]);

  useEffect(() => {
    void fetchApartment();
    const interval = setInterval(fetchApartment, POLL_INTERVAL);
    return () => clearInterval(interval);
  }, [fetchApartment]);

  return { data, loading, error, refresh: fetchApartment, lerpAgents };
}
