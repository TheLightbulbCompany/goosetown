/**
 * Hook providing typed access to the Godot JS bridge.
 *
 * Listens for the 'ready' event from Godot and provides bridge methods
 * that safely no-op when Godot isn't loaded yet.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type { GodotBridge, GodotEventData, GodotEventName } from '../types/godot-bridge';

interface UseGodotBridgeReturn {
  ready: boolean;
  bridge: GodotBridge;
  onEvent: (event: GodotEventName, handler: (data: GodotEventData) => void) => () => void;
}

const noop = () => {};

const nullBridge: GodotBridge = {
  spawnAgent: noop,
  moveAgent: noop,
  removeAgent: noop,
  updateAgentState: noop,
  setCamera: noop,
};

export function useGodotBridge(): UseGodotBridgeReturn {
  const [ready, setReady] = useState(false);
  const handlersRef = useRef<Map<string, Set<(data: GodotEventData) => void>>>(new Map());

  // Register the global event handler that Godot calls
  useEffect(() => {
    window._onGodotEvent = (event: string, data: GodotEventData) => {
      if (event === 'ready') {
        setReady(true);
      }
      const handlers = handlersRef.current.get(event);
      if (handlers) {
        for (const handler of handlers) {
          handler(data);
        }
      }
    };

    // Also listen via CustomEvent for redundancy
    const handleCustomEvent = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      const eventName = e.type.replace('godot_', '');
      if (eventName === 'ready') {
        setReady(true);
      }
      const handlers = handlersRef.current.get(eventName);
      if (handlers) {
        for (const handler of handlers) {
          handler(detail);
        }
      }
    };

    window.addEventListener('godot_ready', handleCustomEvent);
    window.addEventListener('godot_agent_clicked', handleCustomEvent);
    window.addEventListener('godot_location_clicked', handleCustomEvent);

    // Check if bridge is already available (Godot loaded before React)
    if (window._godotBridge) {
      setReady(true);
    }

    return () => {
      window._onGodotEvent = undefined;
      window.removeEventListener('godot_ready', handleCustomEvent);
      window.removeEventListener('godot_agent_clicked', handleCustomEvent);
      window.removeEventListener('godot_location_clicked', handleCustomEvent);
    };
  }, []);

  const bridge: GodotBridge = ready && window._godotBridge ? window._godotBridge : nullBridge;

  const onEvent = useCallback(
    (event: GodotEventName, handler: (data: GodotEventData) => void) => {
      if (!handlersRef.current.has(event)) {
        handlersRef.current.set(event, new Set());
      }
      handlersRef.current.get(event)!.add(handler);

      // Return unsubscribe function
      return () => {
        handlersRef.current.get(event)?.delete(handler);
      };
    },
    [],
  );

  return { ready, bridge, onEvent };
}
