## EditorScript: Export walkability grid and location coordinates from the scene.
## Run via: Script editor > File > Run (Cmd+Shift+X)
##
## Reads:
##   - TerrainLayer tile data → walkable/blocked per cell
##   - YSortedObjects children (StaticBody2D) → collision shapes → blocked cells
##   - Markers + StaticBody2D positions → location coordinates
##
## Writes:
##   - exported/walkability.json
##   - exported/locations.json
##   - Copies to backend/data/goosetown/
@tool
extends EditorScript

const TILE_SIZE: int = 32
const MAP_WIDTH: int = 44
const MAP_HEIGHT: int = 27

# Godot terrain IDs from combined_terrain.tres
# 0=water, 1=shoreline → blocked
# 2=grass, 3=garden path, 4=paving → walkable
const WALKABLE_TERRAINS: Array[int] = [2, 3, 4]

# Backend data directory — resolved relative to Godot project root at runtime
static func _get_backend_data_dir() -> String:
	var project_dir: String = ProjectSettings.globalize_path("res://")
	return project_dir.path_join("../../apps/backend/data/goosetown")


func _run() -> void:
	var scene_root: Node = get_editor_interface().get_edited_scene_root()
	if scene_root == null:
		push_error("ExportMapData: No scene open")
		return

	var terrain_layer: TileMapLayer = scene_root.get_node_or_null("TerrainLayer") as TileMapLayer
	if terrain_layer == null:
		push_error("ExportMapData: TerrainLayer not found")
		return

	var y_sorted: Node2D = scene_root.get_node_or_null("YSortedObjects") as Node2D
	var markers: Node2D = scene_root.get_node_or_null("Markers") as Node2D

	# --- Build walkability grid ---
	var grid: Array = []
	for x in range(MAP_WIDTH):
		var col: Array = []
		for y in range(MAP_HEIGHT):
			col.append(-1)  # default blocked
		grid.append(col)

	# Pass 1: any cell with a tile placed is walkable
	# The PixelLab converter tileset uses corner peering bits but doesn't set
	# the terrain property on tiles. All placed tiles are terrain (walkable).
	# Water/empty areas have no tiles.
	var used_cells: Array[Vector2i] = terrain_layer.get_used_cells()
	for cell in used_cells:
		var x: int = cell.x
		var y: int = cell.y
		if x < 0 or x >= MAP_WIDTH or y < 0 or y >= MAP_HEIGHT:
			continue
		grid[x][y] = 0  # walkable — any placed tile is terrain

	# Pass 2: collision shapes from buildings → blocked
	if y_sorted:
		for child in y_sorted.get_children():
			if child is StaticBody2D:
				_block_collision_shapes(grid, child)

	var walkable_count: int = 0
	for x in range(MAP_WIDTH):
		for y in range(MAP_HEIGHT):
			if grid[x][y] == 0:
				walkable_count += 1

	# --- Build locations ---
	var locations: Dictionary = {}

	# Locations come from Marker2D nodes only (entrance positions, not building centers)
	# After collecting, ensure marker tiles are always walkable (they may overlap collision shapes)
	if markers:
		for child in markers.get_children():
			if child is Marker2D:
				var name_lower: String = child.name.to_snake_case()
				var px: float = child.global_position.x
				var py: float = child.global_position.y
				var tx: int = int(round(px / TILE_SIZE))
				var ty: int = int(round(py / TILE_SIZE))
				locations[name_lower] = {
					"x": tx,
					"y": ty,
					"pixel_x": int(px),
					"pixel_y": int(py),
				}
				# Force marker tiles walkable — these are entrance points
				if tx >= 0 and tx < MAP_WIDTH and ty >= 0 and ty < MAP_HEIGHT:
					grid[tx][ty] = 0

	# --- Write files ---
	var timestamp: String = Time.get_datetime_string_from_system(true)

	var walkability_data: Dictionary = {
		"width": MAP_WIDTH,
		"height": MAP_HEIGHT,
		"tile_size": TILE_SIZE,
		"exported_at": timestamp,
		"grid": grid,
	}

	var locations_data: Dictionary = {
		"exported_at": timestamp,
		"locations": locations,
	}

	# Write to project exported/ dir
	var project_dir: String = ProjectSettings.globalize_path("res://exported")
	DirAccess.make_dir_recursive_absolute(project_dir)
	_write_json(project_dir + "/walkability.json", walkability_data)
	_write_json(project_dir + "/locations.json", locations_data)

	# Copy to backend data dir
	DirAccess.make_dir_recursive_absolute(_get_backend_data_dir())
	_write_json(_get_backend_data_dir() + "/walkability.json", walkability_data)
	_write_json(_get_backend_data_dir() + "/locations.json", locations_data)

	print("ExportMapData: Exported walkability grid (%d walkable, %d blocked) + %d locations" % [
		walkable_count, MAP_WIDTH * MAP_HEIGHT - walkable_count, locations.size()
	])
	for loc_name in locations:
		var loc: Dictionary = locations[loc_name]
		print("  %s: tile(%d, %d) pixel(%d, %d)" % [loc_name, int(loc.x), int(loc.y), loc.pixel_x, loc.pixel_y])


func _block_collision_shapes(grid: Array, body: StaticBody2D) -> void:
	for child in body.get_children():
		if child is CollisionShape2D:
			var shape: Shape2D = child.shape
			var global_pos: Vector2 = body.global_position + child.position

			if shape is RectangleShape2D:
				var rect_shape: RectangleShape2D = shape as RectangleShape2D
				var half: Vector2 = rect_shape.size / 2.0
				var x1: int = int(round((global_pos.x - half.x) / TILE_SIZE))
				var y1: int = int(round((global_pos.y - half.y) / TILE_SIZE))
				var x2: int = int(round((global_pos.x + half.x) / TILE_SIZE))
				var y2: int = int(round((global_pos.y + half.y) / TILE_SIZE))
				for x in range(max(0, x1), min(MAP_WIDTH, x2 + 1)):
					for y in range(max(0, y1), min(MAP_HEIGHT, y2 + 1)):
						grid[x][y] = -1

			elif shape is CircleShape2D:
				var circle_shape: CircleShape2D = shape as CircleShape2D
				var radius_tiles: float = circle_shape.radius / TILE_SIZE
				var cx: float = global_pos.x / TILE_SIZE
				var cy: float = global_pos.y / TILE_SIZE
				var r_ceil: int = int(ceil(radius_tiles))
				var icx: int = int(round(cx))
				var icy: int = int(round(cy))
				for x in range(max(0, icx - r_ceil), min(MAP_WIDTH, icx + r_ceil + 1)):
					for y in range(max(0, icy - r_ceil), min(MAP_HEIGHT, icy + r_ceil + 1)):
						var dx: float = x - cx
						var dy: float = y - cy
						if dx * dx + dy * dy <= radius_tiles * radius_tiles:
							grid[x][y] = -1


func _write_json(path: String, data: Dictionary) -> void:
	var json_str: String = JSON.stringify(data, "  ")
	var file: FileAccess = FileAccess.open(path, FileAccess.WRITE)
	if file == null:
		push_error("ExportMapData: Cannot write to %s" % path)
		return
	file.store_string(json_str)
	file.close()
	print("  Wrote: %s" % path)
