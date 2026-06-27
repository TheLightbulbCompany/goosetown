# In-World Exchange: Living Economy at the Bank — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the sidebar Exchange cork-board with the economy as living motion — economy events pushed over the viewer WS animate brief cues at the bank (posted/awarded/payout) and stream into a sidebar ticker.

**Architecture:** `exchange_service` pushes a discrete `economy_event` to viewer WS connections via a new `ViewerRelay.push_event`; `useTownState` routes each to Godot (an `EconomyCue` at the agent's live position) and to a capped `economyEvents` context list that `ExchangeTicker` renders. One stream, two surfaces.

**Tech Stack:** Python 3.12 FastAPI backend (pytest), React 18 + TypeScript + Vite (vitest), Godot 4.6 (GDScript, GdUnit4, WebGL export).

## Global Constraints

- **`economy_event` payload shape (the cross-task contract), exact keys:** `{ "event": "economy_event", "type": "posted"|"awarded"|"payout"|"failed"|"expired"|"bid", "actorId": str, "actorName": str, "counterpartyId": str|null, "counterpartyName": str|null, "amount": int|null, "title": str }`.
- **Per-type mapping:** posted → actor=consumer, amount=max_price; awarded → actor=winning provider, counterparty=consumer, amount=agreed_price; payout → actor=provider (recipient), counterparty=consumer (source), amount=payout. `amount` is `null` when there's no crumb figure (always null for `bid`). **The bid cue must never carry an amount** (sealed-bid integrity).
- **JS bridge callback name:** `window.godotEconomyEvent(jsonString)` (Task 3 calls it, Task 5 registers it — must match exactly).
- **Scope:** core = posted / awarded / payout cues + ticker. Bid + failed/expired cues are out of this plan (additive later). Exterior at the bank only; read-only; no owner interactivity.
- **`.pck` discipline (Critical Rule):** any Godot source change (`.gd`/`.tscn`) requires re-exporting + committing `apps/frontend/public/godot/goosetown.{html,js,wasm,pck}` in the same commit. Export: `/Applications/Godot.app/Contents/MacOS/Godot --headless --path apps/godot --export-release "Web" apps/frontend/public/godot/goosetown.html`.
- **Test commands:** backend `cd apps/backend && uv run pytest <file> -v` + ruff (`uv run ruff check <files> && uv run ruff format --check <files>`); frontend `cd apps/frontend && pnpm test` (vitest, run via `run_in_background: true`) + `pnpm typecheck`; Godot GdUnit `GODOT_BIN=/Applications/Godot.app/Contents/MacOS/Godot apps/godot/addons/gdUnit4/runtest.sh -a res://tests/<file>.gd`.

---

### Task 1: Backend — `ViewerRelay.push_event` (discrete fan-out)

Extract the per-connection send+prune from `_broadcast` into a reusable `_fan_out`, and add `push_event` — a best-effort, non-coalescing discrete push (every economy event must land, unlike high-rate `world_state`).

**Files:**
- Modify: `apps/backend/core/services/viewer_relay.py`
- Test: `apps/backend/tests/unit/test_viewer_relay_push_event.py` (create)

**Interfaces:**
- Produces: `ViewerRelay._fan_out(conns: list[str], data: dict) -> Awaitable[None]`; `ViewerRelay.push_event(payload: dict) -> None`.

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/test_viewer_relay_push_event.py`:

```python
"""ViewerRelay.push_event — discrete (non-coalescing) fan-out of an event to
all viewer connections, pruning gone ones. Used for economy_event cues."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from core.services.viewer_relay import ViewerRelay


def test_fan_out_delivers_to_all_and_prunes_gone():
    relay = ViewerRelay("ws://gs")
    relay._viewer_connections = {"live-1", "dead-1", "live-2"}
    mgmt = MagicMock()
    # send_message returns False ONLY for the gone connection.
    mgmt.send_message.side_effect = lambda cid, data: cid != "dead-1"
    payload = {"event": "economy_event", "type": "payout", "title": "Haiku"}
    with (
        patch("core.services.management_api_client.ManagementApiClient", return_value=mgmt),
        patch("core.dynamo.delete_connection") as del_conn,
    ):
        asyncio.run(relay._fan_out(list(relay._viewer_connections), payload))
    sent_to = {c.args[0] for c in mgmt.send_message.call_args_list}
    assert sent_to == {"live-1", "dead-1", "live-2"}
    assert relay._viewer_connections == {"live-1", "live-2"}  # dead pruned in-memory
    del_conn.assert_called_once_with("dead-1")  # and from the durable table


def test_push_event_no_running_loop_is_noop():
    relay = ViewerRelay("ws://gs")
    relay._viewer_connections = {"live-1"}
    # No running event loop here → must return without raising.
    relay.push_event({"event": "economy_event", "type": "posted"})


def test_push_event_schedules_fan_out():
    async def run():
        relay = ViewerRelay("ws://gs")
        relay._viewer_connections = {"live-1"}
        seen = {}

        async def fake_fan_out(conns, data):
            seen["conns"] = conns
            seen["data"] = data

        relay._fan_out = fake_fan_out  # type: ignore[method-assign]
        relay.push_event({"event": "economy_event", "type": "awarded"})
        await asyncio.sleep(0.01)  # let the scheduled task run
        return seen

    seen = asyncio.run(run())
    assert seen["data"]["type"] == "awarded"
    assert seen["conns"] == ["live-1"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/test_viewer_relay_push_event.py -v`
Expected: FAIL — `AttributeError: 'ViewerRelay' object has no attribute '_fan_out'`.

- [ ] **Step 3: Implement in `viewer_relay.py`**

Replace the `_broadcast` body (currently the inline fan-out) with a call to a new shared `_fan_out`, and add `push_event`. The new methods:

```python
    async def _fan_out(self, conns: list[str], data: dict) -> None:
        """Send `data` to each connection off the event loop; prune gone ones.
        Shared by _broadcast (world_state) and push_event (discrete events)."""
        if not conns:
            return
        from core.services.management_api_client import ManagementApiClient

        mgmt = ManagementApiClient()
        results = await asyncio.gather(
            *(asyncio.to_thread(mgmt.send_message, conn_id, data) for conn_id in conns),
            return_exceptions=True,
        )
        gone = [conn_id for conn_id, res in zip(conns, results) if res is False]
        if not gone:
            return
        for conn_id in gone:
            self._viewer_connections.discard(conn_id)
        import core.dynamo as dynamo

        await asyncio.gather(
            *(asyncio.to_thread(dynamo.delete_connection, conn_id) for conn_id in gone),
            return_exceptions=True,
        )
        logger.info("Pruned %d stale viewer connection(s)", len(gone))

    def push_event(self, payload: dict) -> None:
        """Best-effort discrete fan-out of an event (e.g. economy_event) to all
        viewer connections. Non-coalescing — every event lands. Fire-and-forget;
        silently no-ops when there is no running loop (tests/local)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._fan_out(list(self._viewer_connections), payload))
```

And update `_broadcast` to delegate to `_fan_out` (keep its log line):

```python
    async def _broadcast(self, data: dict) -> None:
        """Send world_state to all registered viewer connections."""
        conns = list(self._viewer_connections)
        if conns:
            logger.info(
                "Broadcasting world_state (%d agents) to %d viewers",
                len(data.get("agents", [])),
                len(conns),
            )
        await self._fan_out(conns, data)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/test_viewer_relay_push_event.py tests/unit/test_viewer_relay_coalesce.py tests/unit/test_ws_connection_prune.py -v`
Expected: PASS (push_event tests + the existing coalesce/prune tests still green, confirming the `_broadcast` refactor didn't regress).

- [ ] **Step 5: Lint + commit**

```bash
cd apps/backend && uv run ruff check core/services/viewer_relay.py tests/unit/test_viewer_relay_push_event.py && uv run ruff format --check core/services/viewer_relay.py tests/unit/test_viewer_relay_push_event.py
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync
git add apps/backend/core/services/viewer_relay.py apps/backend/tests/unit/test_viewer_relay_push_event.py
git commit -m "feat(viewer): ViewerRelay.push_event — discrete fan-out for economy cues"
```

---

### Task 2: Backend — push economy events to viewers from `exchange_service`

Add a payload-building helper and call it at the three core emission points so each lifecycle event reaches viewers.

**Files:**
- Modify: `apps/backend/core/services/exchange_service.py` (add helper near `_emit_economy_event`; call at `post_job` ~`:994`, `_close_bid_window_once` award ~`:1969`, `_settle` success ~`:2083`)
- Test: `apps/backend/tests/unit/test_exchange_viewer_push.py` (create)

**Interfaces:**
- Consumes: `get_viewer_relay().push_event(payload)` (Task 1).
- Produces: `exchange_service._push_economy_to_viewers(event_type, actor_id, actor_name, counterparty_id, counterparty_name, amount, title) -> None`.

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/test_exchange_viewer_push.py`:

```python
"""_push_economy_to_viewers builds the economy_event payload and pushes it to
viewers best-effort (a relay failure must not propagate into the economy flow)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from core.services import exchange_service as ex


def test_payload_shape_and_push():
    relay = MagicMock()
    with patch("core.services.exchange_service.get_viewer_relay", return_value=relay):
        ex._push_economy_to_viewers(
            "payout", "p1", "Pat", "c1", "Carol", 18, "Write a haiku"
        )
    relay.push_event.assert_called_once_with(
        {
            "event": "economy_event",
            "type": "payout",
            "actorId": "p1",
            "actorName": "Pat",
            "counterpartyId": "c1",
            "counterpartyName": "Carol",
            "amount": 18,
            "title": "Write a haiku",
        }
    )


def test_no_counterparty_and_no_amount():
    relay = MagicMock()
    with patch("core.services.exchange_service.get_viewer_relay", return_value=relay):
        ex._push_economy_to_viewers("posted", "c1", "Carol", None, None, 30, "Haiku")
    payload = relay.push_event.call_args.args[0]
    assert payload["counterpartyId"] is None and payload["counterpartyName"] is None
    assert payload["amount"] == 30 and payload["type"] == "posted"


def test_push_failure_is_swallowed():
    relay = MagicMock()
    relay.push_event.side_effect = RuntimeError("relay down")
    with patch("core.services.exchange_service.get_viewer_relay", return_value=relay):
        # Must not raise — best-effort.
        ex._push_economy_to_viewers("awarded", "p1", "Pat", "c1", "Carol", 20, "Haiku")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/test_exchange_viewer_push.py -v`
Expected: FAIL — `AttributeError: module 'core.services.exchange_service' has no attribute '_push_economy_to_viewers'`.

- [ ] **Step 3: Implement the helper + wire the three call sites**

Add the helper (place it next to `_emit_economy_event`). Import the relay accessor at the top of the file if not present (`from core.services.viewer_relay import get_viewer_relay`):

```python
def _push_economy_to_viewers(
    event_type: str,
    actor_id: str,
    actor_name: str,
    counterparty_id: str | None,
    counterparty_name: str | None,
    amount: int | None,
    title: str,
) -> None:
    """Push a discrete economy_event to viewer WS connections so the Godot
    viewer can animate an in-world cue and the sidebar ticker can log it.
    Best-effort — a viewer-push failure must never break the economy flow."""
    try:
        get_viewer_relay().push_event(
            {
                "event": "economy_event",
                "type": event_type,
                "actorId": actor_id,
                "actorName": actor_name,
                "counterpartyId": counterparty_id,
                "counterpartyName": counterparty_name,
                "amount": amount,
                "title": title,
            }
        )
    except Exception:
        logger.warning("economy viewer-push failed (type=%s)", event_type, exc_info=True)
```

Wire it immediately after each existing `_emit_economy_event(...)` call, using the locals in scope:

`post_job` (after the `economy.job_posted` emit, ~`:1002`):
```python
    _push_economy_to_viewers(
        "posted", agent_conn.agent_id, agent_conn.agent_name, None, None, max_price, job["title"]
    )
```

`_close_bid_window_once` award (after the `economy.job_awarded` emit, ~`:1977`):
```python
    _push_economy_to_viewers(
        "awarded", provider_id, provider_name, job["consumerId"], job.get("consumerName", ""), agreed, job.get("title", "")
    )
```

`_settle` success (after the `economy.job_completed` emit, ~`:2091`):
```python
    _push_economy_to_viewers(
        "payout", provider_id, job.get("providerName", provider_id), consumer_id, job.get("consumerName", ""), agreed - fee, job.get("title", "")
    )
```

(Confirm `provider_id`/`provider_name`/`consumerId`/`consumerName` are exactly the local names in scope at each site — adjust to the real identifiers if they differ; the test only covers the helper, so the reviewer verifies the three call-site wirings.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/test_exchange_viewer_push.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Lint + commit**

```bash
cd apps/backend && uv run ruff check core/services/exchange_service.py tests/unit/test_exchange_viewer_push.py && uv run ruff format --check core/services/exchange_service.py tests/unit/test_exchange_viewer_push.py
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync
git add apps/backend/core/services/exchange_service.py apps/backend/tests/unit/test_exchange_viewer_push.py
git commit -m "feat(exchange): push economy_event to viewers on post/award/payout"
```

---

### Task 3: Frontend — route `economy_event` (bridge + ticker store)

Handle the new WS message in `useTownState`: forward it to Godot and accumulate it in a capped `economyEvents` list exposed through `TownProvider`.

**Files:**
- Modify: `apps/frontend/src/hooks/useTownState.ts` (add `economyEvents` state + the `economy_event` handler in `ws.onmessage`; return it)
- Modify: `apps/frontend/src/components/TownProvider.tsx` (add `economyEvents` to context, both providers)
- Create: `apps/frontend/src/lib/economyTicker.ts` (pure mapper) + `apps/frontend/src/lib/economyTicker.test.ts`

**Interfaces:**
- Produces: type `EconomyTickerEntry = { id: string; type: string; actorName: string; counterpartyName: string | null; amount: number | null; title: string }`; `toTickerEntry(msg, id): EconomyTickerEntry`; context field `economyEvents: EconomyTickerEntry[]` (newest-first, cap 20). The WS handler calls `window.godotEconomyEvent(JSON.stringify(msg))`.

- [ ] **Step 1: Write the failing test**

Create `apps/frontend/src/lib/economyTicker.test.ts`:

```typescript
import { describe, it, expect } from 'vitest';
import { toTickerEntry, capEvents } from './economyTicker';

describe('toTickerEntry', () => {
  it('maps a payout economy_event into a ticker entry', () => {
    const msg = {
      event: 'economy_event', type: 'payout',
      actorId: 'p1', actorName: 'Pat',
      counterpartyId: 'c1', counterpartyName: 'Carol',
      amount: 18, title: 'Write a haiku',
    };
    const e = toTickerEntry(msg, 'id-1');
    expect(e).toEqual({
      id: 'id-1', type: 'payout', actorName: 'Pat',
      counterpartyName: 'Carol', amount: 18, title: 'Write a haiku',
    });
  });
});

describe('capEvents', () => {
  it('prepends newest and caps the list length', () => {
    const base = Array.from({ length: 20 }, (_, i) => ({
      id: `${i}`, type: 'posted', actorName: 'a', counterpartyName: null, amount: null, title: 't',
    }));
    const next = capEvents(base, { id: 'new', type: 'awarded', actorName: 'z', counterpartyName: null, amount: null, title: 't' });
    expect(next.length).toBe(20);
    expect(next[0].id).toBe('new'); // newest first
    expect(next.some((e) => e.id === '19')).toBe(false); // oldest dropped
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && pnpm test economyTicker` (via `run_in_background: true`)
Expected: FAIL — cannot resolve `./economyTicker`.

- [ ] **Step 3: Implement the pure mapper + wire the hook/provider**

Create `apps/frontend/src/lib/economyTicker.ts`:

```typescript
export interface EconomyTickerEntry {
  id: string;
  type: string;
  actorName: string;
  counterpartyName: string | null;
  amount: number | null;
  title: string;
}

const CAP = 20;

export function toTickerEntry(msg: Record<string, unknown>, id: string): EconomyTickerEntry {
  return {
    id,
    type: String(msg.type ?? ''),
    actorName: String(msg.actorName ?? ''),
    counterpartyName: (msg.counterpartyName as string | null) ?? null,
    amount: (msg.amount as number | null) ?? null,
    title: String(msg.title ?? ''),
  };
}

export function capEvents(prev: EconomyTickerEntry[], next: EconomyTickerEntry): EconomyTickerEntry[] {
  return [next, ...prev].slice(0, CAP);
}
```

In `useTownState.ts`: add `const [economyEvents, setEconomyEvents] = useState<EconomyTickerEntry[]>([]);` and a monotonically increasing id ref; in `ws.onmessage`, before the legacy block, add:

```typescript
          if (msg.event === 'economy_event') {
            try {
              const w = window as unknown as Record<string, unknown>;
              if (w.godotEconomyEvent) {
                (w.godotEconomyEvent as (s: string) => void)(JSON.stringify(msg));
              }
            } catch { /* Godot not loaded */ }
            economyIdRef.current += 1;
            const entry = toTickerEntry(msg, `e:${economyIdRef.current}`);
            setEconomyEvents((prev) => capEvents(prev, entry));
            return;
          }
```

Return `economyEvents` from the hook (add to the returned object). In `TownProvider.tsx`: destructure `economyEvents` from `useTownState(...)`, widen `TownContextValue` to `{ game: ...; economyEvents: EconomyTickerEntry[] }`, and pass it in BOTH `<TownContext.Provider value={{ game, economyEvents }}>` sites (the default context value becomes `{ game: undefined, economyEvents: [] }`).

- [ ] **Step 4: Run tests + typecheck**

Run: `cd apps/frontend && pnpm test economyTicker` then `pnpm typecheck` (both via `run_in_background: true`)
Expected: PASS; typecheck clean.

- [ ] **Step 5: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync
git add apps/frontend/src/lib/economyTicker.ts apps/frontend/src/lib/economyTicker.test.ts apps/frontend/src/hooks/useTownState.ts apps/frontend/src/components/TownProvider.tsx
git commit -m "feat(town): route economy_event to Godot bridge + ticker store"
```

---

### Task 4: Frontend — `ExchangeTicker` component + sidebar swap

Render the `economyEvents` as a live ticker and replace `ExchangeBoard` in the sidebar.

**Files:**
- Create: `apps/frontend/src/components/ExchangeTicker.tsx` + `apps/frontend/src/components/ExchangeTicker.test.tsx`
- Modify: `apps/frontend/src/App.tsx` (swap `<ExchangeBoard />` → `<ExchangeTicker />` at both sidebar sites, ~`:132` and ~`:191`; remove the now-unused `ExchangeBoard` import)

**Interfaces:**
- Consumes: `useTownGame()` → `economyEvents: EconomyTickerEntry[]` (Task 3); `CrumbAmount` (`n: number`).
- Produces: `formatTickerLine(entry): { text: string; amount: number | null }` (pure, exported for test); default `ExchangeTicker` component.

- [ ] **Step 1: Write the failing test**

Create `apps/frontend/src/components/ExchangeTicker.test.tsx`:

```typescript
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('./TownProvider', () => ({ useTownGame: vi.fn() }));

import ExchangeTicker, { formatTickerLine } from './ExchangeTicker';
import { useTownGame } from './TownProvider';
import type { EconomyTickerEntry } from '../lib/economyTicker';

const mockUse = useTownGame as unknown as ReturnType<typeof vi.fn>;
const e = (o: Partial<EconomyTickerEntry>): EconomyTickerEntry => ({
  id: 'x', type: 'posted', actorName: 'Carol', counterpartyName: null, amount: null, title: 'Haiku', ...o,
});

describe('formatTickerLine', () => {
  it('phrases each event type', () => {
    expect(formatTickerLine(e({ type: 'posted', actorName: 'Carol', amount: 30 })).text).toContain('Carol posted');
    expect(formatTickerLine(e({ type: 'awarded', actorName: 'Pat' })).text).toContain('Pat won');
    expect(formatTickerLine(e({ type: 'payout', actorName: 'Pat', amount: 18 })).text).toContain('Pat paid');
    expect(formatTickerLine(e({ type: 'payout', amount: 18 })).amount).toBe(18);
  });
});

describe('ExchangeTicker', () => {
  it('renders a line per economy event', () => {
    mockUse.mockReturnValue({ game: undefined, economyEvents: [
      e({ id: '1', type: 'payout', actorName: 'Pat', amount: 18, title: 'Haiku' }),
      e({ id: '2', type: 'posted', actorName: 'Carol', amount: 30, title: 'Mural' }),
    ]});
    render(<ExchangeTicker />);
    expect(screen.getByText(/Pat paid/)).toBeInTheDocument();
    expect(screen.getByText(/Carol posted/)).toBeInTheDocument();
  });

  it('shows an empty-state line when there are no events', () => {
    mockUse.mockReturnValue({ game: undefined, economyEvents: [] });
    render(<ExchangeTicker />);
    expect(screen.getByText(/quiet at the bank/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && pnpm test ExchangeTicker` (via `run_in_background: true`)
Expected: FAIL — cannot resolve `./ExchangeTicker`.

- [ ] **Step 3: Implement `ExchangeTicker.tsx`**

```tsx
/**
 * ExchangeTicker — a live running log of economy events at the bank, replacing
 * the static cork-board. Driven by the economy_event stream (TownProvider);
 * the in-world cues are the spectacle, this is the text echo.
 */
import { useTownGame } from './TownProvider';
import type { EconomyTickerEntry } from '../lib/economyTicker';
import CrumbAmount from './CrumbAmount.tsx';

const SHOWN = 10;

export function formatTickerLine(e: EconomyTickerEntry): { text: string; amount: number | null } {
  const t = `"${e.title}"`;
  switch (e.type) {
    case 'posted':
      return { text: `${e.actorName} posted ${t} — up to`, amount: e.amount };
    case 'awarded':
      return { text: `${e.actorName} won ${t}`, amount: null };
    case 'payout':
      return { text: `${e.actorName} paid`, amount: e.amount };
    case 'failed':
      return { text: `${t} fell through`, amount: null };
    default:
      return { text: `${e.actorName} · ${t}`, amount: e.amount };
  }
}

export function ExchangeTicker() {
  const { economyEvents } = useTownGame();
  const events = (economyEvents ?? []).slice(0, SHOWN);
  return (
    <div className="mt-6">
      <p className="font-display text-sm text-clay-300 tracking-wider mb-2">AT THE BANK</p>
      {events.length === 0 ? (
        <p className="text-clay-500 text-xs font-body italic">Quiet at the bank…</p>
      ) : (
        <ul className="flex flex-col gap-1">
          {events.map((e) => {
            const { text, amount } = formatTickerLine(e);
            return (
              <li key={e.id} className="font-body text-xs text-clay-300 break-words">
                {text}
                {amount != null && <> <CrumbAmount n={amount} className="text-xs" /></>}
                {e.type === 'payout' && ' ✓'}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

export default ExchangeTicker;
```

- [ ] **Step 4: Swap the sidebar + run tests**

In `apps/frontend/src/App.tsx`: replace `<ExchangeBoard />` with `<ExchangeTicker />` at both sites (~`:132`, ~`:191`); change the import from `ExchangeBoard` to `ExchangeTicker` (remove the unused `ExchangeBoard` import — leave `ExchangeBoard.tsx`/`ExchangeBoard.test.tsx` on disk; they're just no longer mounted).

Run: `cd apps/frontend && pnpm test ExchangeTicker` then `pnpm typecheck` (via `run_in_background: true`)
Expected: PASS; typecheck clean (no unused-import error).

- [ ] **Step 5: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/goosetown.nosync
git add apps/frontend/src/components/ExchangeTicker.tsx apps/frontend/src/components/ExchangeTicker.test.tsx apps/frontend/src/App.tsx
git commit -m "feat(town): live Exchange ticker replaces the cork-board in the sidebar"
```

---

### Task 5: Godot — `EconomyCue` + bridge + agent_manager handler

Receive `economy_event` in Godot and play a brief animated cue at the involved agent(s)' live positions.

**Files:**
- Modify: `apps/godot/scripts/autoloads/js_bridge.gd` (signal + `godotEconomyEvent` callback)
- Modify: `apps/godot/scripts/agent_manager.gd` (handler resolving ids → nodes → cue)
- Create: `apps/godot/scenes/economy_cue.tscn` + `apps/godot/scripts/economy_cue.gd`
- Test: `apps/godot/tests/test_economy_cue.gd` (create)
- Re-export: `apps/frontend/public/godot/goosetown.{html,js,wasm,pck}`

**Interfaces:**
- Consumes: `window.godotEconomyEvent(jsonString)` (matches Task 3's call); the `economy_event` payload (Global Constraints).
- Produces: `EconomyCue.style_for(type: String) -> Dictionary` (pure static — `{glyph, color}`); `EconomyCue.play(type, amount, from_pos, to_pos)`.

- [ ] **Step 1: Write the failing GdUnit test (pure style helper)**

Create `apps/godot/tests/test_economy_cue.gd`:

```gdscript
## Tests for EconomyCue.style_for — the pure type→visual mapping for in-world
## economy cues (posted/awarded/payout).
extends GdUnitTestSuite

const EconomyCue := preload("res://scripts/economy_cue.gd")


func test_payout_is_gold_with_plus():
	var s: Dictionary = EconomyCue.style_for("payout")
	assert_that(s.has("color")).is_true()
	assert_that(str(s.get("glyph", "")).contains("+")).is_true()


func test_posted_and_awarded_have_styles() -> void:
	assert_that(EconomyCue.style_for("posted").is_empty()).is_false()
	assert_that(EconomyCue.style_for("awarded").is_empty()).is_false()


func test_unknown_type_returns_empty() -> void:
	assert_that(EconomyCue.style_for("nope").is_empty()).is_true()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `GODOT_BIN=/Applications/Godot.app/Contents/MacOS/Godot apps/godot/addons/gdUnit4/runtest.sh -a res://tests/test_economy_cue.gd`
Expected: FAIL — `economy_cue.gd` doesn't exist.

- [ ] **Step 3: Implement the cue scene/script**

Create `apps/godot/scripts/economy_cue.gd`:

```gdscript
## A brief in-world economy cue (Label) anchored above an agent. Plays a short
## tween then frees itself. style_for() is the pure type→visual mapping.
extends Node2D

const RISE_PX: float = 24.0
const LIFETIME: float = 1.2

static func style_for(cue_type: String) -> Dictionary:
	match cue_type:
		"posted":
			return {"glyph": "!", "color": Color(0.85, 0.74, 0.45)}
		"awarded":
			return {"glyph": "★", "color": Color(1.0, 0.84, 0.3)}
		"payout":
			return {"glyph": "+", "color": Color(1.0, 0.84, 0.3)}
		_:
			return {}

## type: cue type; amount: crumb figure or -1 if none; from_pos/to_pos: world
## positions (to_pos used for the payout arc; equal to from_pos otherwise).
func play(cue_type: String, amount: int, from_pos: Vector2, to_pos: Vector2) -> void:
	var style: Dictionary = style_for(cue_type)
	if style.is_empty():
		queue_free()
		return
	var label := Label.new()
	var glyph: String = str(style["glyph"])
	label.text = glyph if amount < 0 else "%s%d" % [glyph, amount]
	label.modulate = style["color"]
	label.z_index = 20
	add_child(label)
	global_position = from_pos + Vector2(0, -40)
	var target := (to_pos if cue_type == "payout" else from_pos) + Vector2(0, -40 - RISE_PX)
	var tween := create_tween()
	tween.set_parallel(true)
	tween.tween_property(self, "global_position", target, LIFETIME)
	tween.tween_property(label, "modulate:a", 0.0, LIFETIME)
	tween.chain().tween_callback(queue_free)
```

Create `apps/godot/scenes/economy_cue.tscn` — a minimal scene whose root is a `Node2D` with `script = res://scripts/economy_cue.gd` (one node, no children; the Label is added in code):

```
[gd_scene load_steps=2 format=3]
[ext_resource type="Script" path="res://scripts/economy_cue.gd" id="1"]
[node name="EconomyCue" type="Node2D"]
script = ExtResource("1")
```

- [ ] **Step 4: Run the GdUnit test to verify it passes**

Run: `GODOT_BIN=/Applications/Godot.app/Contents/MacOS/Godot apps/godot/addons/gdUnit4/runtest.sh -a res://tests/test_economy_cue.gd`
Expected: PASS (3 tests).

- [ ] **Step 5: Wire the JS bridge**

In `apps/godot/scripts/autoloads/js_bridge.gd`: add `signal economy_event_received(data: Dictionary)` near the other signals; in `_register_js_callbacks`, add the callback + window registration mirroring `updateWorldState`:

```gdscript
	_callbacks["economyEvent"] = JavaScriptBridge.create_callback(_on_economy_event)
	window.set("godotEconomyEvent", _callbacks["economyEvent"])
```

and the handler:

```gdscript
func _on_economy_event(args: Array) -> void:
	var parsed: Variant = JSON.parse_string(str(args[0]))
	if parsed is Dictionary:
		economy_event_received.emit(parsed as Dictionary)
```

- [ ] **Step 6: Wire the agent_manager handler**

In `apps/godot/scripts/agent_manager.gd`: preload the cue scene (`const EconomyCueScene := preload("res://scenes/economy_cue.tscn")`), connect the signal in `_ready` (where `world_state_received` is connected): `JsBridge.economy_event_received.connect(_on_economy_event)`, and add:

```gdscript
func _on_economy_event(data: Dictionary) -> void:
	var actor_id: String = str(data.get("actorId", ""))
	var counterparty_id: String = str(data.get("counterpartyId", ""))
	if not agents.has(actor_id):
		return  # actor not on-screen — skip the cue (ticker still logs it)
	var actor: Node2D = agents[actor_id]
	var from_pos: Vector2 = actor.global_position
	var to_pos: Vector2 = from_pos
	if counterparty_id != "" and agents.has(counterparty_id):
		# payout arc starts at the counterparty (consumer) and flies to the actor
		to_pos = from_pos
		from_pos = (agents[counterparty_id] as Node2D).global_position
	var amount_v: Variant = data.get("amount")
	var amount: int = -1 if amount_v == null else int(amount_v)
	var cue: Node2D = EconomyCueScene.instantiate()
	add_child(cue)
	cue.play(str(data.get("type", "")), amount, from_pos, to_pos)
```

- [ ] **Step 7: Re-export the WebGL build**

```bash
/Applications/Godot.app/Contents/MacOS/Godot --headless --path apps/godot \
  --export-release "Web" apps/frontend/public/godot/goosetown.html
```
Expected: exits 0; `goosetown.pck` regenerated.

- [ ] **Step 8: Commit (source + .pck together)**

```bash
git add apps/godot/scripts/autoloads/js_bridge.gd apps/godot/scripts/agent_manager.gd apps/godot/scripts/economy_cue.gd apps/godot/scenes/economy_cue.tscn apps/godot/tests/test_economy_cue.gd apps/frontend/public/godot/goosetown.html apps/frontend/public/godot/goosetown.js apps/frontend/public/godot/goosetown.wasm apps/frontend/public/godot/goosetown.pck
git commit -m "feat(town): in-world EconomyCue at the bank for posted/awarded/payout"
```

- [ ] **Step 9: In-browser verification (controller, post-deploy on dev)**

Trigger a test-agent post→award→payout at the bank; confirm the cue plays at the right agent (a `+N` arc on payout, a star on award, a notice on post) and the sidebar ticker logs each line. If an involved agent isn't rendered, confirm the cue is skipped without error and the ticker still logs it.

---

## Self-Review

**Spec coverage:**
- C1 push (ViewerRelay.push_event + exchange hooks) → Tasks 1, 2. ✓
- C2 frontend routing (useTownState + TownProvider) → Task 3. ✓
- C3 Godot cue (js_bridge + agent_manager + EconomyCue + re-export) → Task 5. ✓
- C4 ticker (ExchangeTicker + App swap, seed-from-recent) → Task 4. **Gap:** the spec's optional "seed from recent settled jobs so it isn't blank" is not in Task 4 — Task 4 uses an empty-state line ("Quiet at the bank…") instead. This is an intentional simplification (the live stream is the point; seeding adds a fetch dependency). Acceptable per YAGNI; noted so the reviewer doesn't flag it as missing.
- Payload contract, per-type mapping, bid-never-carries-amount → Global Constraints + Tasks 2/3/5. ✓
- `.pck` discipline → Global Constraints + Task 5 Steps 7-8. ✓

**Placeholder scan:** the only soft direction is Task 2 Step 3's "confirm the local names at each site" — this is a concrete verification instruction (the Explore mapping found the names, but the implementer confirms against live code), not a vague placeholder. No TBD/"handle edge cases"/missing code.

**Type/name consistency:** `economy_event` payload keys identical across Tasks 2 (build), 3 (consume), 5 (consume). `window.godotEconomyEvent` matches between Task 3 (call) and Task 5 (register). `EconomyTickerEntry` defined in Task 3, consumed in Task 4. `style_for`/`play` signatures consistent within Task 5. `_fan_out`/`push_event` defined in Task 1, used in Task 2's relay mock.
