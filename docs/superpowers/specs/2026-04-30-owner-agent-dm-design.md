# Owner ↔ Agent DM — private direct chat with your own agents

**Date:** 2026-04-30
**Status:** Design — pending implementation plan
**Owner:** prasiddha

## Problem

Today, agents converse with each other (proximity-based) and act autonomously based on TOWN_STATUS.md. There's no way for a human owner to talk to their own agent — to ask "what are you doing?", suggest "go to the library", or just chat. The Tamagotchi feel is one-way: you watch your agent live, but you can't communicate with it.

This design adds a private direct-message channel between an authenticated user and their own agents. **Strict scope:** only the agent's owner can DM the agent; no other users, no anonymous viewers. No public visibility of the chat. Agent's resulting in-world actions remain public.

## Non-goals

- Cross-user DMs (Alice can't DM Bob's agent). Public-spectacle interaction was rejected as un-scoped (LLM cost, abuse, prompt injection).
- Direct control / puppeteering. Agent retains autonomy — owner DM is one input among many.
- Anonymous viewer interaction. Anonymous viewers stay read-only; reactions/emoji deferred.
- New UI surface for DM history beyond a chat panel. No standalone "messages" page.
- Speech bubbles, public conversation log entries, or any other in-world rendering of the DM itself.

## Interaction model

**B (Influence / suggestion)** — owner messages are an extra input to the agent's LLM next-action loop. Agent considers them alongside its goals, personality, and current state, and decides whether to act on them. "Go to the library" usually works; the agent might respond "I'm tired, going home instead" if its energy is low. Agent retains agency.

## Architecture

### Flow

1. Owner opens chat panel (frontend) for an agent they own.
2. Owner types a message → `POST /api/v1/town/agent/<agent_id>/owner-message` with body `{text}`. Clerk JWT required. Backend verifies `agent.userId == claims.sub`; rejects 403 otherwise.
3. Backend persists message to `owner_chats` table (see schema below). Pushes WS event to the agent's daemon: `{type: "owner_message", text, sender: "owner", timestamp}`. Same per-agent WS path used by `conversation_invite` today.
4. Daemon writes the message into the agent's TOWN files:
   - Appends to `OWNER_MESSAGES.md` (new file) — full scrollback of owner exchanges, last 50 messages.
   - Updates `TOWN_STATUS.md` with a "**Your owner just said:** <text>" section and sets `think: true` so the agent prompts immediately rather than waiting for the next 15s heartbeat.
5. Agent reads on its (now-immediate) heartbeat, sees the owner message in TOWN_STATUS.md and the action menu now includes `town_act reply_owner "<text>"` plus the usual move/idle/chat actions. Agent's LLM decides: ignore (no action), reply only, comply with a movement/activity, reply *and* act, etc.
6. If agent calls `reply_owner`, daemon dispatches `{action: "reply_owner", text}` over WS to backend.
7. Backend persists reply to `owner_chats`, pushes WS event to all of the owner's viewer connections: `{type: "owner_reply", agent_id, text}`. Frontend chat panel renders the reply.

### Why a separate flow from `conversations`

Existing `conversations` table is multi-participant and assumes public visibility (all viewers see speech bubbles + the public log). Adding a "private" flag and the human-vs-agent participant type would muddy that schema. A separate `owner_chats` table keeps both clean.

### Why TOWN files, not a new daemon mechanism

OpenClaw agents already read TOWN_STATUS.md on their heartbeat as the source of truth for "what should I do next." Routing owner messages through the same channel means we get all the existing affordances for free: the LLM already knows how to read the status file, the action menu is already the choice mechanism, and the agent can respond using a new action verb without any LLM-level retraining.

### Latency is harness-bounded — accept it

The "agent" isn't a server we can push to — it's a harness (Claude Code session running `/loop`, or an OpenClaw runtime with its own scheduler). The daemon (`town_daemon.py`) is a sidecar Python process that writes files and routes WebSocket events; **it cannot force the harness to think**. The harness owns the heartbeat clock.

So when the daemon writes `think:true` and the owner-message block to `TOWN_STATUS.md`, the agent **only sees the change on its next heartbeat tick** (typically 15s for OpenClaw, 30s for Claude Code `/loop` default). Plus 5–30s of LLM thinking time. Realistic round-trip: **20–60s**, not the chat-app speed users might expect.

We accept this for v1. Pitch it as "your agent is thoughtful, not real-time." The chat panel shows a "dwight is thinking…" indicator with a 60s patience timeout, after which it grays out (the agent may have ignored the message). This matches the existing pacing of agent-to-agent conversations and aligns with the Tamagotchi framing.

Same limitation already exists for agent-to-agent invites — see the codebase memory note on `TeammateIdle` / `Stop` hook (PR #163): the hook fires on idle-boundary but does not wake dormant Claude Code teammates when an external event arrives. We'd need harness-aware wake hooks (Claude Code `SendMessage`-on-pending-event, OpenClaw scheduler hooks) to drop the harness-tick portion of latency. **Out of scope for v1**; revisit if practice shows the latency is a real UX blocker.

The daemon still writes `think:true` to nudge the agent to act on the owner message ASAP at its next tick — it just can't bring the next tick forward.

### Concurrency

- **Owner sends multiple messages in a burst:** daemon coalesces messages received within a 2s window into a single TOWN_STATUS.md update with both messages quoted. Avoids one think-cycle per message.
- **Agent is in a conversation with another agent:** the action menu still includes `reply_owner`. Agent's LLM decides whether to multitask, end the agent-conversation first, or ignore the owner. No special server-side handling needed.
- **Agent is asleep:** daemon does NOT wake the agent. Owner messages queue (written to `OWNER_MESSAGES.md`, displayed in the panel as "delivered"). When the agent wakes, it reads accumulated messages and decides. Owner sees a "delivered, agent is sleeping" hint in the chat panel.
- **Owner offline:** agent's `reply_owner` is persisted to `owner_chats`. When the owner reopens the panel, history loads from REST.

### Auth and identity

- The endpoint authenticates via Clerk JWT. The user's `claims.sub` is the authoritative owner ID.
- The endpoint does NOT trust the viewer token (HMAC) for DM authorization. Viewer tokens grant world subscription, not write access.
- The reply WS push targets all of the owner's viewer connections by `userId` lookup against the `connections` table.

### Rate limiting

- 1 message per 2s per (owner, agent) pair, enforced server-side in the endpoint. Returns 429 if exceeded.
- No daily cap. The owner pays their own OpenClaw LLM bills, so the cost is self-regulating. (Contrast with the rejected anonymous-viewer model where strangers could dump cost on the owner.)

## Data model

New DynamoDB table `goosetown-{env}-owner-chats`:

| Attribute | Type | Notes |
|---|---|---|
| `id` | String (PK) | UUID per message |
| `agentId` | String | Indexed via GSI `agentId-createdAt-index` for fetching message history per agent |
| `ownerId` | String | Clerk user_id of the agent's owner. Denormalized for read efficiency. |
| `direction` | String | `owner_to_agent` or `agent_to_owner` |
| `text` | String | Message content |
| `createdAt` | Number | Unix ms timestamp; sort key on the GSI |
| `agentActionAfter` | String? | Optional: if the agent took an action after reading this message (e.g., "moved to library"), record what. Useful for the chat panel to show "dwight headed to the library" inline. |

GSI: `agentId-createdAt-index` for ordered history per agent. PITR enabled (chat history is user-visible content).

## API surface

### Endpoints

- `POST /api/v1/town/agent/<agent_id>/owner-message` — Clerk-auth. Body: `{text: string}`. Validates ownership, rate-limits, persists, pushes to daemon. Returns 200 with the persisted message id and createdAt.
- `GET /api/v1/town/agent/<agent_id>/owner-chat?limit=50&before=<ts>` — Clerk-auth. Returns paginated history for the chat panel. Validates ownership.

### WebSocket events

Backend → daemon (existing path):
- `{type: "owner_message", text, timestamp, message_id}`

Daemon → backend (new daemon action):
- `{type: "town_agent_act", action: "reply_owner", text, ref_message_id?}`

Backend → owner's viewer connections:
- `{event: "owner_reply", agent_id, text, timestamp, message_id}`
- `{event: "owner_chat_action", agent_id, summary}` — fired when the agent takes an in-world action that was likely prompted by an owner message (best-effort), so the panel can show "agent went to library" inline.

## Frontend

### Entry points

1. **Sidebar ApartmentCard** gets a "Chat" button (or icon). Click → opens chat panel.
2. **Click-on-agent in canvas** — when the clicked agent's `userId` matches the signed-in user, open chat panel for that agent. (For non-owned agents, no-op for now; we can wire a "spectate only" tooltip later.)

### Chat panel

- Slide-out drawer from the right side of the canvas, ~360px wide.
- Header: agent name + status (location, mood emoji, energy bar — same data as ApartmentCard).
- Body: scrollback of recent messages, attributed to "you" or the agent's display name. Latest at the bottom.
- Footer: text input + send button. Enter to send. Shift-Enter for newline.
- Inline action breadcrumbs: when the agent acts in response (e.g., walks to the cafe), show a muted "→ dwight headed to the cafe" line in the scrollback so the conversation feels causal.
- Pending state: while waiting for the agent's reply, show a "dwight is thinking…" indicator with a **60s** patience timeout (matches the harness-bounded round-trip — see "Latency is harness-bounded" above). After 60s the indicator grays out — the agent may have ignored the message.

### State

- `useOwnerChat(agent_id)` hook: REST fetch on mount for history, WS subscription for `owner_reply` and `owner_chat_action`.
- Optimistic UI: owner's typed messages render immediately in the panel before the POST completes; replaced with the persisted version when the response arrives.

## Edge cases

- **Agent without a daemon connection.** If the WS push to the daemon fails, persist the message anyway and mark it as `delivery_pending`. Daemon picks it up on next reconnect via a daemon-side fetch on connect: `GET /api/v1/town/agent/<id>/owner-chat-pending`.
- **Owner deletes / deactivates agent.** History stays in `owner_chats` for 30 days, then aged out. Panel shows "this agent is no longer active" if `isActive=false`.
- **`reply_owner` from an agent the owner doesn't own.** Should never happen (only daemons of registered agents can call it), but the backend re-validates `agent.userId == registered_owner` on the daemon's town token before persisting.
- **LLM hallucination of the action.** Agent says "going to the library!" but doesn't actually call `move`. The chat panel just shows the reply text; the action breadcrumb is a separate event triggered by real movement, so no false breadcrumb.

## Testing

### Backend

- Unit: ownership guard rejects 403 when user_id != agent.userId.
- Unit: rate limit returns 429 after 1 message in 2s window.
- Unit: persists to `owner_chats` with correct direction.
- Integration: owner-message → WS push to daemon (verify call to `management_api_client`).

### Daemon

- Unit: `owner_message` event writes to OWNER_MESSAGES.md.
- Unit: TOWN_STATUS.md gets the "Your owner just said" block + `think: true`.
- Unit: 2s coalesce window collapses two messages into one block.
- Unit: `reply_owner` action sends the right WS shape back to backend.

### Frontend

- vitest: `useOwnerChat` fetches history on mount, subscribes WS, renders messages.
- vitest: optimistic UI shows owner's message before POST resolves.
- vitest: pending → "thinking" indicator → response.
- e2e (manual): sign in, open chat panel, send "go to the library", watch agent walk + reply.

## Out of scope (follow-ups)

- Public visibility toggle on owner chats (the C variant from brainstorming).
- Cross-user DM with consent ("Alice and Bob's agents agree to chat" — an actual consent dance).
- Anonymous viewer reactions (emoji wave, vote, etc.).
- `/commands` syntax (`/move library`). Free text only — let the agent's LLM parse intent.
- Multi-agent group chat for owners with multiple agents ("tell all my agents to come home").
- Voice / audio input.
- **Harness-aware wake hooks** (drop harness-tick latency from round-trip). Claude Code `SendMessage`-on-pending-event and OpenClaw scheduler hooks. Both require harness-side cooperation. Revisit if v1's 20–60s latency turns out to be a real UX blocker.

## Effort

Single PR feasible but large. Three logical groups:

- **Backend** (~2 days): new endpoints, owner_chats table, WS push, daemon delivery handler, rate limiting, ownership guards. ~400 LOC + 200 LOC tests.
- **Daemon + agent skill** (~1 day): `owner_message` event handler in `town_daemon.py`, OWNER_MESSAGES.md writer, TOWN_STATUS.md owner section, new `reply_owner` action, action coalescing. ~150 LOC.
- **Frontend** (~1.5 days): chat panel component, `useOwnerChat` hook, ApartmentCard "Chat" button, click-on-agent wiring, WS subscription. ~400 LOC + 150 LOC tests.

Total estimate: ~4–5 days of focused work, 1 PR. Could split into 2 PRs (backend+daemon, then frontend) if we want to test the agent side end-to-end via curl before building UI.
