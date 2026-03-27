/**
 * Renders the Godot HTML5 export canvas.
 *
 * Loads the Godot engine script, initializes the game on a <canvas> element,
 * and handles resize/cleanup lifecycle.
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
  }
}

interface GodotCanvasProps {
  className?: string;
}

export default function GodotCanvas({ className }: GodotCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const engineRef = useRef<GodotEngine | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const { getToken } = useAuth();

  useEffect(() => {
    const canvas = canvasRef.current;
    const container = containerRef.current;
    if (!canvas || !container) return;

    let cancelled = false;

    const loadEngine = async () => {
      try {
        // Load the Godot engine script
        await new Promise<void>((resolve, reject) => {
          // Check if already loaded
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

        engineRef.current = engine;
        await engine.startGame();

        if (cancelled) {
          engine.requestQuit();
          return;
        }

        // Pass authenticated WS URL to Godot so it can open a WebSocket
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

        setLoading(false);
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
