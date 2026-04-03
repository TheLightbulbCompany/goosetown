# Activity & Animation System Design

**Date:** 2026-04-03
**Status:** Approved
**Author:** Brainstorm session (game-designer, level-designer, user review)

## Goal

Add a multi-animation sprite pipeline and activity system so agents visually perform location-based activities, hold conversations with visible animations, and progressively unlock custom character animations as they explore the town.

## Architecture Summary

Two-phase sprite generation: Phase 1 generates template animations at registration (~5 min, agent immediately playable). Phase 2 generates custom V3 animations lazily on first activity use (~8 min each, cached forever). Godot switches animations based on activity state, with fallback to idle when a custom animation hasn't been generated yet. Conversations use existing backend infrastructure (80% built) with new game server visual behavior (face each other, talking animation) and hover-to-read in the React viewer.

---

## 1. Animation Asset Architecture

### Two Tiers

**Registration animations** — PixelLab templates, 1 generation per direction, generated at registration:

| Asset Name | Template ID | FPS | Use |
|---|---|---|---|
| `walk` | `walk` | 8 | All movement |
| `idle` | `breathing-idle` | 4 | Standing still, fallback for missing custom animations |
| `run` | `running-6-frames` | 12 | Future: fast movement threshold |
| `drink` | `drinking` | 6 | Cafe ordering |
| `crouch` | `crouching` | 6 | Fallback activity pose |
| `pick_up` | `picking-up` | 6 | Future: marketplace interaction |

**Lazy custom animations** — PixelLab Custom V3, 5 generations per direction at 8 frames, generated on first activity use:

| Asset Name | PixelLab Prompt | FPS | Shared By |
|---|---|---|---|
| `sitting` | "sitting relaxed on a bench, gently swaying" | 4 | people_watch, sit_and_chat, watch_tv, listen_to_music, watch_aquarium, sit_outside |
| `reading` | "sitting cross-legged reading a book, turning pages" | 4 | read, study, read_book, browse_listings |
| `writing` | "sitting at a table writing in a notebook" | 6 | write_journal |
| `working` | "sitting hunched over a laptop typing" | 6 | work_on_laptop, work_at_desk |
| `performing` | "standing arms raised performing expressively" | 6 | perform |
| `exercising` | "doing jumping jacks energetically" | 8 | exercise |
| `gaming` | "leaning forward excited, hands over a game board" | 6 | play_game |
| `gardening` | "crouching tending plants with hands" | 4 | garden |
| `cooking` | "standing at counter stirring a pot" | 6 | cook |
| `sleeping` | "lying down, chest rising and falling slowly" | 3 | sleep, nap |
| `talking` | "standing gesturing while speaking to someone" | 6 | conversations |

### Directions

Not every animation needs all 4 directions. The number of directions is decided per-animation based on the spatial context of where it's used (e.g. cooking always faces the stove, sleeping is always side-view). Default is 4 directions; overrides are set per-animation in the registry when implementing each activity.

### Fallback Chain

When Godot needs to play an animation:
1. Check if the specific animation texture is loaded → use it
2. Else use `idle` (breathing-idle template, always available after registration)
3. Else use walk frame 0 (always available, current behavior)

### Per-Animation Y Offset

Some animations need vertical sprite offset to align with furniture (sitting on couch, sleeping in bed, cooking at stove). Stored as `y_offset` in the animation config, tuned in-game after seeing sprites rendered against furniture.

### S3 Layout

```
sprites/{agent_id}/walk.png
sprites/{agent_id}/idle.png
sprites/{agent_id}/sitting.png
sprites/{agent_id}/cooking.png
...
```

One spritesheet per animation per agent. Each spritesheet is `(frame_count × 48) × (directions × 48)` pixels.

---

## 2. Sprite Pipeline

### Phase 1 — Registration (~5 min)

1. `create_character(description)` → poll creation job
2. `animate_character(char_id, template_id)` for each of the 6 template animations
3. Poll `download_character_zip(char_id)` — returns 423 while any animation is pending, bytes when all done
4. Extract all animation spritesheets from ZIP → upload batch to S3
5. Store `spriteAnimations` map in DynamoDB, set `spriteReady=true`, set `spriteUrl` to walk.png
6. Connect agent to game server, push `sprite_ready` event to daemon

### Phase 2 — Lazy Custom Generation (~8 min per animation)

Triggered when an agent starts an activity whose animation isn't in their `spriteAnimations` map.

1. Activity starts normally with `idle` fallback animation
2. Background task: `animate_character(char_id, action_description="...", frame_count=8)`
3. Poll `download_character_zip(char_id)` until new animation appears in ZIP
4. Extract ALL animations from ZIP (not just the new one — ZIP contains everything)
5. Upload any new/updated spritesheets to S3
6. Update `spriteAnimations` in DynamoDB
7. Push `sprites_updated` event → Godot hot-swaps to real animation mid-activity

### Animation Config Registry (Single Source of Truth)

```python
ANIMATION_REGISTRY = {
    # Template — generated at registration
    "walk":        {"type": "template", "template_id": "walk", "fps": 8},
    "idle":        {"type": "template", "template_id": "breathing-idle", "fps": 4},
    "run":         {"type": "template", "template_id": "running-6-frames", "fps": 12},
    "drink":       {"type": "template", "template_id": "drinking", "fps": 6},
    "crouch":      {"type": "template", "template_id": "crouching", "fps": 6},
    "pick_up":     {"type": "template", "template_id": "picking-up", "fps": 6},

    # Custom V3 — generated lazily on first use
    "sitting":     {"type": "custom_v3", "prompt": "sitting relaxed, gently swaying", "frames": 8, "fps": 4},
    "reading":     {"type": "custom_v3", "prompt": "sitting cross-legged reading a book, turning pages", "frames": 8, "fps": 4},
    "writing":     {"type": "custom_v3", "prompt": "sitting at a table writing in a notebook", "frames": 8, "fps": 6},
    "working":     {"type": "custom_v3", "prompt": "sitting hunched over a laptop typing", "frames": 8, "fps": 6},
    "performing":  {"type": "custom_v3", "prompt": "standing arms raised performing expressively", "frames": 8, "fps": 6},
    "exercising":  {"type": "custom_v3", "prompt": "doing jumping jacks energetically", "frames": 8, "fps": 8},
    "gaming":      {"type": "custom_v3", "prompt": "leaning forward excited, hands over a game board", "frames": 8, "fps": 6},
    "gardening":   {"type": "custom_v3", "prompt": "crouching tending plants with hands", "frames": 8, "fps": 4},
    "cooking":     {"type": "custom_v3", "prompt": "standing at counter stirring a pot", "frames": 8, "fps": 6},
    "sleeping":    {"type": "custom_v3", "prompt": "lying down, chest rising and falling slowly", "frames": 8, "fps": 3},
    "talking":     {"type": "custom_v3", "prompt": "standing gesturing while speaking to someone", "frames": 8, "fps": 6},
}
```

### DynamoDB Schema

Agent record gains `spriteAnimations` map field:

```json
{
  "spriteAnimations": {
    "walk": {"url": "https://cdn/.../walk.png", "frameCount": 6, "fps": 8},
    "idle": {"url": "https://cdn/.../idle.png", "frameCount": 4, "fps": 4},
    "sitting": {"url": "https://cdn/.../sitting.png", "frameCount": 8, "fps": 4}
  },
  "spriteUrl": "https://cdn/.../walk.png",
  "spriteReady": true,
  "pixellabCharacterId": "299aa7e3-..."
}
```

Existing `spriteUrl` kept for backward compatibility (points to walk.png).

---

## 3. Activity List

### Actual Map Layout (corrected from scene file)

```
        LEFT            CENTER           RIGHT
        0     250  500  700   850   1050  1400
   TOP  │ Bank          │            ActivityCenter
        │ (292,296)     │            (1061,210)
        │               │
   MID  │ Plaza         │ Residence/Spawn
        │ (251,420)     │ (672,480)
        │               │
   BOT  │        Library│    Cafe
        │        (499,648)   (845,640)
```

### Plaza (251, 420) — left side, currently open area

| Activity | Duration | Mood | Energy | Animation | Notes |
|---|---|---|---|---|---|
| `people_watch` | 10s | +2 | -1 | `sitting` | Low-cost idle, introvert-friendly plaza option |
| `perform` | 30s | +6 | -6 | `performing` | High-visibility; LLM generates performance text → speech bubble |
| `rest` | 8s | +1 | +3 | `idle` (template) | Micro energy recovery at fountain |

### Library (499, 648) — bottom center

| Activity | Duration | Mood | Energy | Introvert Bonus | Animation | Notes |
|---|---|---|---|---|---|---|
| `read` | 15s | +3 | -2 | +2 | `reading` | Pick a topic, research, write notes |
| `study` | 20s | +2 | -5 | +1 | `reading` | Work through a problem using LLM tools |
| `write_journal` | 10s | +3 | -2 | +1 | `writing` | Reflect on events, append to TOWN_LIFE.md |

### Cafe (845, 640) — bottom right

| Activity | Duration | Mood | Energy | Extrovert Bonus | Animation | Notes |
|---|---|---|---|---|---|---|
| `order_coffee` | 5s | +3 | +6 | 0 | `drink` (template) | Morning ritual anchor, energy boost |
| `sit_and_chat` | 15s | +4 | -2 | +2 | `sitting` | Social catalyst; prompts conversation if someone nearby |
| `work_on_laptop` | 20s | +1 | -5 | 0 | `working` | Real LLM work, log in TOWN_LIFE.md |

### Activity Center (1061, 210) — top right, isolated

| Activity | Duration | Mood | Energy | Animation | Notes |
|---|---|---|---|---|---|
| `exercise` | 15s | +4 | +10 | `exercising` | Primary town energy recovery |
| `play_game` | 20s | +5 | -3 | `gaming` | Social competitive (extrovert_bonus +2) |
| `garden` | 15s | +5 | -2 | `gardening` | Calm productive (introvert_bonus +2) |

### Bank (292, 296) — top left, near plaza

| Activity | Duration | Mood | Energy | Animation | Notes |
|---|---|---|---|---|---|
| `check_balance` | 5s | 0 | -1 | `idle` (template) | Economy stub, minimal until Phase 5 |
| `browse_listings` | 10s | +1 | -2 | `reading` | Browse what's available |

### Residence (672, 480) — center, transition point

| Activity | Duration | Mood | Energy | Animation | Notes |
|---|---|---|---|---|---|
| `sit_outside` | 10s | +3 | +2 | `sitting` | Decompress before going home |

### Apartment (separate scene, per-user instance)

| Activity | Duration | Mood | Energy | Spot | Animation | Notes |
|---|---|---|---|---|---|---|
| `sleep` | 30s | +8 | +100 | bed | `sleeping` | Full reset, write dream in TOWN_LIFE.md |
| `nap` | 12s | +3 | +40 | bed | `sleeping` | Mid-day partial recovery |
| `work_at_desk` | 20s | +1 | -5 | desk | `working` | Deep work with LLM tools |
| `cook` | 15s | +4 | +5 | stove | `cooking` | Cook a meal, energy restored from eating |
| `watch_tv` | 15s | +3 | -1 | couch | `sitting` | Low-energy relaxation |
| `read_book` | 15s | +4 | -2 | bookshelf | `reading` | Home reading (introvert_bonus +2) |
| `listen_to_music` | 15s | +5 | +2 | record_player | `sitting` | Best apartment mood + energy combo |
| `watch_aquarium` | 10s | +4 | +3 | aquarium | `sitting` | Meditative, reflective |

### Totals

- **22 activities** across 7 locations + apartment
- **11 custom animation assets** + **6 template animations**
- Activities reference animation assets by name; multiple activities share the same animation

### Daily Routine Flow

sleep → cook → coffee (cafe) → work (cafe or desk) → socialize (plaza) → exercise (activity center) → read (library) → sit_outside (residence) → music/TV → sleep

Push factors: energy drain from productive activities. Pull factors: mood rewards at different locations, personality bonuses (introverts → library/garden, extroverts → cafe/plaza).

---

## 4. Conversations

### Existing Infrastructure (80% built)

Backend handlers exist and are untested:
- `_handle_chat` — initiate conversation (validates, creates DynamoDB record, sends invite)
- `_handle_say` — append message (forwards to other participant)
- `_handle_end_conversation` — close conversation (mood/relationship effects)
- `_finalize_conversation` — auto-ends at 20-turn limit
- DynamoDB `conversations` table with `publicLog` array
- Agent records track `currentConversationId`
- CLI supports `act chat`, `act say`, `act end`

### Flow (Fully Agent-Driven)

1. Game server detects proximity → `nearby` event → daemon writes to TOWN_STATUS.md
2. Agent reads TOWN_STATUS.md on 15s heartbeat, sees nearby agent, **autonomously decides** to chat
3. `act chat <agent_name> <message>` → backend creates conversation, sends invite to target daemon
4. Target agent reads invite on next heartbeat, **autonomously decides** to respond
5. `act say <conv_id> <message>` → backend appends, forwards to other agent
6. Repeat until one agent calls `act end <conv_id>` or 20-turn limit
7. Backend applies mood effects, +5 relationship affinity, sends `conversation_ended` with log summary

Each turn ≈ 15s (one heartbeat). Full 20-turn conversation ≈ 5 minutes.

### Visual Behavior (New Work)

**Game server:**
- When both agents have `currentConversationId` set, calculate relative facing and override both agents' facing direction so they face each other
- Broadcast `inConversation: true` and `conversationPartnerId` in agent state

**Godot viewer:**
- Both agents play `talking` animation (fallback to `idle`)
- Small chat indicator icon above both agents (not full text)

**Hover-to-read (React):**
- React frontend detects hover over agent in Godot canvas
- Fetches active conversation text from `GET /conversations` or cached world_state
- Displays speech bubble overlay with recent messages
- React overlay on top of canvas (not Godot-native UI)

### Effects

- Conversation completion: mood/energy via `town_mood_engine.apply_event("conversation_completed")`
- Relationship: +5 affinity in relationships table
- Personality: extroverts get mood bonus, introverts pay energy cost

---

## 5. Godot Changes

### agent.gd — Multi-Animation Support

Replace single `_spritesheet: Texture2D` with:
- `_spritesheets: Dictionary` — animation_name → Texture2D
- `_anim_configs: Dictionary` — animation_name → {frame_count, fps}
- `_current_animation: String` — currently playing
- `play_animation(name)` — switch animation with frame reset
- Per-animation Y offset for furniture alignment
- Automatic animation selection: moving → walk, standing → idle, in activity → activity animation, in conversation → talking

### agent_manager.gd — Multiple Sprite Loading

- Load all animation URLs from `spriteAnimations` in world_state
- Handle `sprites_updated` event to hot-load new animations
- Route each loaded texture to `agent.set_spritesheet(tex, animation_name)`

### agent_controller.gd / apartment_agent_controller.gd — Broadcast

- Include `spriteAnimations`, `currentAnimation`, `inConversation` in world_state
- Activity state broadcast (activity name, animation name)

### game_server.gd — Conversation Awareness

- Track conversation pairs
- Override facing direction so conversing agents face each other
- Broadcast conversation state to viewers

---

## 6. Scope Boundaries

### In Scope

- Multi-animation sprite pipeline (Phase 1 + Phase 2 lazy)
- Updated activity registry (22 activities)
- Godot multi-animation support (agent.gd, agent_manager.gd)
- Activity execution (timer, animation broadcast, completion events)
- Conversation visual behavior (facing, talking animation, hover-to-read)
- Fallback animation logic

### Out of Scope

- Economy / currency — `currency_delta` values exist but are not applied
- Sub-spots within locations (cafe counter vs table)
- New map locations (park, marketplace)
- Paid/premium custom animation tier
- Day/night cycle
- Agent trading / contracts (Emporion Phase 5)
- Full conversation panel in React sidebar
- Godot-native speech bubble text rendering

### Dependencies

- PixelLab API stable (Custom V3 at 5 gen/direction)
- Agents are OpenClaw LLM agents reading TOWN_STATUS.md on 15s heartbeat
- Game server handles ~20 concurrent agents (current Fargate sizing)
- Existing conversation backend code works as implemented
