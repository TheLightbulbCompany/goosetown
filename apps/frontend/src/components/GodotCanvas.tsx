/**
 * Renders the Godot HTML5 export canvas.
 *
 * Loads the Godot engine script, initializes the game on a <canvas> element,
 * and handles resize/cleanup lifecycle.
 *
 * Accepts a `scene` prop to switch between town and apartment maps via the
 * JsBridge autoload's `switchScene` callback. The engine is loaded once and
 * persists across scene switches — only the Godot scene tree changes.
 */

import { useEffect, useRef, useState } from 'react';

const GODOT_JS_PATH = '/godot/goosetown.js';

// ---------------------------------------------------------------------------
// Secure JsBridge handlers (paired with apps/godot/scripts/autoloads/js_bridge.gd).
//
// The Godot side calls window.__gt_<name>(args) via a template-only eval; all
// user-controlled payload flows through window.set(key, json) and is parsed
// back into `args` before this handler runs. These handlers must exist on
// window BEFORE the Godot engine script runs, otherwise call_js is a no-op.
// ---------------------------------------------------------------------------

interface GtLoadSpriteArgs {
  url?: string;
  agent_id?: string;
  animation_name?: string;
}

interface GtDispatchEventArgs {
  [key: string]: unknown;
}

let _handlersRegistered = false;

function registerJsBridgeHandlers(): void {
  if (_handlersRegistered) return;
  _handlersRegistered = true;

  const w = window as unknown as {
    __gt_load_sprite?: (args: GtLoadSpriteArgs) => void;
    __gt_dispatch_event_scene_changed?: (args: GtDispatchEventArgs) => void;
    __gt_sprite_load_count?: number;
    godotUpdateAgentSprite?: (json: string) => void;
  };

  w.__gt_dispatch_event_scene_changed = (args) => {
    window.dispatchEvent(new CustomEvent('godot_scene_changed', { detail: args }));
  };

  w.__gt_load_sprite = (args) => {
    const url = String(args.url || '');
    const agentId = String(args.agent_id || '');
    const anim = args.animation_name ? String(args.animation_name) : null;
    if (!url || !agentId) return;
    fetch(url)
      .then((r) => r.blob())
      .then(
        (blob) =>
          new Promise<string>((resolve, reject) => {
            const reader = new FileReader();
            reader.onloadend = () => resolve(String(reader.result));
            reader.onerror = () => reject(new Error('FileReader failed'));
            reader.readAsDataURL(blob);
          }),
      )
      .then((dataUrl) => {
        const base64 = dataUrl.split(',')[1];
        if (!w.godotUpdateAgentSprite) return;
        const payload = anim
          ? { id: agentId, base64, animation: anim }
          : { id: agentId, base64 };
        w.godotUpdateAgentSprite(JSON.stringify(payload));
        w.__gt_sprite_load_count = (w.__gt_sprite_load_count || 0) + 1;
      })
      .catch((e) => {
        console.warn('[GooseTown] sprite load failed:', e);
      });
  };
}

interface GodotEngine {
  startGame: (config?: Record<string, unknown>) => Promise<void>;
  requestQuit: () => void;
}

interface GodotEngineConstructor {
  new (config: { executable: string; canvas: HTMLCanvasElement; args?: string[] }): GodotEngine;
  isWebGLAvailable: (majorVersion?: number) => boolean;
}

declare global {
  interface Window {
    Engine?: GodotEngineConstructor;
    _godotBridge?: {
      switchScene: (sceneName: string) => void;
      spawnAgent: (id: string, spriteUrl: string, x: number, y: number, facing: string, name: string) => void;
      moveAgent: (id: string, x: number, y: number, facing: string, speed: number) => void;
      removeAgent: (id: string) => void;
      updateAgentState: (id: string, data: Record<string, unknown>) => void;
      setCamera: (x: number, y: number, zoom: number) => void;
    };
  }
}

interface GodotCanvasProps {
  className?: string;
  scene?: 'town' | 'apartment';
}

export default function GodotCanvas({ className, scene = 'town' }: GodotCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const engineRef = useRef<GodotEngine | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [engineReady, setEngineReady] = useState(false);
  const currentSceneRef = useRef<string>('town');

  // Load engine once on mount
  useEffect(() => {
    const canvas = canvasRef.current;
    const container = containerRef.current;
    if (!canvas || !container) return;

    let cancelled = false;

    // Register secure JsBridge handlers before loading the engine. The Godot
    // side calls window.__gt_<name>(args) with a pre-parsed object (see
    // apps/godot/scripts/autoloads/js_bridge.gd::call_js). User data never
    // touches an eval string — it flows through window.set(key, json).
    registerJsBridgeHandlers();

    const loadEngine = async () => {
      try {
        // Load the Godot engine script
        await new Promise<void>((resolve, reject) => {
          if (window.Engine) {
            resolve();
            return;
          }
          const script = document.createElement('script');
          script.src = GODOT_JS_PATH;
          script.onload = () => resolve();
          script.onerror = () => reject(new Error('Failed to load Godot engine script'));
          document.head.appendChild(script);
        });

        if (cancelled) return;

        if (!window.Engine) {
          throw new Error('Godot Engine not available after script load');
        }

        // Initialize canvas size
        const rect = container.getBoundingClientRect();
        canvas.width = rect.width;
        canvas.height = rect.height;

        // Create and start the engine
        const engine = new window.Engine({
          executable: '/godot/goosetown',
          canvas,
        });

        // World state is forwarded to Godot via JS bridge from useTownState's WebSocket.
        // No need to set _goosetown_ws_url — Godot doesn't open its own WebSocket.

        engineRef.current = engine;
        await engine.startGame();

        if (cancelled) {
          engine.requestQuit();
          return;
        }

        setLoading(false);

        // Wait for Godot bridge to be ready
        const onGodotReady = () => {
          setEngineReady(true);
        };
        window.addEventListener('godot_ready', onGodotReady, { once: true });
        // If bridge is already set up (race condition), mark ready immediately
        if (window._godotBridge) {
          setEngineReady(true);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Failed to load game');
          setLoading(false);
        }
      }
    };

    void loadEngine();

    // Resize observer to keep canvas matching container
    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const { width, height } = entry.contentRect;
        if (canvas) {
          canvas.width = width;
          canvas.height = height;
        }
      }
    });
    observer.observe(container);

    return () => {
      cancelled = true;
      observer.disconnect();
      if (engineRef.current) {
        engineRef.current.requestQuit();
        engineRef.current = null;
      }
    };
    // Intentionally empty deps: the engine is mount-once; reloading on any
    // prop change would cause an infinite reload loop.
  }, []);

  // Switch scenes when the scene prop changes (after engine is ready)
  useEffect(() => {
    if (!engineReady || !window._godotBridge) return;
    if (scene === currentSceneRef.current) return;

    console.log(`[GodotCanvas] Switching scene: ${currentSceneRef.current} → ${scene}`);
    window._godotBridge.switchScene(scene);
    currentSceneRef.current = scene;
  }, [scene, engineReady]);

  return (
    <div ref={containerRef} className={`relative w-full h-full ${className ?? ''}`}>
      <canvas
        ref={canvasRef}
        id="godot-canvas"
        className="w-full h-full"
        tabIndex={-1}
      />
      {loading && !error && (
        <div className="absolute inset-0 flex items-center justify-center bg-black/50">
          <p className="text-brown-200 font-body text-lg">Loading GooseTown...</p>
        </div>
      )}
      {error && (
        <div className="absolute inset-0 flex items-center justify-center bg-black/50">
          <p className="text-red-400 font-body text-sm">{error}</p>
        </div>
      )}
    </div>
  );
}
