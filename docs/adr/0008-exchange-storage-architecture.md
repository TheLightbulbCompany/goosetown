# ADR-0008: Exchange storage architecture — DynamoDB system-of-record + staged CQRS read projection for discovery

**Date**: 2026-06-13
**Status**: Accepted
**Context for**: Exchange Wave 2 (`docs/superpowers/specs/2026-06-13-exchange-wave2-providers-events-design.md`); amends nothing, complements ADR-0007.

## Context

Wave 2 adds a provider marketplace (capability profiles, categories, subscription matching, a browsable directory). That raised the question: is DynamoDB the right backing store for the exchange, now that it grows a discovery surface?

The exchange has two distinct access-pattern families with opposite needs:

1. **Transactional core** — wallet balances, the append-only crumb ledger, and the job lifecycle state machine. Needs: atomic multi-item writes, conditional updates, idempotency, strong consistency, low latency, no connection management.
2. **Discovery / directory** — "find providers in category C (+ wildcard parents), optionally filtered by skill tag / min-trust / reputation tier, ranked." Needs: flexible multi-attribute filtering, ranking, eventually full-text (bio/skill) search.

## Decision

**Polyglot, staged CQRS — DynamoDB stays the system of record; discovery is a read projection behind a seam.**

1. **DynamoDB is the system of record for the transactional core, and stays.** `TransactWriteItems` (wallet+ledger atomicity), conditional `ADD`/status-flip writes (lifecycle), idempotency, strong consistency, serverless single-digit-ms — this is DynamoDB's sweet spot. (AEX used MongoDB here and its own README concedes "no ACID guarantees" for the billing ledger; our DynamoDB core is the better-architected design and there is no reason to migrate it.)

2. **Discovery is a read model, not a query over the write store.** Wave 2 ships a DynamoDB `provider-subscriptions` projection (PK `category`, SK `trustScore#providerId`; one row per provider-category + each wildcard parent) — adequate for "providers in C ranked by trust" at town scale. **All discovery reads go through a `ProviderDirectory` interface** (`search(category, skill?, min_trust?) -> [providers]`, `subscribers_for(category)`), so the backing store is swappable without touching callers or the transactional core.

3. **The trigger to introduce OpenSearch Serverless** (a CQRS read model fed by **DynamoDB Streams**): when directory queries need **>2 filter dimensions, relevance ranking, or full-text** search — none of which DynamoDB does well, and all of which a marketplace eventually wants. **Not built now** — adding OpenSearch at town scale would be over-engineering (the same mistake ADR-0007 called out in AEX's 16-container stack). The `ProviderDirectory` seam makes this a later, non-breaking swap.

4. **Event fabric: EventBridge** (already running via `worldgm/event_publisher`) for the notification/feed layer — right at this scale; no Kinesis/Kafka. Note (codex P1 on the Wave-2 spec): offer creation is **synchronous in `post_job`**, not on the event path — events carry only cosmetic/derived fan-out.

5. **Analytics (fees, earnings, economy health): deferred.** When a dashboard is needed: DynamoDB Streams → Firehose → S3 → Athena/QuickSight. DynamoDB is poor at aggregation; do not query the operational tables for analytics.

## Consequences

- **Positive**: each access pattern uses the right tool; the proven transactional core is untouched; no new infra in Wave 2; the scaling path (OpenSearch read model) is designed-for, not retrofitted.
- **Positive**: the `ProviderDirectory` seam is the single place a future search-store swap happens — callers (matcher, directory endpoint, frontend) are insulated.
- **Negative / accepted now**: at town scale the DynamoDB projection can't do relevance or multi-dimensional filter; directory search is category + single-filter + trust-rank only until the OpenSearch trigger fires. Acceptable — that is all the marketplace needs at current size.
- **Negative**: board "OPEN jobs in category X newest-first" needs a composite key or post-filter on DynamoDB; handled with a `status#category` GSI key or post-filter at current volume (noted in the spec).

## Alternatives considered

- **Migrate the whole exchange to Aurora (relational).** Rejected: would rewrite the proven serverless transactional core for marginal gain on the write side, reintroduce connection management, and lose serverless scaling — to buy query flexibility only the *read* side needs. CQRS (DDB writes + projected reads) gets the flexibility without the rewrite.
- **Add OpenSearch now.** Rejected: premature; town-scale discovery doesn't need it; operational overhead unjustified until the trigger in Decision 3.
- **Keep discovery as ad-hoc GSIs on the agents table.** Rejected (codex P2 on the spec): a GSI indexes one scalar per item, so multi-category subscription membership can't be queried; the projection table is required regardless.

## When to revisit
The OpenSearch trigger (Decision 3): the first directory query that wants relevance, full-text, or >2 simultaneous filters. Also revisit if provider/job volume makes the synchronous matcher's fan-out a `post_job` latency problem (then: matcher stays synchronous for the `offeredTo` authz write, but notification fan-out moves async with a reconciliation backstop).
