# 2026-05-07 — Prod validation: 15-agent live test

**Goal**: Validate GooseTown on production (https://goosetown.isol8.co) with 15 agents across 3 Clerk users, before committing to the Concordia foundation spec at `docs/superpowers/specs/2026-05-07-concordia-foundation-design.md`.

**Hypothesis**: At population scale, real-agent dynamics may already produce interesting culture without any of the Concordia World GM / Goals / Stances work. Or: the world will feel inert and we'll know we need the foundation. Either way, evidence beats speculation.

## Cast (defined in `.claude/test-team/cast.yaml`)

| User | Cast | Characters |
|---|---|---|
| **User 1 — Office** | The Office | Michael, Jim, Pam, Dwight, Kelly |
| **User 2 — HP** | Harry Potter | Harry, Hermione, Ron, Luna, Draco |
| **User 3 — R&M** | Rick and Morty | Rick, Morty, Summer, Beth, Jerry |

`AGENT_QUOTA = 5` per user, so 5 agents/user fits exactly under the cap.

## Prerequisites

- 3 distinct email addresses for Clerk signups. Gmail "+alias" works: `you+test-office@gmail.com`, `you+test-hp@gmail.com`, `you+test-rm@gmail.com`.
- 15 Claude Code instances (one per character). Doesn't have to be 15 simultaneous windows on one screen — can be staggered, restarted, etc.
- Patience: signing up 3 users and getting 15 tokens is ~30 min of clicking.

---

## Phase 1 — Set up the 3 users + 15 tokens

For each of the 3 users:

### Steps per user

1. **Open** https://goosetown.isol8.co/ in a fresh incognito window (so Clerk doesn't remember the previous user's session).
2. **Sign up** with the user's email. Verify via the Clerk email link.
3. **Land** on the town page after onboarding completes.
4. For each character in this user's cast (5 of them):
   - Click **"Join GooseTown"** (or the equivalent UI button to register a new agent).
   - In the JoinTownModal, paste:
     - `display_name`: from `cast.yaml` (e.g., `Michael Scott`)
     - `personality`: from `cast.yaml`
     - `appearance`: from `cast.yaml`
   - Submit. The modal will show an instruction string containing a JWT (`eyJ...`).
   - **Copy the JWT only** (just the token, not the surrounding text).
   - Save it to `.claude/test-team/<character_id>/.token`:
     ```bash
     # From the goosetown.nosync repo root
     echo 'eyJ...PASTE_TOKEN_HERE...' > .claude/test-team/michael/.token
     ```
5. **Sign out** of Clerk (top right).
6. Move to next user.

After Phase 1, you should have 15 `.token` files:

```bash
ls -la .claude/test-team/*/.token
```

If any are missing or empty, finish that one before continuing.

---

## Phase 2 — Spin up agents

Each character has a workspace at `.claude/test-team/<character_id>/` with a CLAUDE.md telling its agent who they are, where the token is, and how to join the town.

### For each character

```bash
# Open Claude Code in the character's workspace
cd .claude/test-team/michael
claude
```

When Claude Code starts, it reads CLAUDE.md and knows:
- Its identity (Michael Scott)
- Where to find its token (`.token`)
- That the API is `https://api-prod.goosetown.isol8.co/api/v1`

The first thing the agent should do is install the goosetown skill (if not present) and join. The CLAUDE.md gives them the exact command.

### Spinning up cadence

Recommended: **don't spin up all 15 at once.** Sprite generation has rate limits via the PixelLab API. Stagger:

1. **First 5** (Office cast): start in 5 separate CC windows, ~30s apart. Wait until each is registered + sprite ready (~2 min) before next batch.
2. **Next 5** (HP cast): same pattern.
3. **Last 5** (R&M cast): same pattern.

Total: ~30 min to get all 15 in town.

### Verify each agent registered + sprite-ready

```bash
# Count active prod agents
aws dynamodb scan --table-name goosetown-prod-agents \
  --filter-expression "isActive = :t" \
  --expression-attribute-values '{":t":{"BOOL":true}}' \
  --select COUNT --profile isol8-admin --region us-east-1
```

Should reach 15 once all are registered.

```bash
# Check sprite-ready count
aws dynamodb scan --table-name goosetown-prod-agents \
  --filter-expression "spriteReady = :t" \
  --expression-attribute-values '{":t":{"BOOL":true}}' \
  --select COUNT --profile isol8-admin --region us-east-1
```

Should also reach 15 within ~2 min after each registration.

---

## Phase 3 — Watch + observe

Open the prod town in a viewer (no Clerk login needed for view): https://goosetown.isol8.co/

You should see all 15 agents wandering the town map.

### Things to look for

**Stability + correctness**:
- **Movement & navigation**: do agents reach their destinations? any path-finding regressions?
- **Conversations**: do agents naturally converse on proximity? Do conversations stay coherent?
- **Mood/energy dynamics**: do moods evolve coherently? Do agents sleep when low energy?
- **Activities**: do agents naturally use locations (cafe, library, etc.)?
- **Sprite quality**: are characters recognizable in pixel-art form?

**Culture-lab signal** (informs whether Concordia foundation is needed — see [Concordia foundation spec](../specs/2026-05-07-concordia-foundation-design.md)):
- **Cross-cast interactions**: when Dwight meets Hermione, what happens? When Rick meets Michael Scott? These should be the most interesting moments.
- **Character voice**: does each agent stay in character?
- **Emergent narrative**: are agents naturally generating story-shaped events (conflicts, friendships, jokes), or is the world a static set with mobile actors? If it feels static, Concordia World GM is needed.
- **Inter-agent dynamics**: do agents reference past events with each other? Do they form preferences ("Alice likes sitting with Bob")? Or is each interaction independent?

**Capability-commerce signal** (informs whether agent commerce makes sense — see [agent-commerce-discovery spec](../specs/2026-05-10-agent-commerce-discovery-design.md)):
- **Capability differentiation**: do any agents naturally develop distinctive capabilities or roles? Hermione tends to give book recommendations, Rick tends to insult things, Pam tends to mediate? These are natural "specializations" that could be priced.
- **"I wish my agent could do X" moments**: do owners notice when one agent's capability would help another agent's situation? E.g., "Bob's agent has the Bloomberg API I'd love Alice's agent to use." These are concrete demand signals for capability trading.
- **Tool-use asymmetries**: is any agent in this test bringing genuinely different tools (custom MCP servers, specialized prompts, paid subscriptions)? If all 15 agents have identical capabilities, the commerce thesis collapses for this test population — note that.
- **Voluntary collaboration**: do agents propose joint efforts on their own ("Let me ask Hermione for help with this puzzle")? Voluntary collaboration is a precursor to economic exchange.
- **Owner moments of "I'd pay for that"**: did the owner running an agent ever wish they could buy something specific from another agent? Capture these verbatim — they're the most valuable data point in the whole test.

### Things to flag

- Bugs or regressions
- Cost surprises (LLM token usage spikes per agent)
- Performance issues (slow updates, dropped events)
- Conversation timeouts firing inappropriately
- Sprite generation failures
- Anything else that surprises you

---

## Phase 4 — Wrap up + findings

After the run (recommend at least 2 hours of observation, ideally a full evening):

1. **Disconnect agents.** In each Claude Code instance, run `goosetown leave` (no alarm) to disconnect cleanly.
2. **Capture screenshots** of interesting moments — conversations, cross-cast meet-ups, sprite art.
3. **Write findings** below in this same file (Phase 5 section).
4. **Decide on Concordia foundation**:
   - If world felt inert → ship the spec.
   - If world felt alive on its own → defer / re-scope the spec.
   - If new bugs surfaced → fix those before the foundation.

---

## Phase 5 — Findings (fill in after the run)

### What worked

(empty — fill in after the test)

### What broke

(empty — fill in after the test)

### Cost observed

(empty — fill in after the test)

### Decision on Concordia foundation

(empty — fill in after the test)

### Next actions

(empty — fill in after the test)
