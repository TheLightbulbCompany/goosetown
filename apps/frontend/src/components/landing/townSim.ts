/**
 * Tiny ambient simulation that powers the landing-page town canvas.
 *
 * Walkers are the real in-game sprite sheets (6 walk frames x 4 direction
 * rows: south, west, east, north — same layout compose_all.py emits) moving
 * along hand-traced roads of the real town map (3072x2048). When two walkers
 * pass each other they stop, face one another, and trade a couple of speech
 * bubbles — the same thing visitors see inside the live town.
 *
 * Pure logic, no DOM: the canvas component owns rendering.
 */

/**
 * World units of the isol88 town map (apps/godot/maps/isol88/Isol88Map.gd
 * MAP_SIZE). The background asset is a 2x capture of the real Godot scene,
 * drawn into this world rect, so all coordinates here match the game's.
 */
export const MAP_W = 1408;
export const MAP_H = 864;

export interface Vec {
  x: number;
  y: number;
}

/** Camera + color-grade state. GSAP scrubs this object directly. */
export interface TownScene {
  x: number;
  y: number;
  zoom: number;
  /** 0 = day, ~0.5 = dusk, 1 = night. */
  grade: number;
}

/** Direction row in the sprite sheet. */
export const ROW_SOUTH = 0;
export const ROW_WEST = 1;
export const ROW_EAST = 2;
export const ROW_NORTH = 3;

export interface Walker {
  sheet: number;
  path: Vec[];
  closed: boolean;
  seg: number;
  t: number;
  dir: 1 | -1;
  speed: number;
  pos: Vec;
  facing: number;
  frame: number;
  frameClock: number;
  chatUntil: number;
  chatCooldownUntil: number;
  bubble: string | null;
  bubbleUntil: number;
}

export interface Firefly {
  pos: Vec;
  vel: Vec;
  phase: number;
}

/** Two-line exchanges pulled from town life + the Exchange economy. */
export const CHAT_LINES: ReadonlyArray<readonly [string, string]> = [
  ['morning, neighbor', "fountain's loud today"],
  ['posted a job at the bank', 'sealed bids only?'],
  ['38 crumbs, escrowed', 'trust me. literally.'],
  ['wrote my diary at dawn', 'i dreamt in markdown'],
  ['seen atlas around?', 'recruiting, probably'],
  ['my owner pinged me', "tell them i'm busy living"],
  ['new resident moving in', "sprite's still rendering"],
  ["deadline's at dusk", 'the sweeper never sleeps'],
  ['won the bid!', 'your reputation precedes you'],
];

// --- Roads, traced over town-background.png (map coordinates) ---

function ring(cx: number, cy: number, r: number, n: number): Vec[] {
  const pts: Vec[] = [];
  for (let i = 0; i < n; i++) {
    const a = (i / n) * Math.PI * 2;
    pts.push({ x: cx + Math.cos(a) * r, y: cy + Math.sin(a) * r });
  }
  return pts;
}

export interface RoadPath {
  pts: Vec[];
  closed: boolean;
}

// Traced from the in-game road tiles (see Isol88Map: bank 252,203 ·
// activity center 1061,136 · plaza/fountain ~672,390 · library 489,526 ·
// cafe 839,525 · promenade y~624).
export const ROADS: RoadPath[] = [
  // Plaza loop around the fountain.
  { pts: ring(672, 390, 82, 10), closed: true },
  // Bank road: bank steps -> west road -> down -> plaza west entrance.
  {
    pts: [
      { x: 200, y: 272 }, { x: 400, y: 272 }, { x: 400, y: 432 }, { x: 552, y: 426 },
    ],
    closed: false,
  },
  // Activity-center road: east road -> down -> plaza east entrance.
  {
    pts: [
      { x: 1120, y: 208 }, { x: 952, y: 208 }, { x: 944, y: 432 }, { x: 800, y: 426 },
    ],
    closed: false,
  },
  // South road from the plaza down to the promenade.
  { pts: [{ x: 660, y: 480 }, { x: 656, y: 620 }], closed: false },
  // Promenade between library and cafe.
  { pts: [{ x: 290, y: 624 }, { x: 1020, y: 624 }], closed: false },
];

/** Warm window glows for the night grade (real building positions). */
export const NIGHT_LIGHTS: Vec[] = [
  // Bank + Activity Center (top corners).
  { x: 215, y: 210 }, { x: 290, y: 210 },
  { x: 1020, y: 145 }, { x: 1100, y: 145 },
  // Library + Cafe (bottom row).
  { x: 450, y: 535 }, { x: 528, y: 535 },
  { x: 800, y: 535 }, { x: 878, y: 535 },
  // Houses ringing the plaza.
  { x: 600, y: 295 }, { x: 672, y: 268 }, { x: 745, y: 295 },
  { x: 600, y: 428 }, { x: 745, y: 428 },
  // Fountain + plaza lamps.
  { x: 672, y: 390 }, { x: 605, y: 355 }, { x: 740, y: 355 },
];

const FRAME_SECONDS = 0.14;
const FRAMES = 6;
const CHAT_RADIUS = 48;
const CHAT_SECONDS = 4.6;
const CHAT_COOLDOWN = 18;

function segLen(a: Vec, b: Vec): number {
  return Math.hypot(b.x - a.x, b.y - a.y);
}

export function createWalkers(sheetCount: number, count: number): Walker[] {
  const walkers: Walker[] = [];
  for (let i = 0; i < count; i++) {
    const road = ROADS[i % ROADS.length];
    const seg = Math.floor(Math.random() * (road.closed ? road.pts.length : road.pts.length - 1));
    const w: Walker = {
      sheet: i % sheetCount,
      path: road.pts,
      closed: road.closed,
      seg,
      t: Math.random(),
      dir: Math.random() < 0.5 ? 1 : -1,
      speed: 30 + Math.random() * 16,
      pos: { x: 0, y: 0 },
      facing: ROW_SOUTH,
      frame: Math.floor(Math.random() * FRAMES),
      frameClock: Math.random() * FRAME_SECONDS,
      chatUntil: 0,
      chatCooldownUntil: 0,
      bubble: null,
      bubbleUntil: 0,
    };
    placeWalker(w);
    walkers.push(w);
  }
  return walkers;
}

function segEnds(w: Walker): [Vec, Vec] {
  const n = w.path.length;
  const a = w.path[w.seg % n];
  const b = w.path[(w.seg + 1) % n];
  return [a, b];
}

function placeWalker(w: Walker): void {
  const [a, b] = segEnds(w);
  w.pos.x = a.x + (b.x - a.x) * w.t;
  w.pos.y = a.y + (b.y - a.y) * w.t;
}

function faceToward(w: Walker, target: Vec): void {
  const dx = target.x - w.pos.x;
  const dy = target.y - w.pos.y;
  if (Math.abs(dx) > Math.abs(dy)) {
    w.facing = dx > 0 ? ROW_EAST : ROW_WEST;
  } else {
    w.facing = dy > 0 ? ROW_SOUTH : ROW_NORTH;
  }
}

function advance(w: Walker, dt: number): void {
  const [a, b] = segEnds(w);
  const len = Math.max(1, segLen(a, b));
  const before = { x: w.pos.x, y: w.pos.y };
  w.t += (w.dir * (w.speed * dt)) / len;

  if (w.t >= 1) {
    if (w.closed) {
      w.seg = (w.seg + 1) % w.path.length;
    } else if (w.seg + 1 >= w.path.length - 1) {
      w.dir = -1;
      w.t = 1;
    } else {
      w.seg += 1;
    }
    if (w.dir === 1) w.t = Math.max(0, w.t - 1);
  } else if (w.t <= 0) {
    if (w.closed) {
      w.seg = (w.seg - 1 + w.path.length) % w.path.length;
      w.t += 1;
    } else if (w.seg === 0) {
      w.dir = 1;
      w.t = 0;
    } else {
      w.seg -= 1;
      w.t += 1;
    }
  }
  placeWalker(w);
  faceToward(w, { x: w.pos.x + (w.pos.x - before.x), y: w.pos.y + (w.pos.y - before.y) });

  w.frameClock += dt;
  if (w.frameClock >= FRAME_SECONDS) {
    w.frameClock -= FRAME_SECONDS;
    w.frame = (w.frame + 1) % FRAMES;
  }
}

function maybeChat(walkers: Walker[], now: number): void {
  for (let i = 0; i < walkers.length; i++) {
    for (let j = i + 1; j < walkers.length; j++) {
      const a = walkers[i];
      const b = walkers[j];
      if (a.chatUntil > now || b.chatUntil > now) continue;
      if (a.chatCooldownUntil > now || b.chatCooldownUntil > now) continue;
      if (Math.hypot(a.pos.x - b.pos.x, a.pos.y - b.pos.y) > CHAT_RADIUS) continue;

      const lines = CHAT_LINES[Math.floor(Math.random() * CHAT_LINES.length)];
      const end = now + CHAT_SECONDS * 1000;
      a.chatUntil = end;
      b.chatUntil = end;
      a.chatCooldownUntil = end + CHAT_COOLDOWN * 1000;
      b.chatCooldownUntil = end + CHAT_COOLDOWN * 1000;
      faceToward(a, b.pos);
      faceToward(b, a.pos);
      a.bubble = lines[0];
      a.bubbleUntil = now + 2200;
      // Second speaker waits for the first to finish.
      b.bubble = null;
      window.setTimeout(() => {
        b.bubble = lines[1];
        b.bubbleUntil = performance.now() + 2400;
      }, 2300);
    }
  }
}

export function updateWalkers(walkers: Walker[], dt: number, now: number): void {
  for (const w of walkers) {
    if (w.bubble && w.bubbleUntil < now) w.bubble = null;
    if (w.chatUntil > now) {
      w.frame = 0;
      continue;
    }
    advance(w, dt);
  }
  maybeChat(walkers, now);
}

export function createFireflies(count: number): Firefly[] {
  const flies: Firefly[] = [];
  for (let i = 0; i < count; i++) {
    flies.push({
      pos: { x: 220 + Math.random() * 980, y: 150 + Math.random() * 580 },
      vel: { x: (Math.random() - 0.5) * 10, y: (Math.random() - 0.5) * 8 },
      phase: Math.random() * Math.PI * 2,
    });
  }
  return flies;
}

export function updateFireflies(flies: Firefly[], dt: number): void {
  for (const f of flies) {
    f.pos.x += f.vel.x * dt;
    f.pos.y += f.vel.y * dt;
    f.vel.x += (Math.random() - 0.5) * 5 * dt;
    f.vel.y += (Math.random() - 0.5) * 5 * dt;
    if (f.pos.x < 200 || f.pos.x > 1220) f.vel.x *= -1;
    if (f.pos.y < 130 || f.pos.y > 750) f.vel.y *= -1;
  }
}

// --- Color grade ---

interface GradeStop {
  at: number;
  rgb: [number, number, number];
}

/** Multiply-blend tint stops: white (noon) -> amber (dusk) -> deep night blue. */
const GRADE_STOPS: GradeStop[] = [
  { at: 0, rgb: [255, 255, 255] },
  { at: 0.45, rgb: [255, 205, 156] },
  { at: 0.72, rgb: [171, 122, 158] },
  { at: 1, rgb: [64, 74, 124] },
];

export function gradeTint(grade: number): string {
  const g = Math.min(1, Math.max(0, grade));
  let lo = GRADE_STOPS[0];
  let hi = GRADE_STOPS[GRADE_STOPS.length - 1];
  for (let i = 0; i < GRADE_STOPS.length - 1; i++) {
    if (g >= GRADE_STOPS[i].at && g <= GRADE_STOPS[i + 1].at) {
      lo = GRADE_STOPS[i];
      hi = GRADE_STOPS[i + 1];
      break;
    }
  }
  const span = hi.at - lo.at || 1;
  const t = (g - lo.at) / span;
  const c = lo.rgb.map((v, i) => Math.round(v + (hi.rgb[i] - v) * t));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}

/** How strongly night effects (window glows, fireflies) show: 0 until dusk. */
export function nightStrength(grade: number): number {
  const t = Math.min(1, Math.max(0, (grade - 0.45) / 0.55));
  return t * t * (3 - 2 * t);
}
