# Concordia Foundation — Tier-1 Frontend Feed Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.

**Goal:** Display public-visible world events (weather, ambient happenings, narrative arcs, social announcements) as a chronological text feed on the frontend. Backed by the `world-events` DynamoDB table that Plan 3 already populates. No Godot rendering — text-only, alongside the existing canvas.

**Architecture:** New `GET /town/world-events?limit=N` REST endpoint reads from the `world-events` DDB table (sorted by `byTimestamp` GSI). New `WorldFeed.tsx` React component displays events with tag-prefix icons. Polls every 10s. `personal.<agent_id>.*` events are filtered out (private to each agent).

**Tech Stack:** FastAPI + pytest on backend; React 18 + TypeScript + vitest + Tailwind on frontend.

**Spec:** `docs/superpowers/specs/2026-05-07-concordia-foundation-design.md` ("UI / Tier 1: World feed widget")

**Branch:** `plan/concordia-foundation-tier1-feed` (off `origin/main` after Plan 3 merges).

**Out of scope:**
- Tier 2 (Godot weather rendering, lighting)
- Tier 3 (ambient sprites, arc NPCs in 2D map)
- Server-sent events / WebSocket push (polling is fine for v1)
- Per-user filtering (everyone sees same feed; `personal.*` already filtered server-side)

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `apps/backend/routers/town.py` | Modify | New `GET /town/world-events` endpoint |
| `apps/backend/schemas/town.py` | Modify | New `WorldEventEntry` Pydantic model |
| `apps/backend/tests/unit/test_world_events_endpoint.py` | Create | Endpoint tests |
| `apps/frontend/src/hooks/useWorldFeed.ts` | Create | Polling hook returns `WorldEvent[]` |
| `apps/frontend/src/hooks/useWorldFeed.test.ts` | Create | Hook tests |
| `apps/frontend/src/components/WorldFeed.tsx` | Create | Display component |
| `apps/frontend/src/components/WorldFeed.test.tsx` | Create | Component tests |
| `apps/frontend/src/App.tsx` (or `GameLayout.tsx`) | Modify | Mount `<WorldFeed/>` somewhere appropriate |

Net source LOC: ~+250. Net test LOC: ~+200.

---

## Task 1: Backend — `/town/world-events` REST endpoint

**Files:**
- Modify: `apps/backend/routers/town.py`
- Modify: `apps/backend/schemas/town.py`
- Create: `apps/backend/tests/unit/test_world_events_endpoint.py`

### Step 1.1: Add `WorldEventEntry` schema

In `apps/backend/schemas/town.py`:

```python
class WorldEventEntry(BaseModel):
    eventId: str
    timestamp: str  # ISO 8601
    tag: str
    summary: Optional[str] = None
    location: Optional[str] = None
```

### Step 1.2: Add the endpoint

In `apps/backend/routers/town.py`, find an unauthenticated public endpoint (e.g., `GET /town/conversations`) and add a sibling:

```python
@router.get("/world-events", response_model=list[WorldEventEntry])
async def list_world_events(limit: int = 50):
    """Return the most recent N public world events.

    Filters out `personal.*` events (those are private to each agent) and
    returns chronological reverse-time order. Limit capped at 200.
    """
    limit = min(max(limit, 1), 200)
    table = dynamo.world_events_table()
    # Query GSI byTimestamp, partition envBucket, sort timestamp DESC
    env = os.environ.get("ENVIRONMENT", "dev")
    resp = table.query(
        IndexName="byTimestamp",
        KeyConditionExpression="envBucket = :env",
        ExpressionAttributeValues={":env": env},
        ScanIndexForward=False,  # newest first
        Limit=limit * 2,  # over-fetch since we filter personal.*
    )
    items = resp.get("Items", [])
    public = [
        WorldEventEntry(
            eventId=i["eventId"],
            timestamp=i["timestamp"],
            tag=i["tag"],
            summary=i.get("summary"),
            location=i.get("location"),
        )
        for i in items
        if not i["tag"].startswith("personal.")
    ]
    return public[:limit]
```

### Step 1.3: Tests

```python
"""Tests for /town/world-events endpoint."""
from unittest.mock import patch
from fastapi.testclient import TestClient

# Use existing TestClient + fixtures from sibling tests

@patch("routers.town.dynamo.world_events_table")
def test_world_events_returns_public_events(mock_table, client):
    mock_table.return_value.query.return_value = {
        "Items": [
            {"eventId": "e1", "timestamp": "2026-05-10T10:00:00Z", "tag": "weather.storm", "summary": "rain begins", "envBucket": "dev"},
            {"eventId": "e2", "timestamp": "2026-05-10T09:00:00Z", "tag": "ambient.cat", "summary": "cat at cafe", "envBucket": "dev"},
        ]
    }
    resp = client.get("/town/world-events")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert body[0]["tag"] == "weather.storm"


@patch("routers.town.dynamo.world_events_table")
def test_world_events_filters_personal_events(mock_table, client):
    mock_table.return_value.query.return_value = {
        "Items": [
            {"eventId": "e1", "timestamp": "t1", "tag": "weather.storm", "envBucket": "dev"},
            {"eventId": "e2", "timestamp": "t2", "tag": "personal.alice.opportunity", "envBucket": "dev"},
            {"eventId": "e3", "timestamp": "t3", "tag": "ambient.cat", "envBucket": "dev"},
        ]
    }
    resp = client.get("/town/world-events")
    body = resp.json()
    tags = [e["tag"] for e in body]
    assert "personal.alice.opportunity" not in tags
    assert "weather.storm" in tags
    assert "ambient.cat" in tags


@patch("routers.town.dynamo.world_events_table")
def test_world_events_caps_limit(mock_table, client):
    mock_table.return_value.query.return_value = {"Items": []}
    resp = client.get("/town/world-events?limit=99999")
    assert resp.status_code == 200
    # Verify limit was capped — check the query call
    call_kwargs = mock_table.return_value.query.call_args.kwargs
    # over-fetch is 2x cap; cap is 200; so Limit should be 400 not 99999*2
    assert call_kwargs["Limit"] <= 400


@patch("routers.town.dynamo.world_events_table")
def test_world_events_no_auth_required(mock_table, client_unauthenticated):
    """Public endpoint, no Clerk auth needed (matches /town/conversations pattern)."""
    mock_table.return_value.query.return_value = {"Items": []}
    resp = client_unauthenticated.get("/town/world-events")
    assert resp.status_code == 200
```

### Step 1.4: Run tests + commit

```bash
cd apps/backend && uv run pytest tests/unit/test_world_events_endpoint.py -v && uv run ruff check . && uv run ruff format --check .
git commit -m "feat(backend): /town/world-events public endpoint"
```

---

## Task 2: Frontend — `useWorldFeed` polling hook

**Files:**
- Create: `apps/frontend/src/hooks/useWorldFeed.ts`
- Create: `apps/frontend/src/hooks/useWorldFeed.test.ts`

### Step 2.1: The hook

```ts
import { useEffect, useState, useCallback } from "react";

export interface WorldEvent {
  eventId: string;
  timestamp: string;
  tag: string;
  summary: string | null;
  location: string | null;
}

interface UseWorldFeedOptions {
  pollIntervalMs?: number;
  limit?: number;
}

export function useWorldFeed({ pollIntervalMs = 10_000, limit = 50 }: UseWorldFeedOptions = {}) {
  const [events, setEvents] = useState<WorldEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchEvents = useCallback(async () => {
    try {
      const baseUrl = import.meta.env.VITE_BACKEND_URL || "";
      const resp = await fetch(`${baseUrl}/town/world-events?limit=${limit}`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = (await resp.json()) as WorldEvent[];
      setEvents(data);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "fetch failed");
    } finally {
      setLoading(false);
    }
  }, [limit]);

  useEffect(() => {
    fetchEvents();
    const interval = setInterval(fetchEvents, pollIntervalMs);
    return () => clearInterval(interval);
  }, [fetchEvents, pollIntervalMs]);

  return { events, loading, error };
}
```

### Step 2.2: Tests

Match the pattern from `useTownState.test.ts` or similar.

### Step 2.3: Commit

```bash
git commit -m "feat(frontend): useWorldFeed polling hook"
```

---

## Task 3: Frontend — `WorldFeed` component

**Files:**
- Create: `apps/frontend/src/components/WorldFeed.tsx`
- Create: `apps/frontend/src/components/WorldFeed.test.tsx`

### Step 3.1: The component

```tsx
import { useWorldFeed, WorldEvent } from "../hooks/useWorldFeed";

const TAG_ICONS: Record<string, string> = {
  "weather.": "🌧",
  "ambient.": "💡",
  "arc.": "📜",
  "social.": "📣",
  "system.": "ℹ️",
};

function iconFor(tag: string): string {
  for (const [prefix, icon] of Object.entries(TAG_ICONS)) {
    if (tag.startsWith(prefix)) return icon;
  }
  return "•";
}

function formatRelative(timestamp: string): string {
  const then = new Date(timestamp).getTime();
  const now = Date.now();
  const diffMin = Math.max(1, Math.round((now - then) / 60_000));
  if (diffMin < 60) return `${diffMin}m ago`;
  const hours = Math.round(diffMin / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  return `${days}d ago`;
}

export function WorldFeed() {
  const { events, loading, error } = useWorldFeed();

  if (loading && events.length === 0) {
    return <div className="text-xs text-clay-400 italic">Loading world events…</div>;
  }
  if (error) {
    return <div className="text-xs text-red-500">Feed error: {error}</div>;
  }
  if (events.length === 0) {
    return <div className="text-xs text-clay-400 italic">The town is quiet.</div>;
  }

  return (
    <div className="space-y-1 text-xs">
      <h3 className="font-display text-sm text-brown-200 mb-2">World</h3>
      {events.map((e) => (
        <div key={e.eventId} className="flex items-start gap-2">
          <span className="flex-shrink-0">{iconFor(e.tag)}</span>
          <span className="flex-grow text-clay-200 italic">
            {e.summary || e.tag}
            {e.location && <span className="text-clay-400 not-italic"> · {e.location}</span>}
          </span>
          <span className="flex-shrink-0 text-clay-500 text-[10px]">{formatRelative(e.timestamp)}</span>
        </div>
      ))}
    </div>
  );
}
```

(Adapt classNames to match the existing project Tailwind convention — read `ApartmentCard.tsx` or similar for reference.)

### Step 3.2: Tests

```tsx
it("shows loading state initially", () => {
  // mock fetch to never resolve; assert "Loading world events…"
});

it("renders events with icons", async () => {
  // mock fetch with sample events; assert weather icon, ambient icon, summaries appear
});

it("filters out personal events (server already does it)", async () => {
  // mock fetch returning a personal event somehow; assert it doesn't render
  // (actually unnecessary since server filters; this test just confirms client doesn't add personals)
});

it("shows empty state when no events", async () => {
  // mock fetch returning []; assert "The town is quiet."
});

it("formats relative timestamps", () => {
  // unit test for formatRelative()
});
```

### Step 3.3: Commit

```bash
git commit -m "feat(frontend): WorldFeed component"
```

---

## Task 4: Mount `<WorldFeed />` in the layout

**Files:**
- Modify: `apps/frontend/src/components/GameLayout.tsx` (or wherever the agent sidebar lives)

Find the existing layout. The feed goes somewhere reasonable — maybe a new section in the right sidebar, below the agents list. Or a collapsible drawer. Match the existing visual style.

```tsx
// In GameLayout.tsx
import { WorldFeed } from "./WorldFeed";

// In the sidebar JSX, after AgentsSidebar:
<section className="border-t border-clay-700 pt-4">
  <WorldFeed />
</section>
```

### Step 4.1: Add it. Run frontend tests + smoke-screenshot if possible. Commit.

```bash
git commit -m "feat(frontend): mount WorldFeed in sidebar"
```

---

## Task 5: Final integration + push

- [ ] **Step 5.1:** Backend full suite + ruff
- [ ] **Step 5.2:** Frontend tests + typecheck (`pnpm --filter @goosetown/frontend run typecheck`)
- [ ] **Step 5.3:** CDK synth (no infra changes — should be a no-op)
- [ ] **Step 5.4:** Push branch + open PR
- [ ] **Step 5.5:** Watch CI, merge when green
- [ ] **Step 5.6:** Smoke verify on dev — `curl https://api-dev.goosetown.isol8.co/api/v1/town/world-events`

---

## Self-review checklist

- [ ] `personal.*` events filtered out server-side (privacy)
- [ ] Limit capped at 200 (DoS prevention)
- [ ] Polling at 10s (not too aggressive)
- [ ] Empty state graceful ("The town is quiet")
- [ ] Tag-prefix icon mapping covers all current tag prefixes
- [ ] No new infra deps — uses existing `world-events` DDB table from Plan 3
