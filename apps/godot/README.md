# isol88 Godot starter scene

This starter package targets **Godot 4.x**.

## Included
- `maps/isol88/Isol88Map.tscn` — starter scene using the exported PixelLab map as a single Sprite2D
- `maps/isol88/Isol88Map.gd` — helper script with map constants and marker accessors
- `assets/isol88/map-composite.png` — copied from your PixelLab export

## Scene contents
- `MapComposite` — full exported map image
- `PlayerSpawn` — placeholder spawn marker near the lower promenade
- `LibraryEntrance` — marker near the current library entrance
- `FountainCenter` — marker at the fountain plaza
- `NorthTree` and `WestTree` — example landmark markers

## Notes
This is a **starter scene**, not a full tile-by-tile reconstruction.
Right now it uses the composited PixelLab map image so you can get the map into Godot quickly.

## Recommended next steps
1. Instantiate your player at `PlayerSpawn`
2. Add a `Camera2D` that follows the player
3. Add collisions for water, trees, fountain, and the library footprint
4. Replace the flat composite approach later with separate terrain/object layers if you want full editability
