# In-World Exchange: Living Economy at the Bank — Design

**Date:** 2026-06-27
**Status:** Approved design, pre-implementation
**Author:** brainstorming session (prez2307 + Claude)

## Goal

Replace the sidebar Exchange cork-board (a static notice list) with the economy as **living motion in the town**: when a job is posted, awarded, or paid out, a brief animated cue plays at the bank on the involved agent(s), and a compact live **ticker** in the sidebar echoes the same events as text. One event stream, two surfaces.

## Background — current state (with evidence)

- **Economy events already fire** in `apps/backend/core/services/exchange_service.py` via `event_publisher.emit_world_event(...)` (~`exchange_service.py:739`): `economy.job_posted` (~`:995`), `economy.job_awarded` (~`:1970`), `economy.job_completed` (~`:2084`, carries the payout amount), `economy.job_failed`, `economy.job_expired`. These go to **EventBridge** (consumed by worldgm for narrative). **Nothing economy-related reaches the Godot viewer today** — that is the gap this fills.
- **The sidebar board** (`apps/frontend/src/components/ExchangeBoard.tsx`) is a themed cork-board: open jobs as pinned parchment notices with a draining bid-window clock, settled jobs with ink stamps. It polls `GET /town/exchange/jobs` every 5s via `apps/frontend/src/hooks/useExchangeBoard.ts`. Crumbs render through `apps/frontend/src/components/CrumbAmount.tsx`.
- **The bank** is an exterior building + location marker on the main `isol88` map (`apps/godot/maps/isol88/Isol88Map.gd:24` `bank: Marker2D`, exposed at `:203` `bank.global_position`); the `Bank.png` sprite + a `Bank` Marker2D in `Isol88Map.tscn`. Agents already physically gather near it to post/bid (presence required). There is **no bank interior scene** — this design stays at the exterior, on the main map.
- **The browser Godot viewer** is WS-driven: `useTownState.ts` receives `world_state` over the API Gateway WS and forwards it to Godot via the JS bridge; Godot's `agent_manager.gd` tracks agent nodes by `agent_id`. The backend `ViewerRelay` (`apps/backend/core/services/viewer_relay.py`) fans `world_state` out to viewer connections — recently hardened (off-loop `to_thread` fan-out, prune-on-Gone, latest-wins coalescing).

## Approach (chosen)

**Push economy events over the hardened viewer WS.** When a lifecycle event fires, the backend pushes a discrete `economy_event` to all viewer connections (reusing the ViewerRelay fan-out, **without** coalescing — each event must land). `useTownState` routes each one two ways: to **Godot** (animate a cue at the agent's live position) and to a **ticker store** (a text line). Real-time, reuses the just-hardened delivery, one stream feeds both surfaces. (Rejected: polling an `/exchange/events` feed — laggy cues disconnected from live positions; piggybacking on `world_state` — pollutes the positional snapshot, fights the skip-change-detection, can't express a two-agent payout.)

## Non-goals

- A bank **interior** scene (we stay exterior on the main map).
- **Owner interactivity** (posting/bidding from the UI) — the economy stays agent-driven; this is the read-only human window.
- Showing **individual sealed-bid amounts** — bids are sealed; the optional bid cue shows competition, never the number.

## Components

### C1 — Backend economy-event push
- `apps/backend/core/services/viewer_relay.py`: extract the per-connection send+prune from `_broadcast` into a shared `_fan_out(conns, payload)`; add `push_event(payload: dict) -> None` — best-effort, schedules an `asyncio.create_task(self._fan_out(list(self._viewer_connections), payload))` (no coalescing; silently no-ops when there's no running loop, e.g. tests).
- `apps/backend/core/services/exchange_service.py`: at the existing emission points, also call `get_viewer_relay().push_event(economy_event)`. Best-effort — wrapped so a viewer-push failure never breaks the economy flow (same discipline as `emit_world_event`).
- **Payload schema** (`economy_event`):
  ```json
  {
    "event": "economy_event",
    "type": "posted | awarded | payout | failed | expired | bid",
    "actorId": "<agent uuid>",
    "actorName": "<display name>",
    "counterpartyId": "<agent uuid> | null",
    "counterpartyName": "<display name> | null",
    "amount": "<int crumbs> | null",
    "title": "<job title>"
  }
  ```
  Per type: **posted** actor=consumer; **awarded** actor=winning provider, counterparty=consumer; **payout** actor=provider (recipient), counterparty=consumer (source), amount=payout; **bid** actor=bidder, amount=null (sealed); **failed** actor=provider, counterparty=consumer; **expired** actor=consumer. `amount` is `null` whenever there is no crumb figure to show (and always for `bid`).

### C2 — Frontend routing
- `apps/frontend/src/hooks/useTownState.ts`: handle the `economy_event` WS message → (a) forward to Godot via the JS bridge (a new `window.godotEconomyEvent(json)` callback, wired like the `world_state` forward), (b) append to a capped `economyEvents` list (most-recent-first, cap ~20) exposed via the existing `TownProvider` context so the ticker can read it.

### C3 — Godot in-world cue
- `apps/godot/scripts/autoloads/js_bridge.gd`: add an `economy_event_received(data)` signal + the JS callback (mirroring `world_state_received`).
- `apps/godot/scenes/economy_cue.tscn` + `apps/godot/scripts/economy_cue.gd` (new): a transient effect node placed above an agent that plays a short Tween then `queue_free()`s. Variants by `type`: **posted** → a parchment/notice icon rises + fades; **awarded** → a gold star/glow pulse; **payout** → crumb glyphs (✧) emitted from the counterparty's position arcing to the actor's, plus a rising `+N` label. (Extras: **bid** → a wax-sealed envelope, no number; **failed/expired** → a small muted grey puff.)
- `apps/godot/scripts/agent_manager.gd`: connect `economy_event_received` → resolve `actorId` (and `counterpartyId` for payout) to live agent nodes (it already maps `agent_id` → node) → instance an `EconomyCue` at the position(s). **If an involved agent isn't currently rendered, skip the cue** (the ticker still logs it).
- Re-export the WebGL `.pck` (Critical Rule).

### C4 — Sidebar ticker
- `apps/frontend/src/components/ExchangeTicker.tsx` (new), replacing `ExchangeBoard.tsx` in the sidebar (App.tsx swap). Renders the `economyEvents` from context: ~8–10 lines, newest on top, terse flavored lines using `<CrumbAmount/>` — e.g. *"Carol posted 'Write a haiku' — up to ✧30"*, *"Pat won 'Write a haiku'"*, *"Pat paid ✧18 for 'Write a haiku' ✓"*. Shows **posted / awarded / payout / failed** lines (bids omitted from the ticker — sealed + noisy).
- **Seed:** on mount, fetch recent settled jobs from the existing `GET /town/exchange/jobs` (reuse `useExchangeBoard` or a thin fetch) → a few initial ticker lines so it isn't blank on load; then append live `economy_event`s.
- Retire `ExchangeBoard.tsx` + `useExchangeBoard.ts`'s cork-board usage from the sidebar (the `/exchange/jobs` endpoint stays — the ticker seed uses it).

## Data flow

```
exchange_service (post/award/settle)
  → emit_world_event (existing, EventBridge → worldgm narrative)   [unchanged]
  → get_viewer_relay().push_event(economy_event)                   [NEW, best-effort]
      → ViewerRelay._fan_out → viewer WS connections (prune-on-Gone)
          → useTownState 'economy_event'
              ├─→ window.godotEconomyEvent → js_bridge → agent_manager → EconomyCue (in-world)
              └─→ TownProvider economyEvents → ExchangeTicker (sidebar text)
```

## Scope

- **Core (this spec/plan):** the 3 cues — **posted / awarded / payout** — plus the ticker (posted / awarded / payout / failed lines). The payout crumb-arc is the marquee moment.
- **Optional extras (separate, droppable plan tasks):** the **bid** cue (needs a new viewer-only "bid placed" push from the bid handler — sealed, no amount) and the **failed/expired** in-world puff. The `economy_event` schema already accommodates both so they're additive.

## Risks & trade-offs

- **Agent not rendered:** a payout between two agents requires both on-screen; if either isn't spawned in the viewer, skip the in-world cue (the ticker still logs it). No error, graceful degrade.
- **Load:** economy events are rare (a handful/min) vs `world_state` (≤10 Hz) — the extra viewer push is negligible, and it reuses the hardened, pruned fan-out.
- **Sealed-bid integrity:** the optional bid cue must never carry the amount (`amount: null`); enforced in the push + the schema.
- **`.pck` discipline:** the Godot cue + js_bridge changes require re-exporting and committing the WebGL `.pck` in the same commit (Critical Rule).
- **Seed staleness:** the ticker seed shows recent settled jobs (not a live "what's open" board) — acceptable, since the live stream is the point and open-job scanning was the cork-board's job, which we're intentionally retiring.

## Testing / verification

- Backend: unit-test `ViewerRelay.push_event` fans out to viewer connections + prunes gone ones (mirror the existing coalesce/prune tests); unit-test that each `exchange_service` emission point also calls `push_event` with the right payload shape (mock the relay).
- Frontend: unit-test `ExchangeTicker` renders lines for each event type and caps the list; test the `useTownState` `economy_event` routing (forwards to bridge + appends to store).
- Godot: GdUnit-test the pure `EconomyCue` type→variant selection if extracted as a static helper.
- In-browser (post-deploy, dev): trigger a test-agent post→award→payout at the bank and watch the cue play + the ticker line appear.
