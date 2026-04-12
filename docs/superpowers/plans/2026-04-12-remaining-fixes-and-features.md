# Remaining Fixes + Features Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix remaining bugs from the Friends test (shadow sprite fallback, frontend sync), finish unimplemented items (infra, nearby_conversations, group facing), and build two new features (event-driven think, sprite queue).

**Architecture:** 8 independent tasks touching backend, daemon, Godot, frontend, and infra. Each task is independently testable and committable.

**Tech Stack:** Python 3.12 / FastAPI / GDScript 4.6 / React 18 / TypeScript / AWS CDK

---

### Task 1: Shadow Sprite Fallback

**Files:**
- Create: `apps/godot/assets/sprites/shadow_placeholder.png` (generated via script)
- Modify: `apps/godot/scripts/agent.gd`
- Modify: `apps/backend/routers/town.py:1832-1852`

- [ ] **Step 1: Generate shadow sprite PNG**

Create a Python script to generate the shadow placeholder. Run it once to produce the asset:

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown
python3 -c "
from PIL import Image, ImageDraw

FRAME = 96
COLS, ROWS = 6, 4
img = Image.new('RGBA', (COLS * FRAME, ROWS * FRAME), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)

for row in range(ROWS):
    for col in range(COLS):
        cx = col * FRAME + FRAME // 2
        cy = row * FRAME + FRAME // 2
        # Dark semi-transparent humanoid silhouette (oval body + circle head)
        draw.ellipse([cx-18, cy-10, cx+18, cy+30], fill=(30, 30, 40, 180))
        draw.ellipse([cx-12, cy-30, cx+12, cy-6], fill=(30, 30, 40, 180))

img.save('apps/godot/assets/sprites/shadow_placeholder.png')
print('Shadow sprite saved: 576x384')
"
```

- [ ] **Step 2: Add preloaded shadow texture to agent.gd**

In `apps/godot/scripts/agent.gd`, add after the existing constants (around line 17):

```gdscript
const SHADOW_TEXTURE: Texture2D = preload("res://assets/sprites/shadow_placeholder.png")
```

Then in the `setup` function (around line 199), after `_parse_facing(facing_json)`, change the sprite loading logic:

```gdscript
	_parse_facing(facing_json)

	if sprite_url != "" and not OS.has_feature("headless"):
		_load_sprite_from_url(sprite_url)
	elif not OS.has_feature("headless"):
		# No sprite URL — use shadow placeholder
		set_spritesheet(SHADOW_TEXTURE, "walk")

	_update_sprite_frame()
```

- [ ] **Step 3: Remove spriteReady gate on game server connection**

In `apps/backend/routers/town.py`, find the `town_agent_connect` handler (around line 1832). Change:

```python
    # Connect agent to game server if sprite is ready
    if agent.get("spriteReady"):
```

To:

```python
    # Connect agent to game server — use shadow placeholder if no custom sprite yet
    if True:  # Always connect, shadow sprite used as fallback in Godot
```

Actually, cleaner — just remove the `if` and dedent the block:

```python
    # Connect agent to game server (shadow sprite fallback if no custom sprite)
    gs_client = get_game_server_client()
    try:
        await gs_client.connect_agent(
            agent_id=agent_id,
            display_name=agent.get("displayName", agent_name),
            sprite_url=agent.get("spriteUrl", ""),
            location=current_location,
            context=location_context,
            user_id=user_id,
            sprite_animations=agent.get("spriteAnimations", {}),
            on_event=_make_agent_event_callback(),
        )
        logger.info(
            "Agent %s connected to game server (context=%s, location=%s, userId=%s)",
            agent_name, location_context, current_location, user_id[:12],
        )
    except Exception as e:
        logger.warning("Failed to connect agent %s to game server: %s", agent_name, e)
```

Also remove the `_sprite_keepalive` block that follows (lines 1874-1900+) — it's no longer needed since the agent will always have a game server connection sending `agent_state` events.

- [ ] **Step 4: Run tests**

Run: `cd apps/backend && uv run pytest tests/ -x -q`
Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add apps/godot/assets/sprites/shadow_placeholder.png apps/godot/scripts/agent.gd apps/backend/routers/town.py
git commit -m "feat: shadow sprite fallback — agents connect to game server immediately"
```

---

### Task 2: Frontend Header Bar Sync

**Files:**
- Modify: `apps/frontend/src/hooks/useTownState.ts:160-169`
- Modify: `apps/frontend/src/App.tsx`

- [ ] **Step 1: Dispatch custom event on context change**

In `apps/frontend/src/hooks/useTownState.ts`, in the `agent_context_changed` handler (around line 160), after calling `bridge.switchScene()`, dispatch a custom event so App.tsx can update the route:

```typescript
          if (msg.event === 'agent_context_changed') {
            console.log('[GooseTown] Context changed:', msg.context, 'for agent', msg.agent_id);
            try {
              const w = window as unknown as Record<string, unknown>;
              const bridge = w._godotBridge as { switchScene?: (name: string) => void } | undefined;
              if (bridge?.switchScene) {
                const newScene = msg.context === 'apartment' ? 'apartment' : 'town';
                bridge.switchScene(newScene);
                // Notify React to update route
                window.dispatchEvent(new CustomEvent('goosetown_scene_changed', {
                  detail: { scene: newScene },
                }));
              }
            } catch { /* Godot not loaded yet */ }
            return;
          }
```

- [ ] **Step 2: Listen for scene changes in App.tsx**

In `apps/frontend/src/App.tsx`, add a `useEffect` in the `App` component that listens for the custom event and navigates:

```typescript
import { Routes, Route, useLocation, useNavigate } from 'react-router-dom';

// ... inside App component:

  const navigate = useNavigate();

  // Sync React route with Godot scene changes (from WebSocket agent_context_changed)
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      const target = detail.scene === 'apartment' ? '/apartment' : '/';
      if (location.pathname !== target) {
        navigate(target);
      }
    };
    window.addEventListener('goosetown_scene_changed', handler);
    return () => window.removeEventListener('goosetown_scene_changed', handler);
  }, [location.pathname, navigate]);
```

- [ ] **Step 3: Verify frontend builds**

Run: `cd apps/frontend && npx tsc --noEmit`
Expected: No type errors.

- [ ] **Step 4: Commit**

```bash
git add apps/frontend/src/hooks/useTownState.ts apps/frontend/src/App.tsx
git commit -m "fix: sync React route with Godot scene switches via custom event"
```

---

### Task 3: "View in Town" Button Fix

**Files:**
- Modify: `apps/frontend/src/components/ApartmentCard.tsx:121-130`

- [ ] **Step 1: Fix the Link behavior**

In `apps/frontend/src/components/ApartmentCard.tsx`, the "View in town" link (line 123-128) navigates to `/?focus=...` which should switch the Godot scene to town and focus the camera on the agent. The issue is it only sets the URL — it doesn't trigger a Godot scene switch.

Replace the Link with an onClick handler that switches scenes AND navigates:

```typescript
      {agent.location_context === 'town' && (
        <div className="flex justify-end mt-1">
          <button
            onClick={() => {
              // Switch Godot scene to town
              const bridge = (window as any)._godotBridge;
              if (bridge?.switchScene) {
                bridge.switchScene('town');
              }
              // Navigate React router
              window.location.href = `/?focus=${encodeURIComponent(agent.display_name)}`;
            }}
            className="font-body text-xs text-clay-300 hover:text-brown-200 transition-colors bg-transparent border-none cursor-pointer"
          >
            View in town &rarr;
          </button>
        </div>
      )}
```

- [ ] **Step 2: Verify frontend builds**

Run: `cd apps/frontend && npx tsc --noEmit`
Expected: No type errors.

- [ ] **Step 3: Commit**

```bash
git add apps/frontend/src/components/ApartmentCard.tsx
git commit -m "fix: View in Town button switches Godot scene and navigates"
```

---

### Task 4: nearby_conversations in World Update

**Files:**
- Modify: `apps/backend/routers/town.py` (in `_on_game_server_event` callback and `_handle_agent_state` enrichment)
- Modify: `apps/backend/core/services/town_service.py` (add helper)

- [ ] **Step 1: Add get_nearby_conversations helper to TownService**

In `apps/backend/core/services/town_service.py`, add a new method:

```python
    def get_nearby_conversations(
        self, nearby_agent_ids: list[str], exclude_conv_id: str | None = None
    ) -> list[dict]:
        """Find active conversations that any of the nearby agents are in.

        Returns a list of conversation summaries for display in TOWN_STATUS.md.
        Excludes the conversation the requesting agent is already in.
        """
        conversations: list[dict] = []
        seen_conv_ids: set[str] = set()
        for agent_id in nearby_agent_ids:
            agent = self.get_town_agent_by_id(agent_id)
            if not agent:
                continue
            conv_id = agent.get("currentConversationId")
            if not conv_id or conv_id == exclude_conv_id or conv_id in seen_conv_ids:
                continue
            seen_conv_ids.add(conv_id)
            conv = self.get_conversation_by_id(conv_id)
            if not conv or conv.get("status") != "active":
                continue
            participants: list[str] = conv.get("participants") or [
                conv.get("participantAId"), conv.get("participantBId"),
            ]
            participant_names = []
            for pid in participants:
                if pid:
                    p = self.get_town_agent_by_id(pid)
                    if p:
                        participant_names.append(
                            p.get("displayName", p.get("agentName", pid[:8]))
                        )
            pub_log = conv.get("publicLog") or []
            last_msg = pub_log[-1] if pub_log else {}
            conversations.append({
                "conv_id": conv_id,
                "participants": participant_names,
                "last_message": last_msg,
                "location": conv.get("location", ""),
            })
        return conversations
```

- [ ] **Step 2: Enrich agent_state events with nearby_conversations**

In `apps/backend/routers/town.py`, in the `_send_to_daemon` function (around line 104-107), extend the `agent_state` enrichment:

```python
    if evt == "agent_state":
        payload = {**payload, "onboardingPhase": agent.get("onboardingPhase", "alive")}
        # Add nearby_conversations for agents not in a conversation
        nearby_agents = payload.get("nearby_agents", [])
        nearby_ids = [a.get("agent_id") for a in nearby_agents if a.get("agent_id")]
        own_conv = agent.get("currentConversationId")
        if nearby_ids and not own_conv:
            nearby_convs = service.get_nearby_conversations(nearby_ids)
            if nearby_convs:
                payload = {**payload, "nearby_conversations": nearby_convs}
```

Note: `service` is already available in scope since we call `service.get_town_agent_by_id(agent_id)` at line 94.

- [ ] **Step 3: Daemon stores nearby_conversations from agent_state**

In `apps/backend/data/goosetown-skill/goosetown_cli.py`, in `_handle_agent_state` (around line 1260), add after building `agent_info`:

```python
        # Store nearby_conversations for TOWN_STATUS.md rendering
        nearby_convs = data.get("nearby_conversations", [])
        if nearby_convs:
            self.state["nearby_conversations"] = nearby_convs
        elif "nearby_conversations" in self.state:
            del self.state["nearby_conversations"]
```

- [ ] **Step 4: Run tests**

Run: `cd apps/backend && uv run pytest tests/ -x -q`
Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/town_service.py apps/backend/routers/town.py apps/backend/data/goosetown-skill/goosetown_cli.py
git commit -m "feat: surface nearby_conversations in agent_state events for daemon"
```

---

### Task 5: Game Server Group Facing

**Files:**
- Modify: `apps/godot/scripts/server/agent_controller.gd`
- Modify: `apps/backend/routers/town.py` (in `_handle_chat` and `_handle_join_conversation`)

- [ ] **Step 1: Add face_agents_toward_centroid to agent_controller.gd**

In `apps/godot/scripts/server/agent_controller.gd`, add after the existing command handlers:

```gdscript
func face_agents_toward_centroid(agent_ids: Array) -> void:
	## Face each agent in the list toward the centroid of the others.
	if agent_ids.size() < 2:
		return
	var id_to_node: Dictionary = {}
	for aid in agent_ids:
		if _id_to_peer.has(aid):
			var peer: int = _id_to_peer[aid]
			if _agents.has(peer):
				id_to_node[aid] = _agents[peer]
	if id_to_node.size() < 2:
		return
	for aid in id_to_node:
		var agent_node: CharacterBody2D = id_to_node[aid]
		var centroid: Vector2 = Vector2.ZERO
		var count: int = 0
		for other_aid in id_to_node:
			if other_aid != aid:
				centroid += id_to_node[other_aid].global_position
				count += 1
		centroid /= count
		var delta: Vector2 = centroid - agent_node.global_position
		if abs(delta.x) > abs(delta.y):
			agent_node.facing = "east" if delta.x > 0 else "west"
		else:
			agent_node.facing = "south" if delta.y > 0 else "north"
```

Also add a command handler in the `handle_command` match block:

```gdscript
		"set_conversation_group":
			var ids: Array = data.get("agent_ids", [])
			face_agents_toward_centroid(ids)
```

- [ ] **Step 2: Send set_conversation_group from backend**

In `apps/backend/routers/town.py`, in `_handle_chat` after creating the conversation (around line 1296), add:

```python
    # Face agents toward each other
    try:
        gs_client = get_game_server_client()
        await gs_client.send_command(initiator_id, {
            "cmd": "set_conversation_group",
            "agent_ids": [initiator_id, target_id],
        })
    except Exception:
        pass  # Cosmetic
```

Also in `_handle_join_conversation`, after the joiner is added (around line 1535), add:

```python
    # Update group facing for all participants
    try:
        gs_client = get_game_server_client()
        for pid in participants:
            try:
                await gs_client.send_command(pid, {
                    "cmd": "set_conversation_group",
                    "agent_ids": participants,
                })
            except Exception:
                pass
    except Exception:
        pass  # Cosmetic
```

- [ ] **Step 3: Commit**

```bash
git add apps/godot/scripts/server/agent_controller.gd apps/backend/routers/town.py
git commit -m "feat: group conversation facing — agents face toward centroid of participants"
```

---

### Task 6: Event-Driven Think Triggers

**Files:**
- Modify: `apps/backend/data/goosetown-skill/goosetown_cli.py:1233-1258`

- [ ] **Step 1: Add event-driven think override**

In `apps/backend/data/goosetown-skill/goosetown_cli.py`, replace the think logic in `_handle_agent_state` (lines 1248-1258):

```python
        import time

        activity = data.get("activity", "idle")
        speed = data.get("speed", 0)
        is_idle = activity in ("idle", "") and speed < 0.1

        now = time.time()

        # Event-driven think: trigger immediately on meaningful state changes
        nearby = data.get("nearby_agents", [])
        prev_nearby_ids = {a.get("agent_id") for a in self.state.get("nearby", []) if a.get("agent_id")}
        curr_nearby_ids = {a.get("agent_id") for a in nearby if a.get("agent_id")}
        nearby_changed = curr_nearby_ids != prev_nearby_ids

        # Also trigger on nearby_conversations appearing
        has_nearby_convs = bool(data.get("nearby_conversations"))

        # Think if: idle + throttle elapsed, OR meaningful event happened
        time_think = is_idle and (now - self._last_think_time) >= 12.0
        event_think = nearby_changed or has_nearby_convs

        think = time_think or (event_think and is_idle)

        if think:
            self._last_think_time = now
```

- [ ] **Step 2: Write test**

Add to `apps/backend/tests/unit/test_goosetown_cli.py`:

```python
class TestEventDrivenThink:
    """Think triggers immediately on proximity changes, not just time throttle."""

    def test_think_triggers_on_new_nearby_agent(self, daemon):
        daemon.state["agent"] = {"onboardingPhase": "alive", "location": "plaza"}
        daemon.state["nearby"] = []

        # First agent_state — no nearby agents, idle, should think (first time)
        daemon._handle_agent_state({
            "event": "agent_state",
            "location": "plaza",
            "activity": "idle",
            "speed": 0,
            "nearby_agents": [],
            "context": "town",
        })
        content1 = (daemon._workspace_path / "TOWN_STATUS.md").read_text()
        assert "act move" in content1  # Think prompt written

        # Reset think time to simulate 5s passing (under 12s throttle)
        import time
        daemon._last_think_time = time.time() - 5.0

        # New agent appears nearby — should trigger think immediately despite 5s < 12s
        daemon._handle_agent_state({
            "event": "agent_state",
            "location": "plaza",
            "activity": "idle",
            "speed": 0,
            "nearby_agents": [{"agent_id": "a1", "name": "Alice", "display_name": "Alice"}],
            "context": "town",
        })
        content2 = (daemon._workspace_path / "TOWN_STATUS.md").read_text()
        assert "Alice" in content2  # New think prompt with Alice nearby

    def test_no_think_when_nearby_unchanged(self, daemon):
        daemon.state["agent"] = {"onboardingPhase": "alive", "location": "plaza"}
        daemon.state["nearby"] = [{"agent_id": "a1"}]

        # agent_state with same nearby — should NOT think (under throttle)
        import time
        daemon._last_think_time = time.time() - 5.0
        daemon._handle_agent_state({
            "event": "agent_state",
            "location": "plaza",
            "activity": "idle",
            "speed": 0,
            "nearby_agents": [{"agent_id": "a1", "name": "Alice"}],
            "context": "town",
        })
        # TOWN_STATUS.md should not have been rewritten (no think)
        assert not (daemon._workspace_path / "TOWN_STATUS.md").exists()
```

- [ ] **Step 3: Run tests**

Run: `cd apps/backend && uv run pytest tests/unit/test_goosetown_cli.py -v`
Expected: All pass including new tests.

- [ ] **Step 4: Commit**

```bash
git add apps/backend/data/goosetown-skill/goosetown_cli.py apps/backend/tests/unit/test_goosetown_cli.py
git commit -m "feat: event-driven think triggers on proximity changes"
```

---

### Task 7: PixelLab Sprite Job Queue

**Files:**
- Create: `apps/backend/core/services/sprite_queue.py`
- Modify: `apps/backend/routers/town.py` (replace direct task creation)
- Modify: `apps/backend/main.py` (initialize queue in lifespan)

- [ ] **Step 1: Create SpriteQueue class**

Create `apps/backend/core/services/sprite_queue.py`:

```python
"""Bounded-concurrency sprite generation queue.

Prevents PixelLab API rate limiting by limiting concurrent jobs.
Phase A+B (character creation + static sprite) runs at concurrency 3.
Phase C (animations) runs at concurrency 2.
Failed jobs retry up to 3 times with exponential backoff.
"""

import asyncio
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class SpriteJob:
    agent_id: str
    display_name: str
    appearance: str
    retries: int = 0
    max_retries: int = 3


class SpriteQueue:
    """Bounded-concurrency queue for sprite generation jobs."""

    def __init__(self, max_concurrent: int = 3):
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._queue: asyncio.Queue[SpriteJob] = asyncio.Queue()
        self._active_jobs: set[str] = set()
        self._worker_task: asyncio.Task | None = None

    def start(self) -> None:
        """Start the queue worker."""
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._worker())
            logger.info("SpriteQueue worker started (max_concurrent=%d)", self._semaphore._value)

    async def stop(self) -> None:
        """Stop the queue worker."""
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None

    def enqueue(self, agent_id: str, display_name: str, appearance: str) -> int:
        """Add a sprite generation job to the queue.

        Returns the queue position (0 = will start immediately).
        """
        if agent_id in self._active_jobs:
            logger.info("SpriteQueue: agent %s already queued, skipping", agent_id[:8])
            return -1
        job = SpriteJob(agent_id=agent_id, display_name=display_name, appearance=appearance)
        self._queue.put_nowait(job)
        self._active_jobs.add(agent_id)
        position = self._queue.qsize() - 1
        logger.info("SpriteQueue: enqueued agent %s (position %d)", agent_id[:8], position)
        return position

    async def _worker(self) -> None:
        """Process jobs from the queue with bounded concurrency."""
        while True:
            job = await self._queue.get()
            asyncio.create_task(self._process_job(job))

    async def _process_job(self, job: SpriteJob) -> None:
        """Process a single sprite generation job with semaphore throttling."""
        async with self._semaphore:
            try:
                from routers.town import _sprite_pipeline_task

                logger.info(
                    "SpriteQueue: starting job for %s (attempt %d/%d)",
                    job.agent_id[:8], job.retries + 1, job.max_retries,
                )
                await _sprite_pipeline_task(job.agent_id, job.display_name, job.appearance)
                logger.info("SpriteQueue: completed job for %s", job.agent_id[:8])
            except Exception as exc:
                logger.error("SpriteQueue: job failed for %s: %s", job.agent_id[:8], exc)
                if job.retries < job.max_retries:
                    delay = 5 * (2 ** job.retries)
                    logger.info(
                        "SpriteQueue: retrying %s in %ds (attempt %d/%d)",
                        job.agent_id[:8], delay, job.retries + 1, job.max_retries,
                    )
                    await asyncio.sleep(delay)
                    job.retries += 1
                    self._queue.put_nowait(job)
                    return  # Don't remove from active_jobs
                else:
                    logger.error("SpriteQueue: giving up on %s after %d retries", job.agent_id[:8], job.max_retries)
            finally:
                self._active_jobs.discard(job.agent_id)


_sprite_queue: SpriteQueue | None = None


def get_sprite_queue() -> SpriteQueue:
    global _sprite_queue
    if _sprite_queue is None:
        _sprite_queue = SpriteQueue(max_concurrent=3)
    return _sprite_queue
```

- [ ] **Step 2: Replace direct task creation in register_agent**

In `apps/backend/routers/town.py`, in `register_agent` (around line 756), replace:

```python
                task = asyncio.create_task(_sprite_pipeline_task(_agent_id, _display_name, _appearance))
                _background_sprite_tasks.add(task)
                task.add_done_callback(_background_sprite_tasks.discard)
```

With:

```python
                from core.services.sprite_queue import get_sprite_queue
                queue = get_sprite_queue()
                position = queue.enqueue(_agent_id, _display_name, _appearance)
                if position > 0:
                    _send_to_daemon(
                        _agent_id,
                        {"event": "sprite_progress", "status": f"Queued for sprite generation (position {position})"},
                    )
```

- [ ] **Step 3: Start queue in main.py lifespan**

In `apps/backend/main.py`, in the lifespan startup, add:

```python
    from core.services.sprite_queue import get_sprite_queue
    sprite_queue = get_sprite_queue()
    sprite_queue.start()
```

And in shutdown:

```python
    await sprite_queue.stop()
```

- [ ] **Step 4: Run tests**

Run: `cd apps/backend && uv run pytest tests/ -x -q`
Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/sprite_queue.py apps/backend/routers/town.py apps/backend/main.py
git commit -m "feat: sprite job queue with bounded concurrency and retry"
```

---

### Task 8: D10 — Infra for WS_LAMBDA_SECRET

**Files:**
- Modify: `apps/infra/lib/stacks/auth-stack.ts`
- Modify: `apps/infra/lib/stacks/compute-stack.ts`
- Modify: `apps/infra/lib/stacks/api-stack.ts`

- [ ] **Step 1: Add WS_LAMBDA_SECRET to AuthStack**

In `apps/infra/lib/stacks/auth-stack.ts`, find where other secrets are created. Add:

```typescript
this.wsLambdaSecret = new secretsmanager.Secret(this, 'WsLambdaSecret', {
  secretName: `goosetown/${props.environment}/ws_lambda_secret`,
  description: 'Shared secret for WebSocket Lambda → backend /ws-message auth',
  generateSecretString: {
    passwordLength: 64,
    excludePunctuation: true,
  },
  removalPolicy: props.environment === 'prod' ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
});
```

Add `wsLambdaSecret` as a public property on the stack class (follow the pattern of existing secrets).

- [ ] **Step 2: Inject into backend container**

In `apps/infra/lib/stacks/compute-stack.ts`, where secrets are injected into the task definition, add:

```typescript
WS_LAMBDA_SECRET: ecs.Secret.fromSecretsManager(props.authStack.wsLambdaSecret),
```

- [ ] **Step 3: Inject into Lambda and add to POST headers**

In `apps/infra/lib/stacks/api-stack.ts`, in the `$default` Lambda definition:

1. Add env var: `WS_LAMBDA_SECRET_ARN: props.authStack.wsLambdaSecret.secretArn`
2. Grant read: `props.authStack.wsLambdaSecret.grantRead(defaultLambda)`
3. In the inline Python handler code, read the secret at cold start and include it in the `X-WS-Lambda-Secret` header when POSTing to `/ws-message`:

```python
# At module level (cold start)
_ws_secret = None
def _get_ws_secret():
    global _ws_secret
    if _ws_secret is None:
        import boto3
        arn = os.environ.get('WS_LAMBDA_SECRET_ARN', '')
        if arn:
            sm = boto3.client('secretsmanager')
            _ws_secret = sm.get_secret_value(SecretId=arn)['SecretString']
    return _ws_secret or ''

# In the handler, when building the POST request to /ws-message:
headers['X-WS-Lambda-Secret'] = _get_ws_secret()
```

- [ ] **Step 4: CDK synth**

Run: `cd apps/infra && rm -rf ../backend/.venv && npx cdk synth 2>&1 | tail -10`
Expected: No errors.

- [ ] **Step 5: Commit**

```bash
git add apps/infra/lib/stacks/auth-stack.ts apps/infra/lib/stacks/compute-stack.ts apps/infra/lib/stacks/api-stack.ts
git commit -m "feat: provision WS_LAMBDA_SECRET for /ws-message endpoint auth"
```

---

## Verification

After all 8 tasks, run full verification:

- [ ] `cd apps/backend && uv run pytest tests/ -v` — all tests pass
- [ ] `cd apps/backend && uv run ruff check .` — lint clean
- [ ] `cd apps/frontend && npx tsc --noEmit` — type check clean
- [ ] `cd apps/infra && npx cdk synth` — CDK synth clean
- [ ] Godot .pck export (manual — must include shadow sprite + group facing)
- [ ] Push to main, deploy to dev
- [ ] Team test: 3-6 agents verifying all fixes + features
