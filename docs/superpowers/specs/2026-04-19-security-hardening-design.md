# Security Hardening at Trust Boundaries — Design

**Date:** 2026-04-19
**Sub-project:** #1 of 6 (see CLAUDE.md code-review remediation plan)
**Status:** Design approved, ready for implementation plan

## Context

The 2026-04-19 code review surfaced three **CRITICAL** findings that share a root cause: user- or peer-controlled data crosses a trust boundary without validation. This spec covers the subset of findings that fix the validation gap itself, not the downstream refactors those gaps exposed.

Findings in scope:

| ID | Location | Class |
|---|---|---|
| CRIT-1a | `apps/godot/scripts/agent.gd:426-444` (`_load_sprite_from_url`) | JS injection via `JavaScriptBridge.eval` string interpolation |
| CRIT-1b | `apps/godot/scripts/agent_manager.gd:220-244` (`_load_sprite_for_agent`) | Same class |
| CRIT-1c | `apps/godot/scripts/autoloads/js_bridge.gd:83` (`emit_to_react`) | Same class |
| CRIT-2 | `apps/backend/routers/town.py:1232-1409` (`handle_ws_message`) | Missing body schema for `POST /ws-message` |
| H-WS-size | `apps/godot/scripts/server/server_protocol.gd:28` | Unbounded WS packet size on game server |
| H-WS-squat | `apps/godot/scripts/server/server_protocol.gd:195-201` | No pre-identification timeout for WS peers |

## Out of scope

- Refactoring `handle_ws_message` internals — Sub-project #3. This spec only wraps the entry point with a validated Pydantic model; the 180-line body stays intact until #3 splits it.
- Viewer-side (browser WebGL) packet limits — the server is the attack surface, not the browser.
- Token placement in WS URL (still on the Known Issues list) — unrelated validation concern, requires coordinated daemon + API Gateway change.

## Design

### 1. Godot `JsBridge.call_js` helper

**File:** `apps/godot/scripts/autoloads/js_bridge.gd`

New method:

```gdscript
func call_js(fn_name: String, args: Dictionary) -> void:
    if not OS.has_feature("web"):
        return
    var key := "%d_%d" % [Time.get_ticks_msec(), randi()]
    var json := JSON.stringify(args)
    # Store payload in a window slot so user data never touches eval string
    JavaScriptBridge.eval("window.__gt_pending = window.__gt_pending || {};")
    JavaScriptBridge.eval("window.__gt_pending['%s'] = %s;" % [key, json])
    JavaScriptBridge.eval("window.__gt_%s && window.__gt_%s(window.__gt_pending['%s']); delete window.__gt_pending['%s'];" % [fn_name, fn_name, key, key])
```

The `key` is composed of millisecond timestamp + random int (not user-controlled). The `json` value is produced by `JSON.stringify`, which escapes all special characters. `fn_name` is a constant string in every call site — never user input.

**Replace three call sites** with calls through the helper:

- `agent.gd:_load_sprite_from_url` — `JsBridge.call_js("load_sprite", {"url": sprite_url, "agent_id": agent_id})`
- `agent_manager.gd:_load_sprite_for_agent` — same pattern with `{"url": ..., "agent_id": ..., "animation_name": ...}`
- `js_bridge.gd:emit_to_react` — `call_js("on_event", data)` where `data` is the event payload dict

The `window.__gt_load_sprite`, `window.__gt_on_event` handlers must be defined in the JS shim that loads alongside `goosetown.js` in the WebGL export. Godot export template already includes the HTML shell (`apps/frontend/public/godot/goosetown.html`) — the handler definitions go there.

### 2. `/ws-message` discriminated-union schema

**File:** `apps/backend/core/ws_models.py` (new)

```python
from typing import Annotated, Literal, Union
from pydantic import BaseModel, Field

# --- Action bodies (for town_agent_act) ---

class MoveAction(BaseModel):
    action: Literal["move"]
    target: str  # location name

class ChatAction(BaseModel):
    action: Literal["chat"]
    target_agent_id: str

class SayAction(BaseModel):
    action: Literal["say"]
    text: str = Field(max_length=500)

class IdleAction(BaseModel):
    action: Literal["idle"]

class EndConversationAction(BaseModel):
    action: Literal["end"]

class ContextTransitionAction(BaseModel):
    action: Literal["context_transition"]
    target_context: Literal["town", "apartment"]

class ActivityAction(BaseModel):
    action: Literal["activity"]
    activity_id: str

AgentActionBody = Annotated[
    Union[
        MoveAction, ChatAction, SayAction, IdleAction,
        EndConversationAction, ContextTransitionAction, ActivityAction,
    ],
    Field(discriminator="action"),
]

# --- Top-level message types ---

class TownAgentConnect(BaseModel):
    type: Literal["town_agent_connect"]
    token: str
    connection_id: str

class TownAgentAct(BaseModel):
    type: Literal["town_agent_act"]
    connection_id: str
    body: AgentActionBody

class TownDisconnect(BaseModel):
    type: Literal["town_disconnect"]
    connection_id: str
    alarm: str | None = None  # HH:MM

class ViewerSubscribe(BaseModel):
    type: Literal["viewer_subscribe"]
    connection_id: str
    token: str

WsMessage = Annotated[
    Union[TownAgentConnect, TownAgentAct, TownDisconnect, ViewerSubscribe],
    Field(discriminator="type"),
]
```

**File:** `apps/backend/routers/town.py`

The existing `POST /ws-message` endpoint signature changes from `request: WsMessageRequest` (untyped dict body) to `message: WsMessage`. FastAPI validates against the union before the handler runs; unknown `type` or malformed sub-bodies return `422 Unprocessable Entity` with detail.

Inside `handle_ws_message`, replace the current `body.get("type", "")` ladder with a type-narrowing `match`:

```python
match message:
    case TownAgentConnect():
        return await _handle_town_agent_connect(message, ...)
    case TownAgentAct():
        return await _handle_town_agent_act(message, ...)
    case TownDisconnect():
        return await _handle_town_disconnect(message, ...)
    case ViewerSubscribe():
        return await _handle_viewer_subscribe(message, ...)
```

No other refactoring in this spec. The 180-line function stays; only the entry-point shape changes.

**Feature flag**: environment variable `WS_STRICT_VALIDATION` (default `true`). When `false`, the endpoint falls back to the current dict-based path — provides a 48h rollback lever in prod if an uncataloged message shape is discovered post-deploy. Flag is removed in Sub-project #3.

### 3. Godot server packet size cap

**File:** `apps/godot/scripts/server/server_protocol.gd`

```gdscript
const MAX_PACKET_BYTES := 262144  # 256 KB

# In the peer receive loop:
while ws.get_available_packet_count() > 0:
    var packet := ws.get_packet()
    if packet.size() > MAX_PACKET_BYTES:
        push_warning("Oversize WS packet from peer %d (%d bytes), disconnecting" % [peer_id, packet.size()])
        _disconnect_peer(peer_id)
        break
    # ... existing parse path
```

### 4. Godot server pre-identification timeout

**File:** `apps/godot/scripts/server/server_protocol.gd`

```gdscript
const IDENTIFICATION_TIMEOUT_SEC := 10.0

var _peer_connected_at: Dictionary[int, float] = {}

# On new peer in _pending_tcp:
_peer_connected_at[peer_id] = Time.get_ticks_msec() / 1000.0

# On first valid `type` message (promotion to _peers):
_peer_connected_at.erase(peer_id)

# In _process:
var now := Time.get_ticks_msec() / 1000.0
for peer_id in _peer_connected_at.keys():
    if now - _peer_connected_at[peer_id] > IDENTIFICATION_TIMEOUT_SEC:
        print("Dropping unidentified peer %d after %.1fs" % [peer_id, now - _peer_connected_at[peer_id]])
        _disconnect_peer(peer_id)
        _peer_connected_at.erase(peer_id)
```

Iterate over a snapshot of keys to avoid mutation-during-iteration (this is also a Sub-project #4 finding, but the pattern is used here proactively).

## Data flow

```
Browser/Agent/Daemon → API GW → Lambda → POST /ws-message
                                              │
                                              ▼
                                  WsMessage (Pydantic discriminated union)
                                   ├── TownAgentConnect → auth + register
                                   ├── TownAgentAct     → typed action dispatch
                                   ├── TownDisconnect   → sleep + close
                                   ├── ViewerSubscribe  → viewer attach
                                   └── (unknown) → 422 rejected at boundary

Godot peer → TCP → WebSocket upgrade
                    │
                    ▼
          _pending_tcp + _peer_connected_at
             │         │
     (10s timeout)    (first `type` msg)
             ▼         ▼
          dropped    _peers
                       │
                 (size-checked every packet, max 256 KB)
                       │
                    dropped if exceeded
```

## Error handling

| Failure | Response | Logging |
|---|---|---|
| Pydantic validation fails | `422` with detail to Lambda | Lambda WARN + field path |
| Unknown `type` | `422` with detail | Lambda WARN |
| Oversize WS packet | Disconnect peer | Godot WARN with peer_id + size |
| Pre-auth timeout | Disconnect peer | Godot INFO with peer_id + elapsed |
| JsBridge `call_js` on non-web platform | No-op | None (headless server path) |

No silent drops anywhere. Every rejection logs enough context to debug.

## Testing

### Backend

**New file:** `apps/backend/tests/unit/test_ws_models.py`

Covers:
- Each valid message type parses successfully.
- Missing required fields → `ValidationError`.
- Unknown `type` discriminator → `ValidationError`.
- Unknown `action` inside `TownAgentAct.body` → `ValidationError`.
- **Regression test for the CVE shape**: a `town_agent_act` payload with extra arbitrary keys must not be silently forwarded to the game server — either it rejects or the extras are stripped.
- `max_length` on `SayAction.text` rejects oversized strings.

**Modify:** `apps/backend/tests/unit/test_town_endpoints.py` — add one endpoint-level test asserting `POST /ws-message` with garbage body returns `422`.

### Godot

**Extend:** `apps/godot/tests/test_server_protocol_handle_message.gd`

- New test: receive a 300 KB packet → peer is dropped.
- New test: connect but never send `type` → peer is in `_peer_connected_at`, advance clock 11s, peer is dropped. (Use a fake clock via dependency injection or a test-only constant override — do not `await get_tree().create_timer(11)`.)

**New file:** `apps/godot/tests/test_js_bridge_call_js.gd`

- `call_js` with dict containing single-quotes, backslashes, unicode, embedded `</script>` produces JSON that round-trips correctly. (Test the `JSON.stringify` path in GDScript — `JavaScriptBridge.eval` is untested at this layer.)

### Smoke (CI post-deploy)

The existing `apps/frontend/e2e/smoke.spec.ts` Playwright test loads the town and asserts the canvas mounts. Extend it:

- After canvas mount, wait up to 10s for at least one agent sprite to actually render (non-blank texture). This verifies the new `JsBridge.call_js` path works end-to-end in the real browser.

## Risks & mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| `JsBridge.call_js` behaves differently from inline interpolation in the browser | Sprites fail to load post-deploy | Extended smoke test catches regression within minutes of deploy; rollback via revert |
| Uncataloged message shape rejected by strict Pydantic | Agents silently disconnect | `WS_STRICT_VALIDATION` flag allows 48h rollback to legacy path |
| Pre-auth timeout too aggressive for slow real clients | Legit clients dropped | 10s is 10x typical handshake latency; monitor logs post-deploy, raise if false positives |
| GdUnit4 can't test `JavaScriptBridge` calls directly | Coverage gap | Test the pure JSON-serialization helper only; treat the bridge call itself as covered by the Playwright smoke |

## Acceptance criteria

1. Every `JavaScriptBridge.eval` call in `apps/godot/scripts/` either takes no user-controlled data or goes through `JsBridge.call_js`. Verified by grep.
2. `POST /ws-message` with malformed body returns `422`. Verified by new test.
3. Godot server drops WS peers that send > 256 KB in a single packet. Verified by new test.
4. Godot server drops WS peers that don't send a `type` message within 10s. Verified by new test.
5. Playwright smoke test verifies at least one sprite renders in the real browser post-deploy.
6. All existing tests still pass (255 backend, 40 frontend, 13 Godot, 2 smoke).

## Deployment

- Land behind `WS_STRICT_VALIDATION=true` in dev for 48h, monitor for unexpected 422s in Lambda logs and backend access logs.
- Promote to prod with the same flag.
- Remove the flag in Sub-project #3 alongside the `handle_ws_message` split.

## Hand-off to implementation

Next skill: `superpowers:writing-plans`. Target: step-by-step implementation plan that can be executed by a subagent or picked up by `superpowers:executing-plans`.
