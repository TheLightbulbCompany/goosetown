import { useEffect, useRef } from 'react';
import {
  MAP_H,
  MAP_W,
  NIGHT_LIGHTS,
  ROADS,
  TownScene,
  Walker,
  Firefly,
  createFireflies,
  createWalkers,
  gradeTint,
  nightStrength,
  updateFireflies,
  updateWalkers,
} from './townSim';

/** Sprite sheets shipped with the game (6x4 grid of 48px frames). */
const SHEETS = [
  'lucky', 'alice', 'bob', 'knight', 'merchant', 'healer', 'ranger', 'bard',
];

const SPRITE_SRC = 48;
/** Sprites are 48 world px in-game; a touch larger reads better here. */
const SPRITE_SCALE = 1.15;

interface TownCanvasProps {
  /** Mutable scene driven from outside (GSAP scrubs this object). */
  sceneRef: React.MutableRefObject<TownScene>;
  /** When false (reduced motion), the sim freezes on a still frame. */
  animate: boolean;
  /** Draw road polylines for tuning (enabled via ?paths). */
  debugPaths?: boolean;
}

/**
 * Full-viewport canvas that renders the real town map with real resident
 * sprites walking its roads, plus a scroll-driven day -> night color grade.
 */
export function TownCanvas({ sceneRef, animate, debugPaths }: TownCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const animateRef = useRef(animate);
  animateRef.current = animate;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || typeof canvas.getContext !== 'function') return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    let disposed = false;
    let raf = 0;
    let last = performance.now();
    let loaded = false;

    const map = new Image();
    const sheetImgs: HTMLImageElement[] = [];
    const walkers: Walker[] = [];
    let fireflies: Firefly[] = [];

    const isMobile = window.innerWidth < 640;
    const walkerCount = isMobile ? 6 : 9;

    function loadAll() {
      let pending = 1 + SHEETS.length;
      const done = () => {
        pending -= 1;
        if (pending === 0 && !disposed) {
          walkers.push(...createWalkers(sheetImgs.length, walkerCount));
          fireflies = createFireflies(isMobile ? 8 : 14);
          loaded = true;
        }
      };
      map.onload = done;
      map.onerror = done;
      map.src = '/assets/town-background.png';
      for (const name of SHEETS) {
        const img = new Image();
        img.onload = done;
        img.onerror = done;
        img.src = `/assets/sprites/${name}-sheet.png`;
        sheetImgs.push(img);
      }
    }

    function resize() {
      if (!canvas) return;
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const w = canvas.clientWidth;
      const h = canvas.clientHeight;
      if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
        canvas.width = Math.round(w * dpr);
        canvas.height = Math.round(h * dpr);
      }
    }

    function draw(now: number) {
      if (!canvas || !ctx) return;
      resize();
      const cw = canvas.width;
      const ch = canvas.height;
      if (cw === 0 || ch === 0) return;

      const scene = sceneRef.current;
      // Cover-fit at zoom 1; camera clamped so the map always fills the view.
      const scale = Math.max(cw / MAP_W, ch / MAP_H) * Math.max(0.25, scene.zoom);
      const halfW = cw / 2 / scale;
      const halfH = ch / 2 / scale;
      // Gentle autonomous sway keeps the hero alive without fighting GSAP.
      const sway = animateRef.current ? Math.sin(now / 4200) * 18 : 0;
      const camX = Math.min(MAP_W - halfW, Math.max(halfW, scene.x + sway));
      const camY = Math.min(MAP_H - halfH, Math.max(halfH, scene.y));

      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.globalCompositeOperation = 'source-over';
      ctx.imageSmoothingEnabled = false;
      ctx.fillStyle = '#181425';
      ctx.fillRect(0, 0, cw, ch);
      if (!loaded || !map.complete || map.naturalWidth === 0) return;

      ctx.setTransform(scale, 0, 0, scale, cw / 2 - camX * scale, ch / 2 - camY * scale);
      // The asset is a 2x capture of the Godot scene; draw into world units.
      ctx.drawImage(map, 0, 0, MAP_W, MAP_H);

      if (debugPaths) {
        ctx.strokeStyle = 'rgba(255,0,80,0.85)';
        ctx.lineWidth = 4 / scale;
        for (const road of ROADS) {
          ctx.beginPath();
          road.pts.forEach((p, i) => (i === 0 ? ctx.moveTo(p.x, p.y) : ctx.lineTo(p.x, p.y)));
          if (road.closed) ctx.closePath();
          ctx.stroke();
        }
      }

      // Residents, painter's order by y.
      const size = SPRITE_SRC * SPRITE_SCALE;
      const sorted = [...walkers].sort((a, b) => a.pos.y - b.pos.y);
      for (const w of sorted) {
        const img = sheetImgs[w.sheet];
        if (!img || !img.complete || img.naturalWidth === 0) continue;
        // Soft drop shadow.
        ctx.fillStyle = 'rgba(24,20,37,0.28)';
        ctx.beginPath();
        ctx.ellipse(w.pos.x, w.pos.y + 2, size * 0.22, size * 0.09, 0, 0, Math.PI * 2);
        ctx.fill();
        ctx.drawImage(
          img,
          w.frame * SPRITE_SRC, w.facing * SPRITE_SRC, SPRITE_SRC, SPRITE_SRC,
          w.pos.x - size / 2, w.pos.y - size * 0.92, size, size,
        );
      }

      // --- Color grade (screen space multiply tint) ---
      const tint = gradeTint(scene.grade);
      if (tint !== 'rgb(255,255,255)') {
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.globalCompositeOperation = 'multiply';
        ctx.fillStyle = tint;
        ctx.fillRect(0, 0, cw, ch);
      }

      // --- Night: window glows + fireflies (map space, additive) ---
      const night = nightStrength(scene.grade);
      if (night > 0.02) {
        ctx.setTransform(scale, 0, 0, scale, cw / 2 - camX * scale, ch / 2 - camY * scale);
        ctx.globalCompositeOperation = 'screen';
        for (const l of NIGHT_LIGHTS) {
          const r = 60;
          const g = ctx.createRadialGradient(l.x, l.y, 4, l.x, l.y, r);
          g.addColorStop(0, `rgba(255,196,110,${0.5 * night})`);
          g.addColorStop(0.4, `rgba(255,160,80,${0.22 * night})`);
          g.addColorStop(1, 'rgba(255,140,60,0)');
          ctx.fillStyle = g;
          ctx.fillRect(l.x - r, l.y - r, r * 2, r * 2);
        }
        for (const f of fireflies) {
          const a = (0.35 + 0.3 * Math.sin(now / 600 + f.phase)) * night;
          ctx.fillStyle = `rgba(255,236,160,${a.toFixed(3)})`;
          ctx.beginPath();
          ctx.arc(f.pos.x, f.pos.y, 3, 0, Math.PI * 2);
          ctx.fill();
        }
      }

      // --- Speech bubbles (screen space, crisp text untouched by grade) ---
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.globalCompositeOperation = 'source-over';
      const fontPx = Math.round(Math.min(20, Math.max(12, 9 * scale)));
      ctx.font = `${fontPx}px "VCR OSD Mono", monospace`;
      ctx.textBaseline = 'middle';
      for (const w of walkers) {
        if (!w.bubble) continue;
        const sx = (w.pos.x - camX) * scale + cw / 2;
        const sy = (w.pos.y - camY) * scale + ch / 2 - size * 0.95 * scale;
        const padX = fontPx * 0.7;
        const tw = ctx.measureText(w.bubble).width;
        const bw = tw + padX * 2;
        const bh = fontPx * 2;
        const bx = Math.min(cw - bw - 4, Math.max(4, sx - bw / 2));
        const by = sy - bh - 6;
        ctx.fillStyle = '#181425';
        ctx.fillRect(bx - 3, by - 3, bw + 6, bh + 6);
        ctx.fillStyle = '#fffbf2';
        ctx.fillRect(bx, by, bw, bh);
        // Tail.
        ctx.fillStyle = '#181425';
        ctx.fillRect(sx - 5, by + bh + 3, 10, 7);
        ctx.fillStyle = '#fffbf2';
        ctx.fillRect(sx - 3, by + bh, 6, 6);
        ctx.fillStyle = '#181425';
        ctx.fillText(w.bubble, bx + padX, by + bh / 2 + 1);
      }
    }

    function tick(now: number) {
      if (disposed) return;
      raf = requestAnimationFrame(tick);
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      if (document.hidden) return;
      if (loaded && animateRef.current) {
        updateWalkers(walkers, dt, now);
        updateFireflies(fireflies, dt);
      }
      draw(now);
    }

    loadAll();
    raf = requestAnimationFrame(tick);
    return () => {
      disposed = true;
      cancelAnimationFrame(raf);
    };
  }, []);

  return (
    <canvas
      ref={canvasRef}
      aria-hidden
      className="h-full w-full"
      style={{ imageRendering: 'pixelated' }}
    />
  );
}
