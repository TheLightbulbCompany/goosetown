# GooseTown frontend design system

## Direction

The web UI speaks the same language as the game it frames: a cozy retro
cartridge. Chunky pixel UI chrome, the game's own palette, type that sits
low and loud. The landing page is a living diorama — the real town map and
real resident sprites ARE the visual; DOM panels float over it in the
game's UI frames. Nothing should look like a generic SaaS component.

Intent words: cozy, handmade, alive, chunky — never sleek, never flat-gray.

## Tokens (tailwind.config.js)

All colors come from the town's pixel art (Pico-8-adjacent palette):

- `brown-100..900` — warm scale: text on dark (100), parchment (200),
  highlight accent (300), wood (500/700), night shadow (800/900)
- `clay-100..900` — cool scale: body text (100), metadata (300), muted
  (500), borders/inactive (700), the page void (900 `#181425`)
- `moss` `#8FDE5D` — success / live / settled crumbs (town grass)
- `lamplight` `#FEC742` — award gold / NEW badges / window glows (top stop
  of the `.game-title` gradient)

Text hierarchy: headings `brown-200`, body `clay-100`, metadata `clay-300`,
muted/disabled `clay-500`.

## Depth strategy: pixel border-images, one language

Cards and buttons use the game's border-image chrome from `assets/ui/`
(`.box`, `.button`, `.game-frame`, `.bubble`) over translucent `clay-900`
fills — NOT rounded-md + border-2 Tailwind defaults, NOT shadows. The
border-image IS the elevation system. Interior padding on `.box` panels is
small (`p-2 sm:p-3`) because the 12px frame already provides breathing room.

## Typography

- `font-display` = Upheaval Pro (chunky pixel): headings, buttons, labels,
  badge chips. Always `tracking-wider`+. Glyphs sit low — `.button span`
  compensates with `translateY(-15%)`.
- `font-body` = VCR OSD Mono: everything else, including data.
- Hero/title gold gradient: `.game-title` (background-clip: text). Apply it
  to the leaf spans, not a parent, when children are GSAP-transformed —
  Chrome stops painting a parent's clipped background into transformed
  children.

## Motion

- GSAP + ScrollTrigger for camera/scroll choreography; CSS `steps()` for
  anything sprite-like (walk cycles, chip pops) — pixel art moves in steps,
  not tweens.
- Every animated surface needs a `prefers-reduced-motion` path (static
  scene, instant content) — and the jsdom test env takes the same path via
  the missing-`matchMedia` guard.
- Canvas loops: cap DPR at 2, skip work when `document.hidden`.

## Landing-page patterns (src/components/landing/)

- `TownCanvas` + `townSim` — map-space camera `{x, y, zoom, grade}` scrubbed
  by GSAP; grade 0→1 = day→night via multiply tint + screen-blend glows.
- Speech bubbles drawn in screen space (crisp, ungraded), pixel-cornered
  white on `#181425`.
- `landing-sprite-walk` — 2x sprite-sheet walk cycle, 6-frame `steps(6)`.
