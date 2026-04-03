## Debug overlay: shows walkable (green) and blocked (red) tiles.
## Add as a child node of the scene root and toggle visibility.
## Press F3 at runtime to toggle.
@tool
extends Node2D

const TILE_SIZE: int = 32
const MAP_WIDTH: int = 44
const MAP_HEIGHT: int = 27

var grid: Array = []
var show_overlay: bool = true


func _ready() -> void:
	z_index = 100  # Draw on top of everything
	_load_grid()
	queue_redraw()


func _input(event: InputEvent) -> void:
	if event is InputEventKey and event.pressed and event.keycode == KEY_F3:
		show_overlay = !show_overlay
		queue_redraw()


func _draw() -> void:
	if not show_overlay or grid.is_empty():
		return

	for x in range(MAP_WIDTH):
		if x >= grid.size():
			break
		for y in range(MAP_HEIGHT):
			if y >= grid[x].size():
				break
			var color: Color
			if grid[x][y] == 0:
				color = Color(0, 1, 0, 0.15)  # green = walkable
			else:
				color = Color(1, 0, 0, 0.25)  # red = blocked
			draw_rect(Rect2(x * TILE_SIZE, y * TILE_SIZE, TILE_SIZE, TILE_SIZE), color)


func _load_grid() -> void:
	# Try loading from exported file
	var path: String = ProjectSettings.globalize_path("res://exported/walkability.json")
	if not FileAccess.file_exists(path):
		print("DebugWalkability: No walkability.json found")
		return

	var file: FileAccess = FileAccess.open(path, FileAccess.READ)
	var json: JSON = JSON.new()
	if json.parse(file.get_as_text()) != OK:
		return

	var data: Dictionary = json.data
	grid = data.get("grid", [])
	print("DebugWalkability: Loaded %dx%d grid" % [grid.size(), grid[0].size() if grid.size() > 0 else 0])
