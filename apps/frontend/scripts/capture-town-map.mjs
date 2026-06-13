/**
 * Re-captures public/assets/town-background.png from the real Godot web
 * build whenever the isol88 map changes.
 *
 * The in-game camera zoom is pinned (bridge setCamera ignores its zoom
 * arg; Isol88Map clamps to cover-zoom), so this captures the map in four
 * camera-position quadrants at the fixed zoom and stitches them.
 *
 * Usage: with the frontend dev server running on $PORT (default 5199):
 *   node scripts/capture-town-map.mjs
 *   python3 -c "...stitch step printed at the end..."
 */
import { chromium } from '@playwright/test';

const PORT = process.env.PORT ?? '5199';
// Viewport sized so the game canvas is exactly 2816x1728 device px
// (sidebar 320 + nav 49): at the pinned zoom 2 each shot covers a
// 704x432-world quadrant at 4px/world.
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 3136, height: 1777 }, deviceScaleFactor: 1 });
// Freeze the clock at noon so the "day" time-of-day tileset renders.
await page.clock.install({ time: new Date('2026-06-12T12:00:00') });
await page.goto(`http://localhost:${PORT}/town`, { waitUntil: 'domcontentloaded' });
await page.waitForFunction(() => !!window._godotBridge?.setCamera, null, { timeout: 60000 });
for (let i = 0; i < 12; i++) await page.clock.runFor(500);
await page.evaluate(() => {
  document.querySelectorAll('nav, button, a, [class*="Toastify"]').forEach((e) => (e.style.visibility = 'hidden'));
});
const quads = [['tl', 352, 216], ['tr', 1056, 216], ['bl', 352, 648], ['br', 1056, 648]];
for (const [name, x, y] of quads) {
  await page.evaluate(([qx, qy]) => window._godotBridge.setCamera(qx, qy, 2), [x, y]);
  for (let i = 0; i < 3; i++) await page.clock.runFor(200);
  await page.locator('canvas').first().screenshot({ path: `/tmp/quad-${name}.png` });
  console.log('captured', name);
}
await browser.close();
console.log(`
Stitch + quantize with:
python3 -c "
from PIL import Image
tiles = {n: Image.open(f'/tmp/quad-{n}.png') for n in ['tl','tr','bl','br']}
full = Image.new('RGB', (5632, 3456))
full.paste(tiles['tl'], (0, 0)); full.paste(tiles['tr'], (2816, 0))
full.paste(tiles['bl'], (0, 1728)); full.paste(tiles['br'], (2816, 1728))
out = full.resize((2816, 1728), Image.NEAREST).quantize(colors=256, method=Image.MAXCOVERAGE, dither=Image.NONE)
out.save('public/assets/town-background.png', optimize=True)
"`);
