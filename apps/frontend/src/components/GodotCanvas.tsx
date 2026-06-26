/**
 * Renders the Godot HTML5 export canvas.
 *
 * Loads the Godot engine script, initializes the game on a <canvas> element,
 * and handles resize/cleanup lifecycle.
 *
 * Accepts a `scene` prop (any id registered in `SCENES`) to switch maps via the
 * JsBridge autoload's `switchScene` callback. The engine is loaded once and
 * persists across scene switches — only the Godot scene tree changes.
 */

import { useEffect, useRef, useState } from 'react';

const GODOT_BASE = import.meta.env.VITE_GODOT_BASE ?? '/godot';
const GODOT_JS_PATH = `${GODOT_BASE}/goosetown.js`;

// Approximate uncompressed payload (wasm + pck). Godot's onProgress reports
// total=0, so we estimate percent against this; cap at 99 until startGame
// resolves. Update if the export grows materially.
const GODOT_PAYLOAD_BYTES = 46_000_000;

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
    __gt_dispatch_event_goosetown_bubble_clicked?: (args: GtDispatchEventArgs) => void;
    __gt_sprite_load_count?: number;
    godotUpdateAgentSprite?: (json: string) => void;
  };

  w.__gt_dispatch_event_scene_changed = (args) => {
    window.dispatchEvent(new CustomEvent('godot_scene_changed', { detail: args }));
  };

  w.__gt_dispatch_event_goosetown_bubble_clicked = (args) => {
    window.dispatchEvent(new CustomEvent('goosetown_bubble_clicked', { detail: args }));
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
  new (config: {
    executable: string;
    canvas: HTMLCanvasElement;
    args?: string[];
    /**
     * 0 = none (we own the sizing), 1 = project (use the project's window size),
     * 2 = adaptive (resize to browser window — the default; fights our parent
     * container and overflows on layouts with sidebars).
     */
    canvasResizePolicy?: 0 | 1 | 2;
    /**
     * Fired during engine asset download (.wasm/.pck). `current` counts
     * uncompressed bytes; `total` is ALWAYS 0 in Godot 4.6's web loader
     * (verified empirically) — callers must supply their own denominator.
     */
    onProgress?: (current: number, total: number) => void;
  }): GodotEngine;
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
  /** Scene id from the SCENES registry. Validated by sceneForPath() upstream. */
  scene?: string;
}

export default function GodotCanvas({ className, scene = 'town' }: GodotCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const engineRef = useRef<GodotEngine | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [progress, setProgress] = useState<number | null>(null);
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

        // Initialize canvas size — DPR-aware so HiDPI displays render
        // at the parent's CSS size, not at the window's pixel size.
        const rect = container.getBoundingClientRect();
        const dpr0 = window.devicePixelRatio || 1;
        canvas.style.width = `${rect.width}px`;
        canvas.style.height = `${rect.height}px`;
        canvas.width = Math.round(rect.width * dpr0);
        canvas.height = Math.round(rect.height * dpr0);

        // Create and start the engine. canvasResizePolicy: 0 disables
        // Godot's built-in canvas-resize logic, which by default sets
        // canvas.style.{width,height} to window dimensions and overflows
        // our sidebar-constrained parent. The ResizeObserver below owns
        // the canvas sizing instead.
        const engine = new window.Engine({
          executable: `${GODOT_BASE}/goosetown`,
          canvas,
          canvasResizePolicy: 0,
          onProgress: (current: number, total: number) => {
            if (cancelled) return;
            const denom = total > 0 ? total : GODOT_PAYLOAD_BYTES;
            setProgress(Math.min(99, Math.round((current / denom) * 100)));
          },
        });

        // World state is forwarded to Godot via JS bridge from useTownState's WebSocket.
        // No need to set _goosetown_ws_url — Godot doesn't open its own WebSocket.

        engineRef.current = engine;
        await engine.startGame();

        if (cancelled) {
          engine.requestQuit();
          return;
        }

        setProgress(null);
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

    // Resize observer to keep canvas matching container.
    //
    // The Godot WebGL engine's startGame() sets ``canvas.width`` and
    // ``canvas.style.width`` to window-derived values, which makes the
    // canvas overflow its sidebar-constrained parent. Own the sizing
    // here: set CSS dims to the parent's content rect, and backing-store
    // dims to that times the device pixel ratio so HiDPI rendering
    // stays sharp without overflow.
    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const { width, height } = entry.contentRect;
        if (!canvas) continue;
        const dpr = window.devicePixelRatio || 1;
        canvas.style.width = `${width}px`;
        canvas.style.height = `${height}px`;
        canvas.width = Math.round(width * dpr);
        canvas.height = Math.round(height * dpr);
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

  // Derive a human-readable loading label for the two distinct phases:
  //   Phase 1 (loading=true):  engine script + WASM + .pck downloading
  //   Phase 2 (loading=false, engineReady=false): engine started, waiting for
  //     the JsBridge autoload to signal readiness via 'godot_ready' event.
  //     This covers the WS connect + first world_state broadcast window —
  //     showing "Connecting…" makes the gap feel intentional, not broken.
  const loadingLabel = loading ? 'Loading town…' : 'Connecting…';
  const showOverlay = (loading || !engineReady) && !error;

  return (
    <div ref={containerRef} className={`relative w-full h-full ${className ?? ''}`}>
      <canvas
        ref={canvasRef}
        id="godot-canvas"
        className="w-full h-full"
        style={{ backgroundColor: '#1a1722' }}
        tabIndex={-1}
      />
      {showOverlay && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-black/50 px-8">
          <p className="text-brown-200 font-body text-lg">{loadingLabel}</p>
          {/* bg-clay-700: the palette has no clay-800 — an undefined shade renders no CSS */}
          <div className="w-full max-w-xs h-2 bg-clay-700 rounded-full overflow-hidden">
            <div
              className={
                progress === null
                  ? 'h-full w-1/3 bg-brown-500 rounded-full animate-pulse'
                  : 'h-full bg-brown-500 rounded-full transition-all duration-200'
              }
              style={progress === null ? undefined : { width: `${progress}%` }}
            />
          </div>
          {progress !== null && (
            <p className="text-clay-300 font-body text-xs">{progress}%</p>
          )}
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
