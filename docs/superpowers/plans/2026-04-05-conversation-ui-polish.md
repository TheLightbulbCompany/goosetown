# Conversation UI Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Polish the conversation experience: comic-style speech bubbles, name-on-hover, agents face each other, talking animation.

**Architecture:** Godot scene restructure for bubble UI + hover detection, backend sends facing/activity commands during conversations, game server handles set_facing command.

**Tech Stack:** GDScript (Godot 4.6), Python (FastAPI backend), pytest

---

### File Map

- **Create:** `apps/godot/assets/speech_bubble.png` — 9-patch bubble texture
- **Modify:** `apps/godot/scenes/agent.tscn` — restructure speech bubble, add Area2D for hover
- **Modify:** `apps/godot/scripts/agent.gd` — hover logic, bubble management, talking animation mapping
- **Modify:** `apps/godot/scripts/server/agent_controller.gd` — set_facing command
- **Modify:** `apps/godot/scripts/server/apartment_agent_controller.gd` — set_facing command
- **Modify:** `apps/backend/routers/town.py` — facing + activity on chat/say/end
- **Modify:** `apps/frontend/public/godot/goosetown.pck` — re-export

---

### Task 1: Create Speech Bubble Asset

**Files:**
- Create: `apps/godot/assets/speech_bubble.png`

- [ ] **Step 1: Generate a 9-patch speech bubble PNG**

Create a small pixel art speech bubble texture. This should be a white rounded rectangle with a 1px dark border and a small triangular tail at the bottom center. Size: approximately 32x28 pixels (the NinePatchRect will stretch it). The tail should be ~6px wide and ~4px tall, centered at the bottom.

Use Python PIL to generate it programmatically:

```python
from PIL import Image, ImageDraw

# Create a 32x28 pixel bubble
img = Image.new("RGBA", (32, 28), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)

# White rounded rectangle body (top 24px)
# Border
draw.rounded_rectangle([0, 0, 31, 23], radius=4, fill=(255, 255, 255, 255), outline=(60, 60, 60, 255), width=1)

# Triangle tail at bottom center
tail_points = [(13, 23), (19, 23), (16, 27)]
draw.polygon(tail_points, fill=(255, 255, 255, 255))
# Tail border lines (left and right edges only)
draw.line([(13, 23), (16, 27)], fill=(60, 60, 60, 255), width=1)
draw.line([(19, 23), (16, 27)], fill=(60, 60, 60, 255), width=1)

img.save("apps/godot/assets/speech_bubble.png")
```

Run from the repo root: `python3 -c "<above code>"`

- [ ] **Step 2: Verify the asset**

Open the PNG and confirm it looks like a white speech bubble with a small tail pointing down.

- [ ] **Step 3: Commit**

```bash
git add apps/godot/assets/speech_bubble.png
git commit -m "feat: add 9-patch speech bubble texture asset

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Restructure Agent Scene — Speech Bubble + Hover Area

**Files:**
- Modify: `apps/godot/scenes/agent.tscn`

This task modifies the Godot scene file directly. The scene is a text-based `.tscn` format.

- [ ] **Step 1: Update agent.tscn**

Replace the entire file content with:

```
[gd_scene load_steps=4 format=3]

[ext_resource type="Script" path="res://scripts/agent.gd" id="1_agent"]
[ext_resource type="Texture2D" path="res://assets/speech_bubble.png" id="2_bubble"]

[sub_resource type="CircleShape2D" id="CircleShape2D_1"]
radius = 10.0

[node name="Agent" type="CharacterBody2D"]
collision_layer = 1
collision_mask = 2
script = ExtResource("1_agent")

[node name="Sprite" type="Sprite2D" parent="."]
position = Vector2(0, -16)

[node name="NameLabel" type="Label" parent="."]
visible = false
offset_left = -40.0
offset_top = -50.0
offset_right = 40.0
offset_bottom = -35.0
horizontal_alignment = 1
text = "Agent"

[node name="SpeechBubbleContainer" type="NinePatchRect" parent="."]
visible = false
offset_left = -60.0
offset_top = -90.0
offset_right = 60.0
offset_bottom = -55.0
texture = ExtResource("2_bubble")
patch_margin_left = 6
patch_margin_top = 6
patch_margin_right = 6
patch_margin_bottom = 8

[node name="SpeechText" type="Label" parent="SpeechBubbleContainer"]
layout_mode = 1
anchors_preset = 15
anchor_right = 1.0
anchor_bottom = 1.0
offset_left = 4.0
offset_top = 2.0
offset_right = -4.0
offset_bottom = -6.0
horizontal_alignment = 1
vertical_alignment = 1
autowrap_mode = 2
text = ""

[node name="HoverArea" type="Area2D" parent="."]
collision_layer = 0
collision_mask = 0
input_pickable = true

[node name="HoverShape" type="CollisionShape2D" parent="HoverArea"]
position = Vector2(0, -16)
shape = SubResource("CircleShape2D_1")

[node name="CollisionShape2D" type="CollisionShape2D" parent="."]
shape = SubResource("CircleShape2D_1")

[node name="NavigationAgent2D" type="NavigationAgent2D" parent="."]
path_desired_distance = 4.0
target_desired_distance = 16.0
avoidance_enabled = true
radius = 16.0
neighbor_distance = 100.0
max_neighbors = 5
max_speed = 100.0
```

Key changes from original:
- `NameLabel` starts with `visible = false` (hover-only)
- `SpeechBubble` Label replaced with `SpeechBubbleContainer` (NinePatchRect) + `SpeechText` (Label child)
- NinePatchRect uses the bubble texture with 9-patch margins
- SpeechText has word wrap (`autowrap_mode = 2`), centered, with padding offsets
- Added `HoverArea` (Area2D) with `input_pickable = true` for mouse detection
- `HoverShape` reuses the same CircleShape2D as the physics collision

- [ ] **Step 2: Commit**

```bash
git add apps/godot/scenes/agent.tscn
git commit -m "feat: restructure agent scene — NinePatchRect bubble, hover area

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Update agent.gd — Hover, Bubble, Talking

**Files:**
- Modify: `apps/godot/scripts/agent.gd`

- [ ] **Step 1: Update node references**

Find the `@onready` declarations at the top of agent.gd and replace:

```gdscript
@onready var sprite: Sprite2D = $Sprite
@onready var name_label: Label = $NameLabel
@onready var speech_bubble: Label = $SpeechBubble
@onready var nav_agent: NavigationAgent2D = $NavigationAgent2D
```

With:

```gdscript
@onready var sprite: Sprite2D = $Sprite
@onready var name_label: Label = $NameLabel
@onready var speech_container: NinePatchRect = $SpeechBubbleContainer
@onready var speech_text_label: Label = $SpeechBubbleContainer/SpeechText
@onready var nav_agent: NavigationAgent2D = $NavigationAgent2D
@onready var hover_area: Area2D = $HoverArea
```

- [ ] **Step 2: Add hover signals in _ready**

In the `_ready()` function, after `nav_agent.velocity_computed.connect(...)`, add:

```gdscript
	hover_area.mouse_entered.connect(_on_hover_enter)
	hover_area.mouse_exited.connect(_on_hover_exit)
```

- [ ] **Step 3: Add hover handler functions**

Add these functions after `_ready()`:

```gdscript
func _on_hover_enter() -> void:
	if speech_text == "":
		name_label.visible = true


func _on_hover_exit() -> void:
	name_label.visible = false
```

- [ ] **Step 4: Update update_state to use new nodes**

Replace the existing `update_state` function:

```gdscript
func update_state(data: Dictionary) -> void:
	var text: String = str(data.get("speechText", ""))
	if text != "" and text != speech_text:
		speech_text = text
		speech_text_label.text = speech_text
		speech_container.visible = true
		name_label.visible = false  # Hide name when speaking
	elif text == "":
		speech_text = ""
		speech_container.visible = false
		# Name stays hidden until next hover
```

- [ ] **Step 5: Add "talking" to ACTIVITY_ANIMATION**

Find the `ACTIVITY_ANIMATION` dictionary and add after the last entry (before the closing `}`):

```gdscript
	"talking": "talking",
```

- [ ] **Step 6: Update setup() to use name_label**

The `setup()` function references `name_label` — verify it still works. The node name is still `NameLabel` so `$NameLabel` still resolves. No change needed.

- [ ] **Step 7: Commit**

```bash
git add apps/godot/scripts/agent.gd
git commit -m "feat: agent hover-to-show-name, comic speech bubble, talking animation mapping

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Game Server — set_facing Command

**Files:**
- Modify: `apps/godot/scripts/server/agent_controller.gd`
- Modify: `apps/godot/scripts/server/apartment_agent_controller.gd`

- [ ] **Step 1: Add set_facing to town agent_controller.gd**

In `handle_command()`, add a new match arm before `_:`:

```gdscript
		"set_facing":
			var direction: String = str(data.get("direction", ""))
			if direction in ["south", "west", "east", "north"]:
				var agent_node: CharacterBody2D = _agents[peer_id]
				agent_node.facing = direction
```

- [ ] **Step 2: Add set_facing to apartment_agent_controller.gd**

In `handle_command()`, add the same match arm before `_:`:

```gdscript
		"set_facing":
			var direction: String = str(data.get("direction", ""))
			if direction in ["south", "west", "east", "north"]:
				var agent_node: CharacterBody2D = _agents[peer_id]
				agent_node.facing = direction
```

- [ ] **Step 3: Commit**

```bash
git add apps/godot/scripts/server/agent_controller.gd apps/godot/scripts/server/apartment_agent_controller.gd
git commit -m "feat: set_facing command in game server controllers

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Backend — Facing + Activity on Conversation Events

**Files:**
- Modify: `apps/backend/routers/town.py`

- [ ] **Step 1: Add a helper to calculate facing direction**

Add this function near the top of `routers/town.py`, after the imports:

```python
def _facing_toward(from_state: dict, to_state: dict) -> str:
    """Calculate which direction 'from' should face to look at 'to'.

    Uses cached agent_state positions from the game server.
    Returns 'east', 'west', 'north', or 'south'.
    """
    from_pos = from_state.get("position", {})
    to_pos = to_state.get("position", {})
    dx = to_pos.get("x", 0) - from_pos.get("x", 0)
    dy = to_pos.get("y", 0) - from_pos.get("y", 0)

    if abs(dx) > abs(dy):
        return "east" if dx > 0 else "west"
    else:
        return "south" if dy > 0 else "north"
```

- [ ] **Step 2: Add facing + talking to _handle_chat**

In `_handle_chat()`, find the block where we send `set_speech` (added in the previous sprint). Right after it, add:

```python
    # Face agents toward each other and start talking animation
    try:
        gs_client = get_game_server_client()
        initiator_state = gs_client.get_cached_agent_state(initiator_id)
        target_state = gs_client.get_cached_agent_state(target_id)
        if initiator_state and target_state:
            init_facing = _facing_toward(initiator_state, target_state)
            target_facing = _facing_toward(target_state, initiator_state)
            await gs_client.send_command(initiator_id, {"cmd": "set_facing", "direction": init_facing})
            await gs_client.send_command(target_id, {"cmd": "set_facing", "direction": target_facing})
        await gs_client.send_command(initiator_id, {"cmd": "set_activity", "activity": "talking"})
    except Exception:
        pass  # Cosmetic, don't fail conversation
```

- [ ] **Step 3: Add talking activity to _handle_say**

In `_handle_say()`, find the block where we send `set_speech`. Right after it, add:

```python
    # Set talking activity on the speaker
    try:
        gs_client = get_game_server_client()
        await gs_client.send_command(speaker_id, {"cmd": "set_activity", "activity": "talking"})
    except Exception:
        pass
```

- [ ] **Step 4: Clear talking activity on conversation end**

In `_finalize_conversation()`, after the mood/relationship effects loop (around line 1440+), add:

```python
    # Clear talking activity for both participants
    gs_client = get_game_server_client()
    for agent_id in (a_id, b_id):
        if not agent_id:
            continue
        try:
            await gs_client.send_command(agent_id, {"cmd": "set_activity", "activity": ""})
            await gs_client.send_command(agent_id, {"cmd": "set_speech", "text": ""})
        except Exception:
            pass
```

- [ ] **Step 5: Run tests**

Run: `cd apps/backend && uv run pytest tests/ -q`

Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
git add apps/backend/routers/town.py
git commit -m "feat: face agents toward each other and set talking activity during conversations

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Export and Push

- [ ] **Step 1: Export Godot .pck**

Open Godot, export Web (Runnable) to `apps/frontend/public/godot/`. Exclude `.godot/*` and `exported/*` in Resources tab. Delete `.godot/imported` and `.godot/exported` first if export is slow.

- [ ] **Step 2: Commit and push**

```bash
git add apps/frontend/public/godot/goosetown.pck
git commit --amend --no-edit
git push origin main
```

- [ ] **Step 3: Test after deploy**

1. Hard refresh browser
2. Connect both agents to plaza
3. Verify agent names are hidden until you hover
4. Start conversation: `python3 goosetown_cli.py act chat test-fox "Hello!"`
5. Verify: comic-style bubble appears above captain-owl with "Hello!"
6. Verify: both agents face each other
7. Verify: captain-owl plays talking animation (or idle if talking anim not yet generated)
8. Reply from test-fox and verify bubble appears on test-fox
9. End conversation and verify bubbles clear and talking activity stops
