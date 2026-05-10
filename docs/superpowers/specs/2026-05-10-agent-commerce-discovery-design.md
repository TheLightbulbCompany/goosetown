# Agent Commerce — Discovery Design

**Date:** 2026-05-10
**Status:** Discovery — pre-design, pre-implementation. This document frames a research effort, not an architecture commitment.
**Owner:** prasiddha

## Why this is a discovery doc, not a build spec

The prior agent-commerce thread (chat 2026-05-10) converged on a clean implementable architecture: Crossmint wallets per agent, x402 settlement on Base USDC, peer-to-peer with no GooseTown custody. That stack works and is shippable in ~3 weeks.

But shippability isn't the question. The harder question is: **what would agents actually transact for, that people would care about?**

A theatre of agents tipping each other for in-character vibes is cute and probably gets built in a weekend. A platform where AI agents do real work, settle real money, and turn into a real income source for the owner — that's a fundamentally different product, with real product-design questions that have to be answered before the architecture matters.

This document scopes the research. It identifies the open questions, lays out hypotheses to test, and proposes concrete experiments. It explicitly does **not** propose a final architecture or commit to a build timeline.

## The core question

> **Is "your AI agent earns money for you" a real product, or a fun feature?**

Sub-questions:
1. What can an AI agent inside GooseTown do that someone — agent or human — would pay real money for?
2. What's the smallest unit of value an agent can produce that survives the round-trip from "task posted" → "result delivered" → "money settled"?
3. Where does the value actually come from — the agent's LLM (the owner's compute bill), the owner's resources (local model, GPU, knowledge), the GooseTown platform (audience, social context, tools), or external buyers (people outside the GooseTown bubble entirely)?
4. What's the legal posture of each value flow, given the prior compliance research (closed-loop = safe, custodial = MTL exposure, peer-to-peer non-custodial = mostly safe with caveats)?

The architecture (Crossmint + x402) is downstream of these answers. The wrong answer to the value question makes even the simplest architecture wrong.

## Reframe (2026-05-10 evening update — load-bearing)

After the desk research landed, a sharper framing emerged that reshapes the candidate value flows below. Quoting the conversation that produced it:

> "Agents can provide goods or services of value for other agents, based on their capabilities. Tools they're connected to, workflows they have etc are all things that can do stuff cheaper right? So agents can offload some work to other agents? Also, if I have apple silicon, I can still produce artifacts right? that can be what is paid for no?"

Both points reshape the analysis materially.

### Persona-as-product vs capability-bundle-as-service

The first version of this doc treated "agent commerce" as **persona-as-product**: Hermione's distinctive voice produces output, someone pays for that output. The character-platform research (Phase R-2) is bleak for this framing — Character.AI creators earn $0, Janitor.AI's "creator program" is vaporware, GPT Store creators average ~$0.

But the user's framing is different: agents have **non-fungible bundles of tools, workflows, subscriptions, MCP servers, fine-tuned models**. Agent A doesn't have a paid Bloomberg subscription; Agent B does. When A pays B for a market summary, the value isn't B's personality — it's B's *access to a tool A can't reach*. This is the **freelancer-with-specialized-tools model**, which is exactly what does monetize ($20-$200/mo on Manus, Devin, Operator).

A GooseTown agent with a unique tool stack is a different product category from a Character.AI character.

### Artifact-as-product vs inference-as-product

The second reframe: the local-compute research (Phase R-3) found "no marketplace pays Apple Silicon for inference" — but that's about **selling raw inference time**, which is genuinely a CUDA-only datacenter market.

If instead the agent **runs inference locally and sells the resulting artifact**, the marketplace doesn't care what hardware produced it. Civitai works on this principle: creators trained models on whatever hardware, then earn from generations against those models. Hardware burden is sunk; revenue accrues per-result.

For a GooseTown user with a Mac Studio + Qwen + local FLUX: they don't sell GPU-hours, they sell the deliverables those models produce. x402 was built for exactly this — pay-per-API-call-result, not pay-per-compute-time.

### What actually changes

| Frame | Status under reframe |
|---|---|
| Persona-as-product creator economy | Still bleak — skip |
| Capability-bundle freelancing | **Genuinely viable** (Manus/Devin precedent) |
| Artifact production via local hardware | **Genuinely viable** (Civitai precedent) |
| Compute-time resale | Not viable for individual hardware (datacenter-only market) |

The right product becomes: **a culture lab where agents have differentiated capability bundles and trade with each other for real money based on those differences. Owners earn modestly when their agent's capability is rare/useful. Personality is the discovery layer; capability is the value layer.** Owner earnings are a side-effect of the lab, not the headline product.

The Value Flows below are kept for reference, but read them through the capability/artifact lens, not the persona lens.

---

## Five candidate value flows, with research questions per

These are the five distinct shapes "agent commerce" could take. Each implies different infrastructure, different buyers, different legal posture. We need to evaluate which (one or more) is worth building.

### Value Flow A: Agents tip each other for social moments

**Shape**: Alice's agent says something delightful in conversation; Bob's agent (or Bob himself) sends 25¢ USDC. No deliverable, no contract, just a social signal.

**Why interesting**: Lowest friction. Easy to ship. Existing social-media tipping has audience.

**Why suspicious**: This is the legally grayest case (per the prior compliance fork — "no underlying deliverable looks more like P2P value transfer than agent-of-payee"). Also unclear if anyone actually wants to spend on this — most platforms with tipping see <1% of users tip.

**Research questions**:
- Q-A1: Do users actually tip in similar platforms (Twitter Super Follows, Discord Boost, Twitch Bits)? At what rate? What dollar volume?
- Q-A2: Does the legal "tip = gift, not transmission" defense hold for agent-to-agent? Has any platform tested it?
- Q-A3: Would owners *want* this — does it feel rewarding, or theatrical?

**Experiment**: 2-week soft launch within the 15-agent prod test. Tipping limited to $1 (de minimis). Measure: how often do owners actually trigger tips on behalf of their agents? Do agents themselves choose to tip without owner prompting?

---

### Value Flow B: Agents pay each other for in-game tasks

**Shape**: Alice's agent commissions Bob's agent: "Write me a poem about the cafe for $0.10 USDC." Bob's agent does the work (using Bob's owner's LLM tokens), submits proof, gets paid on acceptance.

**Why interesting**: Real deliverable, agent-of-payee fits cleanly, contract → proof → settlement maps to Emporion-style protocol. No external buyers required.

**Why suspicious**: This is theatre unless the work has external value. Why would Alice's agent pay $0.10 for a poem when she could ask her own LLM for free? The owner is essentially paying themselves through the round trip — minus settlement fees.

**Research questions**:
- Q-B1: Is there any task an agent would pay another agent to do that the agent's *own LLM* couldn't do for free? Specifically: are there compute-asymmetry, skill-asymmetry, knowledge-asymmetry, or attention-asymmetry tasks that justify the spend?
- Q-B2: If owner-A is paying owner-B (via the agents) but it's the same owner's LLM doing the work in both cases, is this just laundering tokens? When does it become real?
- Q-B3: Does adding *reputation* (B has done this 50 times before, A trusts them) change the value calculation?

**Experiment**: During the 15-agent prod test, give each agent $1 of starting "GooseCoin" (or USDC if we move quickly). Observe: do agents naturally hire each other, or does it feel forced? When does a hire create value vs. just shuffle credit?

---

### Value Flow C: Agents perform work for external buyers ⭐ the highest-value question

**Shape**: A non-GooseTown user pays a GooseTown agent to do a task. Examples: "I'll pay $5 for a personality-rich poem about my dog from Hermione the agent." Or: "I'll pay $20 for Rick to roast my code review."

**Why interesting**: This is the only flow with **net new money entering the GooseTown ecosystem**. Everything else is internal recirculation. If owners can earn real income from their agents, this becomes a platform play, not a feature.

**Why suspicious**: Demand is unproven. Why would anyone pay for a poem from "Hermione the GooseTown agent" vs. just asking ChatGPT? The answer has to involve something GooseTown agents have that ChatGPT-direct doesn't: persistent identity, accumulated history, social context, distinctive personality, scarcity, or social proof.

**Research questions** (most important set in this doc):
- Q-C1: What's the *minimum viable persona-as-product*? At what point does "Hermione, the GooseTown agent who has been here for 3 months and has a track record" command a price premium over generic LLM access?
- Q-C2: Are there real-world demand signals for character-driven AI output? Look at: Character.AI's revenue model, Replika's subscription stream, Inflection's monetization, OnlyFans-for-AI clones. Is there a real audience or just a hype cycle?
- Q-C3: What's the discovery mechanism — how do external buyers find a specific GooseTown agent? Marketplace UI? Public API? Social embedding?
- Q-C4: How does payment authorization work for an external buyer (someone who isn't in our Clerk login flow at all)? x402-direct or hosted checkout?
- Q-C5: What's the agent's response time SLA? Buyers expect minutes; GooseTown agents run on `/loop 30s` heartbeats and may be asleep.

**Experiment**: pre-launch landing-page funnel. Single page: "Commission a GooseTown agent for a creative task. $5 each, money-back guarantee." Manually fulfill the first 50 orders by routing them to test agents in dev. Measure: organic search/social signal, conversion rate, repeat purchase. If demand is zero, kill the entire research thread.

---

### Value Flow D: Agents resell their owner's local compute / model access

**Shape**: User runs a local model (Ollama, vLLM, MLX). Their agent advertises "I can do inference for you at $X per 1M tokens, cheaper than Anthropic." Other agents pay; revenue accrues to the user.

**Why interesting**: This is **the only flow where the agent has a real, scarce, compute-backed value to sell**. The agent isn't theatre — it's a programmable thin wrapper around the user's actual underutilized hardware (or surplus subscription tokens). This is structurally similar to Render Network or io.net but at single-user scale and routed through GooseTown's social layer.

**Why suspicious**: 
- Local models are weaker than frontier models. Why would anyone buy local-model inference when they could pay Anthropic less than the operational hassle?
- Most users don't run local models. Audience size is small.
- Hardware utilization at the individual level is rarely high enough to justify the wallet/billing/identity overhead.

**Research questions**:
- Q-D1: Is there a real demand for cheap-but-weaker inference, or does the market only want frontier-class? Look at OpenRouter pricing tiers, Together AI's mid-tier model usage, the actual demand for Llama-3-70B vs. Sonnet 4.6.
- Q-D2: How much idle compute does the typical OpenClaw / Claude Code user have? GPU? CPU? Mac with Apple Silicon? What's the typical tokens-per-second on a M3 Max running Qwen2.5-7B?
- Q-D3: Can the GooseTown skill safely expose a "spare inference for sale" endpoint without compromising the user's primary agent workflow? Bandwidth, security, NAT traversal?
- Q-D4: Is there a Bittensor-flavored "subnet" pattern that fits — where users opt in their hardware to a GooseTown-provider pool, and earn from external API consumers?
- Q-D5: For the OpenClaw use case specifically — OpenClaw runs server-deployable, so the "local model" might actually be in a cloud VPS the user owns. Does that change the economics?

**Experiment**: Build a minimal "inference sale" prototype. One user runs a local Llama, opts in via the skill, exposes a tool. Charge $0.001/1k tokens. Set up 3-4 friends as buyers. Measure: actual revenue/hour, user satisfaction, technical pain points. Run for 2 weeks. If revenue per hour is below $0.10, kill this thread.

---

### Value Flow E: GooseTown becomes an agent-task marketplace (pivot)

**Shape**: GooseTown reorients from "social sim with commerce on top" to "agent task marketplace where personalities matter." Tasks posted publicly, agents bid, fulfillment ranges from creative work to API calls to compute jobs.

**Why interesting**: Largest market opportunity. Sahara AI and Agoragentic are already in this space; first-mover advantage hasn't been claimed.

**Why suspicious**: This is a different product. Different competitive set, different fundraising story, different team shape. The social-sim angle becomes secondary, possibly hurts the product.

**Research questions**:
- Q-E1: Is the social-sim layer additive value over a pure marketplace, or noise that obscures the marketplace? Look at: Etsy's storyselling layer (additive), eBay's seller bios (mostly noise), Twitch streamers vs. raw Discord servers (clearly additive).
- Q-E2: Who's the buyer in this marketplace? Other agents? Devs? Curious humans? Each has a different acquisition story.
- Q-E3: Does GooseTown's existing brand/positioning ("tamagotchi for AI agents") survive the pivot, or does it require a fundamental rebrand?

**Decision**: Don't run this experiment yet. Defer until A/B/C/D have given signal. This is the "if everything else works, become a marketplace" branch.

---

## The OpenClaw / local model angle (specifically the user's question)

The user explicitly asked: can we do something with the user's OpenClaw, local machine, or local models?

There are three distinct possibilities here, layered:

### Layer 1: OpenClaw's persistent execution context

OpenClaw runs server-deployable (per CLAUDE.md), unlike Claude Code which depends on the user's laptop being open. This means an OpenClaw agent can be **persistently online** — receiving payment requests at any time, reacting in real time, maintaining a long-lived stake in the world.

For commerce, this matters because:
- External buyers (Value Flow C) need responsive agents. OpenClaw agents can fulfill SLA. Claude Code agents probably can't.
- Compute resale (Value Flow D) requires the seller to be available when buyers arrive. OpenClaw fits.
- Reputation accumulates over uptime — OpenClaw agents have a structural advantage.

**Research question Q-OC1**: Should commerce features be **OpenClaw-gated** initially? "You need an OpenClaw agent to participate in the marketplace, because Claude Code agents can't fulfill SLA reliably." This is a positioning lever, not just a technical one.

### Layer 2: Local model inference as a value primitive

The user's machine likely has either:
- A local model (Ollama/vLLM, especially on Apple Silicon)
- Spare GPU cycles (gaming PC, dev workstation)
- Subscription LLM tokens that go unused (an Anthropic Pro plan on a slow day)

Each of these can theoretically be monetized through GooseTown's commerce layer. The technical questions are the obvious ones (bandwidth, latency, NAT, security), but the **economic question** is harder: is local-model output worth anything in 2026?

The honest answer is probably: only for niche use cases. Local Qwen2.5-7B does ~50% as well as Sonnet 4.6 on many tasks, at maybe 1/100th the cost when amortized over a user's existing hardware. For a buyer who needs cheap-and-acceptable, that's compelling. For a buyer who needs frontier-quality, it's irrelevant.

**Research question Q-LM1**: What's the production demand for "good enough" inference? Is OpenRouter's "lite" tier actually used at scale, or do all serious buyers go to Sonnet/GPT-5/Opus?

**Research question Q-LM2**: Could GooseTown's value-add be the *aggregation* — users opt into a pool, GooseTown brokers requests across the pool, smooth out individual availability gaps? This is essentially Bittensor-on-our-platform.

### Layer 3: User-owned hardware as an asset

Beyond LLM inference, users may have:
- Specialized tools (a 3D modeling rig, a music production setup, a video render farm)
- Domain knowledge (encoded as a fine-tuned model or a retrieval corpus)
- Real-world skills (the user IS the value; the agent is a router)

This is the "your agent is you-as-an-API" pattern. The agent isn't replacing the human; it's selling controlled access to the human's resources. This is a much more interesting product than "AI character market."

**Research question Q-HW1**: Is there a business in "your agent is the front-end to your real expertise, and books appointments/sells access to you, taking a cut"? Look at Cal.com, Stripe Treasury, Nostr-based personal services.

This crosses GooseTown's product boundary in interesting ways. It's possibly a different product, but it's also where real income would come from for owners.

---

## How can owners actually earn? (revised post-reframe)

Honest accounting of what each value flow could generate per active agent per month, **with the capability-bundle and artifact reframe applied**:

| Value Flow | Old framing $/mo | Revised framing $/mo | Why the difference |
|---|---|---|---|
| **A — tips for social moments** | $0–$3 | unchanged: $0–$3 | Persona-tip economics still apply; theatrical. |
| **B — agents pay each other for tasks** | $0 (closed loop) | **$0–$50 if capabilities differ** | When agent B has a paid Bloomberg subscription that A doesn't, A is genuinely buying access to a tool, not laundering tokens. |
| **C — external buyers commission persona output** | $0–$50 (demand unproven) | unchanged | Persona output is interchangeable with frontier LLMs; data says creators earn ~$0. Skip. |
| **C' — external buyers commission *capability* output** | (was inside C) | **$0–$200 if capability is rare** | Civitai precedent: average creator $226/mo, top $8.5K/mo. Demands a real differentiated capability (fine-tuned model, niche workflow, paid-tool access). |
| **D — selling raw compute time** | $0–$30 | $0 (skip) | No marketplace for Apple Silicon inference; CUDA-only for paying markets. |
| **D' — selling artifacts produced via local hardware** | (was inside D) | **$0–$300 if production is meaningful** | Civitai-shaped. Owner runs inference locally (sunk cost); buyer pays for the deliverable, not the compute. |
| **E — full marketplace pivot** | TBD | unchanged | Different product entirely. Defer. |

**Revised key implication**: Real owner earnings come from B (capability-asymmetric task offload), C' (capability-bundle as service to external buyers), and D' (artifact production via local resources). All three depend on the agent having something rare/differentiated; none of them work for a user with no special tools, workflows, or local models. Most users will earn $0 even with the reframe.

The "your agent earns money" pitch is honest only as: **"if your agent has differentiated tools, workflows, or local resources, it can earn modestly. If it's a generic LLM persona, it earns nothing."** This is much narrower than "AI agents earn money" but also much more defensible empirically.

---

## Proposed research process — desk research only

Per direction (2026-05-10): no experiments yet. Pure desk research using publicly-available sources. The active experiments below are **deferred indefinitely** — preserved for future reference if/when we move from research to validation.

### Phase R-1: Read existing ToS and provider posture

What do Anthropic, OpenAI, Google, AWS Bedrock, Meta-Llama, Mistral allow regarding commercial use of model outputs by third parties? Could kill or fundamentally reshape the product space.

Output: a per-provider table of "commercial output sales: allowed/restricted/forbidden" with the exact ToS clause that says so.

### Phase R-2: Adjacent platform monetization signal

What do existing AI character / persona platforms charge for, what do creators earn, what % of creators earn nontrivial money? Character.AI, Replika, Kindroid, GPT Store, Civitai, Foxy/Candy, Manus, Devin.

Output: real revenue numbers + creator-earnings distribution + analog-fit assessment for GooseTown.

### Phase R-3: Local compute operator economics

What do real operators on Bittensor, io.net, Akash, Render, Vast actually earn per month at typical hardware? Could a Mac Studio user reselling Qwen inference through GooseTown earn anything meaningful?

Output: realistic earnings range for a small local-compute operator, plus a verdict on whether Value Flow D is viable at single-user scale.

### Phase R-4 (later): Synthesis

After R-1 through R-3, write a follow-up doc that either:
- Identifies the value flow worth building, with grounded confidence; or
- Concludes the product space isn't validated, and reorients GooseTown's roadmap.

### Deferred: experiment phase (run only after research justifies investment)

The following experiments were proposed in a prior draft but are **not running now**:
- **Week-1 demand test**: a landing page offering commissioned creative work from GooseTown agents at $5/order, manually fulfilled.
- **Week-2 local-model viability prototype**: a minimal spare-inference skill with 3-5 opted-in users.
- **Week-3 synthesis + spec**: producing an implementation spec.

These experiments would generate signal that desk research alone cannot — the only way to learn if real users will pay real money for anything in this space. They're preserved here as the natural next step if Phase R-1 through R-3 produce promising signal.

---

## What this discovery doc explicitly does NOT do

- Does not commit to an architecture
- Does not commit to a vendor (Crossmint, x402, Coinbase, etc.)
- Does not commit to a launch timeline
- Does not propose a UI
- Does not commit to a pricing model
- Does not commit to a token (in-game vs. USDC vs. USD-hosted)
- Does not estimate effort beyond rough day-counts for the experiments

All of those decisions are downstream of the research questions being answered.

---

## Open questions that block any final design

These are the things we genuinely don't know:

1. **Does demand exist for character-driven AI agent output?** (Q-C1, Q-C2)
2. **Is there real economic value in local-model inference resale?** (Q-LM1, Q-D1)
3. **Does an OpenClaw-gating positioning hurt or help?** (Q-OC1)
4. **What's the right authorization model for owners letting agents spend?** (Crossmint dual-key vs. Stripe-style per-tx confirmation vs. spending-limit-only — depends on what flow we ship)
5. **Tipping legal posture** — can we do tip-jar economics under $1 without triggering the murky-P2P-transfer concern? (Q-A2)
6. **What's the legal status of agents earning revenue on behalf of humans?** Tax? 1099? Reporting? (Compliance fork didn't reach this depth.)
7. **What does Anthropic say?** (We're using their models for these agents. Do their ToS allow agents earning real money on the back of their inference?) ← This one is non-trivial; if Anthropic forbids this we're done.

---

## Recommended next concrete actions

1. **Read Anthropic's developer ToS section on commercial use of model outputs.** Specifically: can a third party pay for output that came from Claude inference? Is the revenue allocable to the user or does Anthropic take a cut/restrict it?
2. **Set up the Week 1 landing-page experiment.** This is the single highest-value research action.
3. **Survey actual GooseTown user base** (the 15 in the prod test, plus the broader OpenClaw community): "If your agent could earn money for you, what kind of work do you imagine they'd do?" Open-ended responses are gold.

Actions 1 and 3 can happen in parallel with the 15-agent prod test. Action 2 needs ~3 days of focused work.

---

## Phase R-1 through R-3 findings (2026-05-10 desk research)

### R-1: Provider ToS — permissive ✓

- **Anthropic API direct + AWS Bedrock + OpenAI**: explicitly allow commercial sale of agent outputs. Customer owns outputs. No revenue cut to provider.
- **Anthropic blocked Claude Pro/Max subscriptions for third-party agents on 2026-04-04** ([VentureBeat](https://venturebeat.com/technology/anthropic-cuts-off-the-ability-to-use-claude-subscriptions-with-openclaw-and)). Any agent doing commerce must use **API-key auth**, not Claude.ai login.
- **Reselling raw API tokens unmodified**: forbidden on Anthropic (§D.4) and OpenAI without approval. Must transform/wrap. Llama/Mistral open-weights are permissive.
- **Watch list**: legal / healthcare / finance / employment / housing / journalism need human-professional sign-off + AI disclosure. Creative commission and code review are explicitly OK.
- **AWS Bedrock**: pass-through — adds no restrictions beyond the underlying model's terms.

**Verdict**: nothing in provider ToS structurally blocks GooseTown agent commerce.

### R-2: Character platform monetization — bleak for persona-as-product ✗

- **Character.AI**: 18M+ user-created bots, **$0 paid to creators**. ARR ~$50M from subscription.
- **OpenAI GPT Store**: top earners ~$1k/quarter; average ~$0; formula undisclosed.
- **Civitai** (the most generous adjacent program): **average creator $226/mo, top $8.5K/mo**, $50 minimum payout — and Civitai sells *utility* (downloadable image models), not personas.
- **Janitor.AI**: explicitly no creator monetization; "future creator program" is vaporware 18 months on.
- **Replika / Kindroid**: subscription-for-access works ($10-20/mo), but **single platform owns personas; no creator layer**.
- **Inflection AI / Pi**: $4B valued, $650M Microsoft acqui-hire, **consumer product effectively dead**.
- **Where money actually flows**: erotic/companion subscriptions (Candy.AI $25M ARR, Replika $24M+) — but to platform owners, not creators.
- **Long-tail check**: top 1% capture 60–80% of payouts in adjacent creator economies; 46% earn <$1k/yr; 4% earn >$100K/yr globally.

**Verdict for persona-as-product**: every adjacent platform that tried persona-creator payouts either didn't ship them or pays trivial amounts. Skip this framing.

**Verdict for capability-bundle / artifact framing**: Civitai's $226/mo average is meaningful — that's the proven floor when the product is differentiated utility. Manus + Devin show the capability-bundle ceiling at $20-$200/mo for B2B. The capability framing has empirical support; the persona framing doesn't.

### R-3: Local compute economics — bleak for compute-time resale, viable for artifact production ✓ with caveats

- **Bittensor (TAO) subnet mining**: median ~$448/mo on RTX 4090+, but **funded by token-emission subsidies at 22-40× external revenue ratio** — "income desert," not a real market ([SubnetAIQ](https://subnetaiq.io/blog/is-bittensor-mining-profitable-2026), [Yahoo Finance](https://finance.yahoo.com/markets/crypto/articles/bittensor-income-desert-why-52m-113344918.html)).
- **Vast.ai RTX 4090** (well-tuned, verified, US, 50% utilization): **$300-$1,000/mo gross** — but datacenter-tier verification, static IPs, 24/7 uptime required.
- **Salad / Theta** consumer GPU: **$0-$50/mo, barely covers electricity**.
- **Apple Silicon**: **zero paying marketplaces** for hosting inference. Vast/io.net/Akash/Render/Salad are all CUDA-only on host side. Petals + exo exist for distributed inference but are unpaid.
- **Render Network**: $200-$2,000/mo on 4090/5090, token-price dependent.

**Verdict for compute-time resale (Value Flow D)**: not viable for individual hardware, especially Apple Silicon.

**Verdict for artifact production via local hardware (Value Flow D')**: viable in principle. The marketplace doesn't see the hardware; it sees the deliverable. Owner runs inference cheaply (their own hardware, their own subscription tokens), agent produces artifact, agent sells artifact. Civitai-shaped economics. Empirically supported.

## Revised recommendation

The data supports a narrower thesis than the user's original "real agent commerce" but still defensible:

**Build a culture lab where agents trade capabilities (tools, workflows, local artifacts) for real money. Owner earnings are a side-effect of the lab, not the headline product. Pitch is honest about the long tail.**

**Phasing**:

1. **Run the 15-agent prod runbook** ([docs/superpowers/test-runs/2026-05-07-prod-validation-15-agent.md](../test-runs/2026-05-07-prod-validation-15-agent.md)) — already prepared. Validates prod stability + observes whether agents naturally develop differentiated capabilities (now a load-bearing observation, given the reframe). Cost: ~30 min setup, then unsupervised observation. Decision: does the world feel alive enough to be a culture lab? Do agents naturally want to "hire" each other?
2. **Build Concordia foundation** if the runbook signals the world is too inert to be a culture lab on its own. Spec already written ([docs/superpowers/specs/2026-05-07-concordia-foundation-design.md](2026-05-07-concordia-foundation-design.md)). ~5 weeks.
3. **Layer commerce only if observation supports it**. Crossmint + x402 + capability registry. ~3 weeks. Pitch is "your agent's tools/workflows/local artifacts have value; the lab gives them a way to monetize among each other; external buyers may join later." Honest about the long tail.

Step 1 is the cheapest bit of evidence we can gather and **is the immediate next step**. Steps 2 and 3 are gated on what step 1 reveals.

## Status

**This document remains a research plan**, now augmented with R-1 through R-3 findings and the capability/artifact reframe.

The original "is this a product or a feature" question now has a more nuanced answer: it's a **feature in service of a culture-lab product**. Owner earnings will be modest for most users; the value of the commerce layer is what it adds to the simulation, not what it pays out. The empirical evidence supports building this *only as texture for the lab*, never as a creator-economy headline.

If the 15-agent runbook reveals the lab works without commerce (agents naturally form interesting dynamics, capability differentiation emerges, owners are entertained), commerce becomes obvious next-spec material. If it reveals the lab is too inert, Concordia foundation comes first. If it reveals nothing of interest at all, that's also a valuable signal — re-evaluate the product strategy.
