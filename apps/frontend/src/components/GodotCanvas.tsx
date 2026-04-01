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

import { useAuth } from '@clerk/clerk-react';
import { useEffect, useRef, useState } from 'react';

const GODOT_JS_PATH = '/godot/goosetown.js';

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
  const { getToken } = useAuth();

  // Load engine once on mount
  useEffect(() => {
    const canvas = canvasRef.current;
    const container = containerRef.current;
    if (!canvas || !container) return;

    let cancelled = false;

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

        // Set WS URL BEFORE starting Godot so _ready() can read it
        const wsUrl = import.meta.env.VITE_WS_URL;
        if (wsUrl && getToken) {
          try {
            const clerkToken = await getToken();
            const backendUrl = import.meta.env.VITE_BACKEND_URL;
            const resp = await fetch(`${backendUrl}/town/viewer-token`, {
              headers: { Authorization: `Bearer ${clerkToken}` },
            });
            if (resp.ok) {
              const { token } = await resp.json();
              (window as unknown as Record<string, unknown>)._goosetown_ws_url =
                `${wsUrl}?token=${token}&type=viewer`;
            }
          } catch (e) {
            console.warn('Could not get viewer token:', e);
          }
        }

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
    // eslint-disable-next-line react-hooks/exhaustive-deps -- getToken is a new
    // function reference on every render (from useAuth), so including it in deps
    // would cause the engine to reload in an infinite loop. We only need it once
    // at mount time to fetch the viewer token.
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
