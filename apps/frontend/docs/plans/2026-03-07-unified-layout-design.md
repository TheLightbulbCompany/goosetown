# Unified GooseTown Layout Design

## Goal

Consistent layout across town and apartment views: left sidebar with your agents always visible, full PixiJS viewport (drag/pan/zoom) on both maps.

## Layout

Both pages share the same structure:

```
+--------------------------------------------------+
| TownNav (top bar: Town / Apartment links)        |
+----------+---------------------------------------+
|          |                                       |
|  Left    |                                       |
|  Sidebar |         Map Area                      |
|  (w-80)  |   (town or apartment PixiJS)          |
|          |                                       |
|  Your    |         + zoom controls               |
|  Agents  |                                       |
|          |                                       |
+----------+---------------------------------------+
```

- Left sidebar: ApartmentCard-style cards for your agents, stacked vertically, scrollable
- Clicking an agent pans the map to them if they're on the current view
- Map fills remaining space with full PixiJS viewport (drag/pan/zoom)
- Zoom controls (+/-) in bottom-right of map area

## Component Changes

### New: `GameLayout.tsx`
- Shared layout component used by both pages
- Props: sidebar content + children (map area)
- Renders: left sidebar (w-80, scrollable, border-right) + flex-1 map area

### Modified: Town page (`Town.tsx` / `Game.tsx`)
- Use GameLayout instead of custom flex layout
- Agent cards in left sidebar instead of PlayerDetails on right
- Remove right-side PlayerDetails sidebar
- Keep zoom controls in map area

### Modified: Apartment page (`Apartment.tsx`)
- Use GameLayout instead of scrollable page layout
- Agent cards in left sidebar instead of bottom card section
- Map fills right side (no more scrollable page with min-h)

### Modified: `ApartmentMap.tsx`
- Wrap in PixiViewport (same as town map)
- Full drag/pan/zoom/pinch/momentum/clamp
- Add zoom controls (+/- buttons)
- Ctrl/Cmd + wheel zoom

### Unchanged: `ApartmentCard.tsx`
- Same card component, just rendered in sidebar instead of bottom section
- "View in town" link still works

## What doesn't change

- TownNav component (already shared)
- TownProvider context (already wraps entire app)
- PixiViewport component (reused as-is for apartment)
- Town PixiGame internals (map rendering, player sprites)
- API endpoints or backend
