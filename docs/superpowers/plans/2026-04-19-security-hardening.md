# Security Hardening at Trust Boundaries — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close three CRITICAL and two HIGH findings about unvalidated data crossing trust boundaries — Godot JS eval injection, missing Pydantic validation on `POST /ws-message`, unbounded WebSocket packet size, pre-auth peer squatting.

**Architecture:** Four independent tracks (A, B, C, D) — three in `apps/godot/` (GDScript), one in `apps/backend/` (Python). Each track lands its own commit with tests. A final smoke-test extension verifies JS bridge behavior in a real browser post-deploy. Tracks A, B, and (C or D) can run in parallel; C and D share a file so they must merge sequentially.

**Tech Stack:** Godot 4.6 (GDScript + GdUnit4), FastAPI + Pydantic v2, pytest, Playwright.

**Spec:** `docs/superpowers/specs/2026-04-19-security-hardening-design.md`

**Baseline tests that must still pass:** 255 backend / 40 frontend / 13 Godot / 2 Playwright smoke.

---

## Parallelization Map

| Track | Files touched | Can run parallel with |
|---|---|---|
| A — JsBridge helper | `apps/godot/scripts/autoloads/js_bridge.gd`, `apps/godot/scripts/agent.gd`, `apps/godot/scripts/agent_manager.gd`, frontend JS shim | B, C, D |
| B — `/ws-message` schema | `apps/backend/core/ws_models.py` (new), `apps/backend/routers/town.py` | A, C, D |
| C — Packet size cap | `apps/godot/scripts/server/server_protocol.gd` | A, B |
| D — Pre-auth timeout | `apps/godot/scripts/server/server_protocol.gd` | A, B |
| E — Smoke extension | `apps/frontend/e2e/smoke.spec.ts` | Runs after A lands |

C and D touch the same file; merge C first, then rebase D, or run them sequentially in one subagent.

---

## File Structure

**Created:**
- `apps/backend/core/ws_models.py` — Pydantic discriminated-union schema
- `apps/backend/tests/unit/test_ws_models.py`
- `apps/godot/tests/test_js_bridge_call_js.gd`

**Modified:**
- `apps/godot/scripts/autoloads/js_bridge.gd`
- `apps/godot/scripts/agent.gd`
- `apps/godot/scripts/agent_manager.gd`
- `apps/godot/scripts/server/server_protocol.gd`
- `apps/godot/tests/test_server_protocol_handle_message.gd`
- `apps/backend/routers/town.py`
- `apps/frontend/public/godot/goosetown.html` (JS handler definition)
- `apps/frontend/e2e/smoke.spec.ts`

---

# Track A — Godot JsBridge Helper + Call-Site Migration

### Task A1: Test-drive the `_build_eval_script` pure function

**Files:**
- Create: `apps/godot/tests/test_js_bridge_call_js.gd`
- Test target: `apps/godot/scripts/autoloads/js_bridge.gd` (not yet modified)

- [ ] **Step 1: Write the failing test**

```gdscript
## Tests for ``JsBridge.call_js`` secure helper.
##
## The whole point of the helper is that user-controlled data never appears
## in the string passed to JavaScriptBridge.eval. These tests verify that
## invariant against the pure script-builder; the actual eval side-effect is
## covered by the Playwright smoke extension (Track E).
extends GdUnitTestSuite

const JsBridgeScript := preload("res://scripts/autoloads/js_bridge.gd")


func _make_bridge() -> Node:
	var bridge: Node = JsBridgeScript.new()
	add_child(auto_free(bridge))
	return bridge


func test_build_eval_script_contains_only_fn_name_and_key() -> void:
	var bridge := _make_bridge()
	var script: String = bridge._build_eval_script("load_sprite", "__gt_payload_123_456")

	# The fn_name and key must appear; nothing else user-controlled should.
	assert_that(script).contains("__gt_load_sprite")
	assert_that(script).contains("__gt_payload_123_456")


func test_build_eval_script_does_not_contain_payload_values() -> void:
	var bridge := _make_bridge()
	var script: String = bridge._build_eval_script("load_sprite", "__gt_payload_1")

	# The builder takes no payload — that goes through window.set() — so even
	# with a malicious key attempt it cannot leak into the eval string.
	assert_that(script).does_not_contain("alert")
	assert_that(script).does_not_contain("'; ")
	assert_that(script).does_not_contain("<script")


func test_json_stringify_escapes_single_quotes_in_payload() -> void:
	# Confirm the Godot built-in JSON handles the hostile strings we worry
	# about. If this ever regresses we need a different escape.
	var hostile := {"url": "https://evil.example/'); alert(1);//"}
	var serialized := JSON.stringify(hostile)
	var round_trip: Variant = JSON.parse_string(serialized)
	assert_that(round_trip is Dictionary).is_true()
	assert_that((round_trip as Dictionary).get("url")).is_equal(hostile["url"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/godot && godot --headless --path . -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a res://tests/test_js_bridge_call_js.gd`
Expected: FAIL — `_build_eval_script` does not exist on `JsBridgeScript`.

- [ ] **Step 3: Add the `_build_eval_script` and `call_js` methods to `js_bridge.gd`**

Edit `apps/godot/scripts/autoloads/js_bridge.gd`. Insert these two methods BEFORE the existing `emit_to_react` function (around line 78):

```gdscript
## Build the JS string that call_js() will eval. Pure function for testability.
## Both inputs MUST come from non-user-controlled sources (fn_name is a
## whitelist constant in every call site; key is generated here from time+rand).
## User payload data goes through window.set() — never into the eval string.
func _build_eval_script(fn_name: String, key: String) -> String:
	return (
		"(function(){"
		+ "var p = window['%s'];" % key
		+ "if(window.__gt_%s && p)window.__gt_%s(JSON.parse(p));" % [fn_name, fn_name]
		+ "delete window['%s'];" % key
		+ "})()"
	)


## Call a pre-defined JS handler (window.__gt_<fn_name>) with a dictionary
## payload. User-controlled values are stashed on the window object via the
## JavaScriptObject.set API, never interpolated into the eval string.
##
## The receiving side (frontend JS) must define window.__gt_<fn_name>(args).
func call_js(fn_name: String, args: Dictionary) -> void:
	if not OS.has_feature("web"):
		return
	var window: JavaScriptObject = JavaScriptBridge.get_interface("window")
	var json_str: String = JSON.stringify(args)
	var key: String = "__gt_payload_%d_%d" % [Time.get_ticks_msec(), randi()]
	# Stash user data as a string property — never touches eval.
	window.set(key, json_str)
	JavaScriptBridge.eval(_build_eval_script(fn_name, key))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/godot && godot --headless --path . -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a res://tests/test_js_bridge_call_js.gd`
Expected: PASS — all three tests green.

- [ ] **Step 5: Commit**

```bash
git add apps/godot/scripts/autoloads/js_bridge.gd apps/godot/tests/test_js_bridge_call_js.gd
git commit -m "feat(godot): add JsBridge.call_js secure helper with tests"
```

---

### Task A2: Migrate `emit_to_react` to use `call_js`

**Files:**
- Modify: `apps/godot/scripts/autoloads/js_bridge.gd:78-84`

- [ ] **Step 1: Replace `emit_to_react` body**

Old (`apps/godot/scripts/autoloads/js_bridge.gd:78-84`):

```gdscript
func emit_to_react(event_name: String, data: Dictionary = {}) -> void:
	if OS.has_feature("web"):
		var json_str: String = JSON.stringify(data)
		JavaScriptBridge.eval(
			"window.dispatchEvent(new CustomEvent('godot_%s', {detail: JSON.parse('%s')}))"
			% [event_name, json_str]
		)
```

New:

```gdscript
func emit_to_react(event_name: String, data: Dictionary = {}) -> void:
	# event_name is always a constant literal at the call site (whitelist);
	# data goes through call_js which stashes it via window.set().
	if not OS.has_feature("web"):
		return
	call_js("dispatch_event_" + event_name, data)
```

- [ ] **Step 2: Add corresponding JS handlers to the frontend shim**

Edit `apps/frontend/public/godot/goosetown.html`. Find the `<script>` block that sets up Godot (search for `Engine` or `godotSpawnAgent`). Before that block, add:

```html
<script>
	// Secure bridge handlers. Godot calls window.__gt_<name>(args) with a
	// pre-parsed object; no string interpolation occurs on the Godot side.
	window.__gt_dispatch_event_scene_changed = function(args) {
		window.dispatchEvent(new CustomEvent('godot_scene_changed', { detail: args }));
	};
	// Add more __gt_dispatch_event_<name> handlers as emit_to_react call sites
	// appear. Only scene_changed is used today (see js_bridge.gd:183).
</script>
```

Grep for all current `emit_to_react(` call sites to confirm the full list:

Run: `grep -rn "emit_to_react(" apps/godot/scripts/`
Expected: confirms only `scene_changed` is used (line 183). If any other event names appear, add a handler for each.

- [ ] **Step 3: Verify compile — run Godot tests**

Run: `cd apps/godot && godot --headless --path . -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a res://tests/`
Expected: all 13 existing Godot tests + 3 new call_js tests pass.

- [ ] **Step 4: Commit**

```bash
git add apps/godot/scripts/autoloads/js_bridge.gd apps/frontend/public/godot/goosetown.html
git commit -m "refactor(godot): route emit_to_react through call_js helper"
```

---

### Task A3: Migrate `agent.gd:_load_sprite_from_url` to `call_js`

**Files:**
- Modify: `apps/godot/scripts/agent.gd:422-444`
- Modify: `apps/frontend/public/godot/goosetown.html` (add handler)

- [ ] **Step 1: Replace the method body**

Old (`apps/godot/scripts/agent.gd:422-444`):

```gdscript
func _load_sprite_from_url(url: String) -> void:
	if not OS.has_feature("web"):
		return

	var js_code: String = """
	(async function() {
		try {
			var resp = await fetch('%s');
			var blob = await resp.blob();
			var reader = new FileReader();
			reader.onload = function() {
				var base64 = reader.result.split(',')[1];
				if (window.godotUpdateAgentSprite) {
					window.godotUpdateAgentSprite(JSON.stringify({id: '%s', base64: base64}));
				}
			};
			reader.readAsDataURL(blob);
		} catch(e) {
			console.warn('Failed to load sprite:', e);
		}
	})();
	""" % [url, agent_id]
	JavaScriptBridge.eval(js_code)
```

New:

```gdscript
func _load_sprite_from_url(url: String) -> void:
	if not OS.has_feature("web"):
		return
	JsBridge.call_js("load_sprite", {"url": url, "agent_id": agent_id})
```

- [ ] **Step 2: Add the `__gt_load_sprite` handler to `goosetown.html`**

In the `<script>` block from Task A2, add:

```javascript
window.__gt_load_sprite = function(args) {
	// args: { url, agent_id, animation_name? }
	var url = String(args.url || '');
	var agentId = String(args.agent_id || '');
	var anim = args.animation_name ? String(args.animation_name) : null;
	if (!url || !agentId) return;
	fetch(url)
		.then(function(r) { return r.blob(); })
		.then(function(blob) {
			var reader = new FileReader();
			reader.onloadend = function() {
				var base64 = reader.result.split(',')[1];
				if (window.godotUpdateAgentSprite) {
					var payload = anim
						? { id: agentId, base64: base64, animation: anim }
						: { id: agentId, base64: base64 };
					window.godotUpdateAgentSprite(JSON.stringify(payload));
				}
			};
			reader.readAsDataURL(blob);
		})
		.catch(function(e) { console.warn('[GooseTown] sprite load failed:', e); });
};
```

- [ ] **Step 3: Verify `JsBridge` is accessible from agent.gd**

`JsBridge` is an autoload (confirmed via `apps/godot/scripts/autoloads/js_bridge.gd:2` comment). Accessible as a global singleton — no import needed. If Godot complains it's undeclared, check `project.godot` autoload section.

- [ ] **Step 4: Run Godot tests**

Run: `cd apps/godot && godot --headless --path . -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a res://tests/`
Expected: all tests still pass (the test file only tests pure functions; this migration is covered by smoke test E).

- [ ] **Step 5: Commit**

```bash
git add apps/godot/scripts/agent.gd apps/frontend/public/godot/goosetown.html
git commit -m "refactor(godot): migrate _load_sprite_from_url to JsBridge.call_js"
```

---

### Task A4: Migrate `agent_manager.gd:_load_sprite_for_agent` to `call_js`

**Files:**
- Modify: `apps/godot/scripts/agent_manager.gd:220-244`

- [ ] **Step 1: Replace the method body**

Old (`apps/godot/scripts/agent_manager.gd:220-244`):

```gdscript
func _load_sprite_for_agent(agent_id: String, url: String, animation_name: String) -> void:
	if not OS.has_feature("web"):
		return
	var js_code := """
		(function() {
			fetch('%s')
				.then(function(r) { return r.blob(); })
				.then(function(b) {
					var reader = new FileReader();
					reader.onloadend = function() {
						var base64 = reader.result.split(',')[1];
						if (window.godotUpdateAgentSprite) {
							window.godotUpdateAgentSprite(JSON.stringify({
								id: '%s',
								base64: base64,
								animation: '%s'
							}));
						}
					};
					reader.readAsDataURL(b);
				})
				.catch(function(e) { console.error('Sprite load failed:', e); });
		})();
	""" % [url, agent_id, animation_name]
	JavaScriptBridge.eval(js_code)
```

New:

```gdscript
func _load_sprite_for_agent(agent_id: String, url: String, animation_name: String) -> void:
	if not OS.has_feature("web"):
		return
	JsBridge.call_js("load_sprite", {
		"url": url,
		"agent_id": agent_id,
		"animation_name": animation_name,
	})
```

The `__gt_load_sprite` handler added in A3 already handles the `animation_name` optional field.

- [ ] **Step 2: Run Godot tests**

Run: `cd apps/godot && godot --headless --path . -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a res://tests/`
Expected: all tests pass.

- [ ] **Step 3: Verify no remaining eval interpolations**

Run: `grep -rn "JavaScriptBridge.eval" apps/godot/scripts/`
Expected: only three remaining occurrences — all inside `js_bridge.gd` (the `_emit_ready_event` which uses static strings, and the new `call_js`). No occurrences in `agent.gd` or `agent_manager.gd`.

- [ ] **Step 4: Commit**

```bash
git add apps/godot/scripts/agent_manager.gd
git commit -m "refactor(godot): migrate _load_sprite_for_agent to JsBridge.call_js"
```

---

# Track B — `/ws-message` Pydantic Discriminated Union

### Task B1: Create `ws_models.py` with discriminated union

**Files:**
- Create: `apps/backend/core/ws_models.py`
- Create: `apps/backend/tests/unit/test_ws_models.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for core.ws_models — Pydantic discriminated union validating
incoming WebSocket messages forwarded from API Gateway."""
from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from core.ws_models import WsMessage


_adapter: TypeAdapter[WsMessage] = TypeAdapter(WsMessage)


class TestTownAgentConnect:
    def test_accepts_minimal_valid_payload(self) -> None:
        msg = _adapter.validate_python({
            "type": "town_agent_connect",
            "token": "abc",
            "connection_id": "c1",
        })
        assert msg.type == "town_agent_connect"
        assert msg.token == "abc"

    def test_rejects_missing_token(self) -> None:
        with pytest.raises(ValidationError):
            _adapter.validate_python({
                "type": "town_agent_connect",
                "connection_id": "c1",
            })


class TestTownAgentAct:
    def test_accepts_move_action(self) -> None:
        msg = _adapter.validate_python({
            "type": "town_agent_act",
            "connection_id": "c1",
            "body": {"action": "move", "target": "plaza"},
        })
        assert msg.type == "town_agent_act"
        assert msg.body.action == "move"
        assert msg.body.target == "plaza"

    def test_accepts_say_action_with_text(self) -> None:
        msg = _adapter.validate_python({
            "type": "town_agent_act",
            "connection_id": "c1",
            "body": {"action": "say", "text": "hello"},
        })
        assert msg.body.action == "say"
        assert msg.body.text == "hello"

    def test_rejects_say_text_over_max_length(self) -> None:
        with pytest.raises(ValidationError):
            _adapter.validate_python({
                "type": "town_agent_act",
                "connection_id": "c1",
                "body": {"action": "say", "text": "x" * 501},
            })

    def test_rejects_unknown_action(self) -> None:
        with pytest.raises(ValidationError):
            _adapter.validate_python({
                "type": "town_agent_act",
                "connection_id": "c1",
                "body": {"action": "eat_the_pigeons"},
            })

    def test_rejects_move_without_target(self) -> None:
        with pytest.raises(ValidationError):
            _adapter.validate_python({
                "type": "town_agent_act",
                "connection_id": "c1",
                "body": {"action": "move"},
            })


class TestTownDisconnect:
    def test_accepts_without_alarm(self) -> None:
        msg = _adapter.validate_python({
            "type": "town_disconnect",
            "connection_id": "c1",
        })
        assert msg.alarm is None

    def test_accepts_with_alarm_string(self) -> None:
        msg = _adapter.validate_python({
            "type": "town_disconnect",
            "connection_id": "c1",
            "alarm": "07:30",
        })
        assert msg.alarm == "07:30"


class TestTopLevelValidation:
    def test_rejects_unknown_type(self) -> None:
        with pytest.raises(ValidationError):
            _adapter.validate_python({
                "type": "town_eat_sandwich",
                "connection_id": "c1",
            })

    def test_rejects_missing_type(self) -> None:
        with pytest.raises(ValidationError):
            _adapter.validate_python({"connection_id": "c1"})

    def test_cve_regression_extra_keys_do_not_pass_through(self) -> None:
        """Regression guard for the 2026-04-19 CRITICAL finding: arbitrary
        extra keys in body must not be silently forwarded to the game server.
        Pydantic's default is to ignore extras, but we verify it explicitly
        here so a future config change doesn't reintroduce the bug."""
        msg = _adapter.validate_python({
            "type": "town_agent_act",
            "connection_id": "c1",
            "body": {"action": "idle", "malicious_extra": "drop_table"},
        })
        # body must be an IdleAction instance with only known fields
        body_dict = msg.body.model_dump()
        assert "malicious_extra" not in body_dict
        assert body_dict == {"action": "idle"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/test_ws_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.ws_models'`.

- [ ] **Step 3: Create `core/ws_models.py`**

```python
"""Pydantic discriminated-union schema for WebSocket messages forwarded from
API Gateway to ``POST /ws-message``.

Replaces the previous untyped ``dict`` body on ``WsMessageRequest``. FastAPI
validates the payload at the request boundary; handlers receive a typed
object and pattern-match on the concrete class.

Adding a new message type:
    1. Add a Pydantic class with ``type: Literal["..."]``.
    2. Add it to the ``WsMessage`` union.
    3. Add a handler branch in ``routers.town.handle_ws_message``.
"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Action bodies (nested inside TownAgentAct)
# ---------------------------------------------------------------------------


class MoveAction(BaseModel):
    action: Literal["move"]
    target: str


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
        MoveAction,
        ChatAction,
        SayAction,
        IdleAction,
        EndConversationAction,
        ContextTransitionAction,
        ActivityAction,
    ],
    Field(discriminator="action"),
]


# ---------------------------------------------------------------------------
# Top-level messages
# ---------------------------------------------------------------------------


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
    alarm: str | None = None


class ViewerSubscribe(BaseModel):
    type: Literal["town_subscribe"]
    connection_id: str


WsMessage = Annotated[
    Union[TownAgentConnect, TownAgentAct, TownDisconnect, ViewerSubscribe],
    Field(discriminator="type"),
]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/test_ws_models.py -v`
Expected: all 11 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/ws_models.py apps/backend/tests/unit/test_ws_models.py
git commit -m "feat(backend): add WsMessage discriminated-union Pydantic schema"
```

---

### Task B2: Add `WS_STRICT_VALIDATION` config flag

**Files:**
- Modify: `apps/backend/core/config.py`

- [ ] **Step 1: Locate the Settings class**

Run: `grep -n "class Settings" apps/backend/core/config.py`

- [ ] **Step 2: Add the new setting**

Add this field (alongside existing settings like `ENVIRONMENT`, `WS_LAMBDA_SECRET`):

```python
WS_STRICT_VALIDATION: bool = True  # Set to False to roll back to legacy dict-based body handling for 48h post-deploy
```

Read the existing file first to find the correct location and style — match the existing field declarations.

- [ ] **Step 3: Verify config loads**

Run: `cd apps/backend && uv run python -c "from core.config import settings; print(settings.WS_STRICT_VALIDATION)"`
Expected: `True`.

- [ ] **Step 4: Commit**

```bash
git add apps/backend/core/config.py
git commit -m "feat(backend): add WS_STRICT_VALIDATION config flag"
```

---

### Task B3: Wire validated dispatch into `handle_ws_message`

**Files:**
- Modify: `apps/backend/routers/town.py:1227-1234` (and the dispatch ladder 1246-1409)
- Modify: `apps/backend/tests/unit/test_town_endpoints.py` — add validation-at-boundary test

- [ ] **Step 1: Read current `handle_ws_message` structure**

Run: `sed -n '1225,1260p' apps/backend/routers/town.py` via Read tool. Confirm the first ~35 lines to see the current body-typed endpoint signature and the first dispatch branch.

- [ ] **Step 2: Write the failing endpoint-level test**

Add to `apps/backend/tests/unit/test_town_endpoints.py` (append at end of file):

```python
class TestWsMessageValidation:
    """Boundary-validation regression tests for /ws-message under
    WS_STRICT_VALIDATION=True (the default).

    Auth setup: the shared-secret check at town.py:1240 is skipped when
    ``settings.ENVIRONMENT == "dev"`` AND ``settings.WS_LAMBDA_SECRET`` is
    falsy. The conftest sets ENVIRONMENT=dev; we just need to make sure the
    secret is empty. If the conftest sets a value, pass it as a header.
    """

    _HEADERS = {"x-ws-lambda-secret": ""}  # adjust if conftest sets one

    def test_rejects_unknown_type(self, client) -> None:
        resp = client.post(
            "/town/ws-message",
            json={
                "connectionId": "c1",
                "principalId": "p1",
                "body": {"type": "town_eat_sandwich"},
            },
            headers=self._HEADERS,
        )
        assert resp.status_code == 422

    def test_rejects_agent_act_with_unknown_action(self, client) -> None:
        resp = client.post(
            "/town/ws-message",
            json={
                "connectionId": "c1",
                "principalId": "p1",
                "body": {
                    "type": "town_agent_act",
                    "connection_id": "c1",
                    "body": {"action": "eat_the_pigeons"},
                },
            },
            headers=self._HEADERS,
        )
        assert resp.status_code == 422

    def test_rejects_say_with_missing_text(self, client) -> None:
        resp = client.post(
            "/town/ws-message",
            json={
                "connectionId": "c1",
                "principalId": "p1",
                "body": {
                    "type": "town_agent_act",
                    "connection_id": "c1",
                    "body": {"action": "say"},
                },
            },
            headers=self._HEADERS,
        )
        assert resp.status_code == 422
```

If the `client` fixture is in `conftest.py`, these pick it up automatically. Before running, verify the test conftest sets `ENVIRONMENT=dev` and either `WS_LAMBDA_SECRET=""` or exposes the shared secret so the header can be set explicitly.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/test_town_endpoints.py::TestWsMessageValidation -v`
Expected: FAIL — endpoint currently accepts arbitrary bodies and returns 200.

- [ ] **Step 4: Replace `WsMessageRequest.body` typing + add dispatch**

Edit `apps/backend/routers/town.py`. Two changes:

**4a.** Add imports at the top of the file (join existing imports):

```python
from core.ws_models import (
    TownAgentAct,
    TownAgentConnect,
    TownDisconnect,
    ViewerSubscribe,
    WsMessage,
)
from pydantic import TypeAdapter, ValidationError
```

**4b.** Replace the `WsMessageRequest` class (currently at line 1227-1230):

```python
class WsMessageRequest(BaseModel):
    connectionId: str
    principalId: str
    body: dict
```

with:

```python
class WsMessageRequest(BaseModel):
    connectionId: str
    principalId: str
    body: dict  # Validated separately via WsMessage below — see handle_ws_message.


_ws_message_adapter: TypeAdapter[WsMessage] = TypeAdapter(WsMessage)
```

**4c.** Inside `handle_ws_message`, immediately after the shared-secret auth check (around line 1244), before `body = request.body`, insert:

```python
    # Strict validation: parse body into typed WsMessage. When the flag is
    # off we fall through to the legacy dict-based path below (preserved for
    # rollback). Unknown type / malformed sub-body returns 422 at the
    # boundary.
    if settings.WS_STRICT_VALIDATION:
        try:
            _ws_message_adapter.validate_python(request.body)
        except ValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail={"validation_errors": exc.errors(include_url=False)},
            )
```

This adds validation at the boundary without rewriting the 180-line dispatch ladder — Sub-project #3 does that refactor. The legacy handler keeps its `body.get("type", "")` access; strict validation only guards the entrypoint.

- [ ] **Step 5: Run endpoint tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/test_town_endpoints.py::TestWsMessageValidation -v`
Expected: all three new tests PASS.

- [ ] **Step 6: Run the whole backend test suite to verify no regressions**

Run: `cd apps/backend && uv run pytest -q`
Expected: 255 existing + 11 (ws_models) + 3 (endpoint boundary) = **269 passed, 0 failed**.

- [ ] **Step 7: Commit**

```bash
git add apps/backend/routers/town.py apps/backend/tests/unit/test_town_endpoints.py
git commit -m "feat(backend): enforce WsMessage schema at /ws-message boundary"
```

---

# Track C — Godot Server Packet Size Cap

### Task C1: Test-drive `_should_accept_packet_size`

**Files:**
- Modify: `apps/godot/tests/test_server_protocol_handle_message.gd`
- Modify: `apps/godot/scripts/server/server_protocol.gd`

- [ ] **Step 1: Write the failing test**

Append to `apps/godot/tests/test_server_protocol_handle_message.gd`:

```gdscript
# ---------------------------------------------------------------------------
# Packet size cap
# ---------------------------------------------------------------------------


func test_should_accept_packet_size_rejects_oversize() -> void:
	var proto: ServerProtocol = ServerProtocolScript.new()
	add_child(auto_free(proto))

	assert_that(proto._should_accept_packet_size(100)).is_true()
	assert_that(proto._should_accept_packet_size(262144)).is_true()  # exactly at limit OK
	assert_that(proto._should_accept_packet_size(262145)).is_false()
	assert_that(proto._should_accept_packet_size(5_000_000)).is_false()


func test_max_packet_bytes_is_256kb() -> void:
	# Documented protocol limit; any future change must be deliberate.
	assert_that(ServerProtocolScript.MAX_PACKET_BYTES).is_equal(262144)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/godot && godot --headless --path . -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a res://tests/test_server_protocol_handle_message.gd`
Expected: FAIL — `MAX_PACKET_BYTES` and `_should_accept_packet_size` don't exist.

- [ ] **Step 3: Add constant and helper to `server_protocol.gd`**

Add below the existing `PEER_TIMEOUT` constant (around line 25):

```gdscript
## Maximum WS frame size. Protects against memory exhaustion from a hostile
## or broken peer. World-state broadcasts with ~20 agents are < 8 KB; 256 KB
## gives generous headroom.
const MAX_PACKET_BYTES: int = 262144  # 256 KB


## Pure helper: is this packet size acceptable?
func _should_accept_packet_size(size: int) -> bool:
	return size <= MAX_PACKET_BYTES
```

- [ ] **Step 4: Enforce the cap in the receive loop**

Replace the `while ws.get_available_packet_count() > 0:` block in `_process` (around line 156-160):

```gdscript
			# Read all available packets.
			while ws.get_available_packet_count() > 0:
				var packet := ws.get_packet()
				if not _should_accept_packet_size(packet.size()):
					push_warning(
						"ServerProtocol: peer %d sent oversize packet (%d bytes), disconnecting"
						% [peer_id, packet.size()]
					)
					ws.close()
					disconnected_ids.append(peer_id)
					break
				var text := packet.get_string_from_utf8()
				_last_message_time[peer_id] = Time.get_unix_time_from_system()
				_handle_message(peer_id, text)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd apps/godot && godot --headless --path . -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a res://tests/`
Expected: all tests pass, including the 2 new ones.

- [ ] **Step 6: Commit**

```bash
git add apps/godot/scripts/server/server_protocol.gd apps/godot/tests/test_server_protocol_handle_message.gd
git commit -m "feat(godot): cap WS packet size at 256 KB with peer disconnect on overflow"
```

---

# Track D — Pre-Auth Peer Identification Timeout

### Task D1: Test-drive `_unidentified_peers_to_evict`

**Files:**
- Modify: `apps/godot/tests/test_server_protocol_handle_message.gd`
- Modify: `apps/godot/scripts/server/server_protocol.gd`

- [ ] **Step 1: Write the failing test**

Append to `apps/godot/tests/test_server_protocol_handle_message.gd`:

```gdscript
# ---------------------------------------------------------------------------
# Pre-auth identification timeout
# ---------------------------------------------------------------------------


func test_identification_timeout_is_10_seconds() -> void:
	assert_that(ServerProtocolScript.IDENTIFICATION_TIMEOUT_SEC).is_equal(10.0)


func test_unidentified_peers_evict_returns_stale_peers() -> void:
	var proto: ServerProtocol = ServerProtocolScript.new()
	add_child(auto_free(proto))

	# Seed two peers: one fresh, one stale.
	proto._peer_connected_at[1] = 100.0
	proto._peer_connected_at[2] = 50.0

	# At t=105s: peer 2 is 55s old (evict), peer 1 is 5s old (keep).
	var evict: Array = proto._unidentified_peers_to_evict(105.0)
	assert_that(evict).contains_exactly([2])


func test_unidentified_peers_evict_empty_when_no_peers() -> void:
	var proto: ServerProtocol = ServerProtocolScript.new()
	add_child(auto_free(proto))
	assert_that(proto._unidentified_peers_to_evict(1000.0)).is_empty()


func test_unidentified_peers_evict_respects_exact_threshold() -> void:
	var proto: ServerProtocol = ServerProtocolScript.new()
	add_child(auto_free(proto))

	proto._peer_connected_at[1] = 0.0
	# Exactly at the timeout boundary — should NOT evict (strict greater-than).
	assert_that(proto._unidentified_peers_to_evict(10.0)).is_empty()
	# Just past — should evict.
	assert_that(proto._unidentified_peers_to_evict(10.01)).contains_exactly([1])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/godot && godot --headless --path . -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a res://tests/test_server_protocol_handle_message.gd`
Expected: FAIL — constant and helper and `_peer_connected_at` don't exist.

- [ ] **Step 3: Add the state, constant, and helper**

Edit `apps/godot/scripts/server/server_protocol.gd`.

**3a.** Add constant under the existing `MAX_PACKET_BYTES` (Task C1 land first, then D rebases):

```gdscript
## Peers that connect but never send a first ``type`` message within this
## window are evicted. Protects against TCP-level squatting.
const IDENTIFICATION_TIMEOUT_SEC: float = 10.0
```

**3b.** Add state dictionary in the state declarations (around line 33):

```gdscript
var _peer_connected_at: Dictionary = {}  # peer_id -> float (Time.get_unix_time_from_system)
```

**3c.** Initialize when a peer moves to `_peers` — in the TCP→WS upgrade path (around line 129-132), add after `_last_message_time[peer_id] = ...`:

```gdscript
			_peer_connected_at[peer_id] = Time.get_unix_time_from_system()
```

**3d.** Erase when identified — in `_handle_message` after `_identified[peer_id] = true` (line 197):

```gdscript
			_peer_connected_at.erase(peer_id)
```

**3e.** Add the pure helper above `_handle_message` (around line 181):

```gdscript
## Pure helper: return peer_ids whose pre-auth window has expired.
## ``now`` is expected to be ``Time.get_unix_time_from_system()`` in prod; tests
## pass a controlled value to avoid flake.
func _unidentified_peers_to_evict(now: float) -> Array:
	var result: Array = []
	for peer_id in _peer_connected_at.keys():
		var connected_at: float = _peer_connected_at[peer_id]
		if now - connected_at > IDENTIFICATION_TIMEOUT_SEC:
			result.append(peer_id)
	return result
```

**3f.** Call the helper in `_process`. Add a new block before the `# Emit disconnected signals and clean up.` loop (around line 170-172):

```gdscript
		# Evict unidentified peers past the timeout window.
		var stale_peers: Array = _unidentified_peers_to_evict(Time.get_unix_time_from_system())
		for peer_id in stale_peers:
			print(
				"ServerProtocol: peer %d unidentified after %.1fs, disconnecting"
				% [peer_id, Time.get_unix_time_from_system() - _peer_connected_at[peer_id]]
			)
			if _peers.has(peer_id):
				var ws: WebSocketPeer = _peers[peer_id]
				ws.close()
			disconnected_ids.append(peer_id)
			_peer_connected_at.erase(peer_id)
```

**3g.** Also erase from `_peer_connected_at` in the cleanup loop (add after `_last_message_time.erase(peer_id)` around line 177):

```gdscript
			_peer_connected_at.erase(peer_id)
```

**3h.** Also clear `_peer_connected_at` in `stop()` (around line 65):

```gdscript
	_peer_connected_at.clear()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/godot && godot --headless --path . -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a res://tests/`
Expected: all tests pass, including the 4 new ones.

- [ ] **Step 5: Commit**

```bash
git add apps/godot/scripts/server/server_protocol.gd apps/godot/tests/test_server_protocol_handle_message.gd
git commit -m "feat(godot): evict unidentified WS peers after 10s timeout"
```

---

# Track E — Playwright Smoke Extension (runs after Track A)

### Task E1: Verify at least one sprite renders post-deploy

**Files:**
- Modify: `apps/frontend/e2e/smoke.spec.ts`

- [ ] **Step 1: Add a new Playwright test to the existing `describe` block**

Append inside `test.describe('GooseTown smoke', () => { ... })`, after the existing two tests:

```typescript
  test('at least one agent sprite renders in the canvas', async ({ page }) => {
    // Regression guard for the JsBridge.call_js migration (security-hardening
    // sub-project #1). If the __gt_load_sprite handler or window.set() path
    // broke, no sprite ever gets uploaded to the Godot texture, so agents
    // render as blank shadow placeholders. We detect a non-blank sprite by
    // polling the godotUpdateAgentSprite callback count exposed on window.
    await page.goto('/');

    // Wait for the canvas + public conversations (ensures town has loaded
    // data).
    await expect(page.locator('canvas').first()).toBeVisible({ timeout: 20_000 });

    // Poll for evidence that at least one sprite was loaded successfully.
    // The JS shim in goosetown.html increments window.__gt_sprite_load_count
    // on each successful __gt_load_sprite call — see Task E2.
    await expect
      .poll(
        async () => await page.evaluate(() => (window as unknown as { __gt_sprite_load_count?: number }).__gt_sprite_load_count || 0),
        { timeout: 30_000, intervals: [1000, 2000, 3000] },
      )
      .toBeGreaterThan(0);
  });
```

- [ ] **Step 2: Add the counter increment to the `__gt_load_sprite` handler in `goosetown.html`**

Edit the handler added in Task A3. Change:

```javascript
window.__gt_load_sprite = function(args) {
	// ... existing body ...
	.then(function(blob) {
		// ... existing body ...
		reader.onloadend = function() {
			// ... existing body ...
			if (window.godotUpdateAgentSprite) {
				// ... existing payload ...
				window.godotUpdateAgentSprite(JSON.stringify(payload));
			}
		};
		// ... existing body ...
	})
	.catch(function(e) { console.warn('[GooseTown] sprite load failed:', e); });
};
```

to also increment a counter after a successful `godotUpdateAgentSprite` call:

```javascript
if (window.godotUpdateAgentSprite) {
	// ... existing payload construction ...
	window.godotUpdateAgentSprite(JSON.stringify(payload));
	window.__gt_sprite_load_count = (window.__gt_sprite_load_count || 0) + 1;
}
```

- [ ] **Step 3: Run Playwright locally to verify**

Run: `cd apps/frontend && E2E_BASE_URL=https://dev.goosetown.isol8.co pnpm playwright test e2e/smoke.spec.ts -g "sprite renders"`
Expected: PASS — the dev deploy has seeded agents so at least one sprite loads within 30s.

If this fails with "counter is 0", verify that the deployed version actually contains the updated `goosetown.html` (the .pck might have been regenerated without the shim change). Track A3 commits the HTML change; ensure that's live in dev before running.

- [ ] **Step 4: Commit**

```bash
git add apps/frontend/e2e/smoke.spec.ts apps/frontend/public/godot/goosetown.html
git commit -m "test(smoke): verify sprite renders end-to-end after JsBridge migration"
```

---

# Final Verification

### Task F1: Run the full test matrix

- [ ] **Step 1: Backend tests**

Run: `cd apps/backend && uv run pytest -q`
Expected: **269 passed** (255 existing + 11 ws_models + 3 endpoint boundary).

- [ ] **Step 2: Backend lint**

Run: `cd apps/backend && uv run ruff check . && uv run ruff format --check .`
Expected: clean.

- [ ] **Step 3: Frontend tests**

Run: `cd apps/frontend && pnpm vitest --run`
Expected: **40 passed** (unchanged; this sub-project doesn't touch frontend hooks).

- [ ] **Step 4: Frontend lint + typecheck**

Run: `cd apps/frontend && pnpm lint && pnpm typecheck`
Expected: clean.

- [ ] **Step 5: Godot tests**

Run: `cd apps/godot && godot --headless --path . -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a res://tests/`
Expected: **22 passed** (13 existing + 3 call_js + 2 packet-size + 4 pre-auth timeout). If one of the test counts is off, verify each track's test additions landed.

- [ ] **Step 6: Grep for residual JS injection surface**

Run: `grep -rn "JavaScriptBridge.eval.*%" apps/godot/scripts/`
Expected: **no matches**. All remaining `JavaScriptBridge.eval` calls in `apps/godot/scripts/autoloads/js_bridge.gd` should use only compile-time constant strings (the `_emit_ready_event` block defining `window._godotBridge`).

- [ ] **Step 7: Update CLAUDE.md to reflect the resolution**

Edit `CLAUDE.md`. Move the six "Critical" and "High priority" items added by this sub-project from "Still Open" to "Resolved" with strikethrough:

- JS injection in Godot WebGL client → Resolved via `JsBridge.call_js` helper; user-controlled data never touches `JavaScriptBridge.eval`.
- `/ws-message` body schema → Resolved via `core/ws_models.py` discriminated union; validated at request boundary under `WS_STRICT_VALIDATION=true`.
- WS packet size unbounded on Godot server → Resolved via `MAX_PACKET_BYTES=256KB` cap in `server_protocol.gd`.
- Pre-auth peer accumulation → Resolved via `IDENTIFICATION_TIMEOUT_SEC=10.0` eviction in `server_protocol.gd`.

Also update the test baseline line:

Before: `[x] Frontend + backend + E2E test coverage in CI (255 backend / 40 frontend / 13 Godot / 2 smoke)`
After: `[x] Frontend + backend + E2E test coverage in CI (269 backend / 40 frontend / 22 Godot / 3 smoke)`

- [ ] **Step 8: Commit CLAUDE.md**

```bash
git add CLAUDE.md
git commit -m "docs: mark security hardening sub-project resolved; update test counts"
```

---

## Deployment Notes

1. Merge to main. Dev pipeline deploys automatically.
2. Monitor Lambda authorizer + backend logs for 48h for unexpected `422`s on `/ws-message` (indicates an uncataloged message shape — if any, re-enable `WS_STRICT_VALIDATION=false` via Vercel/CDK env, fix the schema, re-enable).
3. Monitor Godot server logs for "peer X unidentified after" / "oversize packet" — expected count is near zero in prod.
4. The `WS_STRICT_VALIDATION` flag is removed in Sub-project #3 alongside the `handle_ws_message` split.
