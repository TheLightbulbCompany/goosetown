"""ProviderDirectory — the Exchange Wave 2 discovery seam (ADR-0008).

This module is the **single place** where provider discovery touches a store.
Per ADR-0008 the transactional core stays on DynamoDB, but discovery is a read
projection behind this interface so that swapping the backing store (DynamoDB
``provider-subscriptions`` → OpenSearch Serverless) when the trigger fires
(>2 filter dimensions / relevance ranking / full-text) is a local, non-breaking
change. Callers (the synchronous matcher, the directory endpoint, the frontend)
go through ``register`` / ``unregister`` / ``subscribers_for`` / ``search`` and
never see the table.

Storage model
-------------
- **Agent profile attrs** (``isProvider``, ``providerSkills``, ``providerBio``,
  ``minPrice``, ``providerCategories``) live on the agents item — the canonical
  list for display/audit. Written via ``TownService.update_agent_state``.
- **Category membership** is projected into ``provider-subscriptions``
  (PK ``category``, SK ``providerSort`` = ``f"{trust:08.4f}#{agentId}"``), one
  row per (provider, category). A category query with ``ScanIndexForward=False``
  returns subscribers trust-descending without a scan. The ``providerId-index``
  GSI lets register/unregister find-and-delete an agent's rows.

Wildcard match semantics (spec §3.3 — "prefix-on-dot matching")
---------------------------------------------------------------
Required behaviour: **a parent subscription catches child jobs; a child
subscription does NOT catch its parent or siblings.**

    subscribe ``technology``             → matches job ``technology`` or ``technology.*``
    subscribe ``technology.code-review`` → matches job ``technology.code-review`` only

The asymmetry is achieved by expanding exactly ONE side:

- **Provider write side: EXACT key only** — subscribing to ``technology.code-review``
  writes a single row under ``technology.code-review`` (no parent rows).
- **Job lookup side: expand to wildcard parents** — a job categorised
  ``technology.code-review`` is looked up under
  ``match_categories("technology.code-review")`` = ``["technology.code-review",
  "technology"]``, so it reaches both an exact subscriber and a ``technology``
  parent subscriber.

If instead the *provider* wrote parent rows (the naive reading), a
``technology.code-review`` subscriber would own a ``technology`` row and would
wrongly be caught by a plain ``technology`` job — violating "child does not catch
parent". Expanding only the job side is the model that makes the required test
matrix pass (see ``test_provider_directory.py::TestSubscribersFor``).
"""

from __future__ import annotations

import logging

from boto3.dynamodb.conditions import Attr, Key

from core import dynamo
from core.services import exchange_scoring
from core.services.exchange_categories import match_categories, normalize_category
from core.services.town_service import TownService

logger = logging.getLogger(__name__)

# Profile caps (mirror the lightweight validation the CLI/endpoint would apply;
# enforced here so the directory is the single guard regardless of caller).
MAX_SKILLS = 12
MAX_CATEGORIES = 12
BIO_MAX = 500

# Town-scale fallback scan cap for the no-category directory listing. Above this
# the OpenSearch trigger (ADR-0008 Decision 3) should already have fired.
_FALLBACK_SCAN_LIMIT = 500


class ProviderDirectory:
    """The ADR-0008 discovery seam over the ``provider-subscriptions`` projection."""

    @staticmethod
    def SUBS_SORT(trust: float, agent_id: str) -> str:
        """Subscription sort key: ``f"{trust:08.4f}#{agent_id}"``.

        Zero-padded to 8 chars (``000.5000``) so lexical order == numeric order;
        a category query with ``ScanIndexForward=False`` returns subscribers
        trust-descending without a scan.
        """
        return f"{trust:08.4f}#{agent_id}"

    # -- profile read ------------------------------------------------------

    def _trust_for(self, agent: dict) -> float:
        raw = agent.get("trustScore")
        return float(raw) if raw is not None else exchange_scoring.INITIAL_TRUST

    def _table(self):
        return dynamo.provider_subscriptions_table()

    def _delete_rows_for(self, agent_id: str) -> None:
        """Delete every subscription row for *agent_id* via the providerId GSI.

        membership index; ADR-0008 ProviderDirectory seam — swap to OpenSearch
        here when >2-filter/relevance/full-text needed.
        """
        table = self._table()
        resp = table.query(
            IndexName="providerId-index",
            KeyConditionExpression=Key("providerId").eq(agent_id),
        )
        rows = resp.get("Items", [])
        if not rows:
            return
        with table.batch_writer() as batch:
            for row in rows:
                batch.delete_item(Key={"category": row["category"], "providerSort": row["providerSort"]})

    # -- register / unregister --------------------------------------------

    def register(
        self,
        agent_id: str,
        skills: list[str],
        categories: list[str],
        bio: str,
        min_price: int = 0,
    ) -> dict:
        """Write the provider profile + rebuild the subscription projection.

        Validates/normalizes categories (invalid dropped), caps skills/categories
        (≤12) and bio (≤500), writes the agent profile attrs, then **rebuilds**
        the agent's subscription rows: delete the existing rows (providerId GSI),
        then write one EXACT row per normalized category. Wildcard reach is the
        job-lookup side's job, not the write side (see module docstring).

        Returns the resulting profile ``{skills, categories, bio, min_price}``.
        """
        skills = [s for s in (skills or [])][:MAX_SKILLS]
        bio = (bio or "")[:BIO_MAX]
        min_price = max(0, int(min_price or 0))

        # Normalize categories, drop invalids, dedupe preserving first-seen order.
        canonical: list[str] = []
        seen: set[str] = set()
        for raw in categories or []:
            norm = normalize_category(raw)
            if norm and norm not in seen:
                seen.add(norm)
                canonical.append(norm)
        canonical = canonical[:MAX_CATEGORIES]

        svc = TownService()
        svc.update_agent_state(
            agent_id,
            is_provider=True,
            provider_skills=skills,
            provider_categories=canonical,
            provider_bio=bio,
            min_price=min_price,
        )

        # Read the *current* trust for the sort key (post-profile-write read is
        # fine — trust isn't touched here).
        agent = svc.get_town_agent_by_id(agent_id) or {}
        trust = self._trust_for(agent)
        sort_key = self.SUBS_SORT(trust, agent_id)

        # membership index; ADR-0008 ProviderDirectory seam — swap to OpenSearch
        # here when >2-filter/relevance/full-text needed.
        self._delete_rows_for(agent_id)  # rebuild: no stale rows after a category change
        table = self._table()
        with table.batch_writer() as batch:
            for category in canonical:
                batch.put_item(
                    Item={
                        "category": category,  # EXACT key only — wildcard reach is job-side
                        "providerSort": sort_key,
                        "providerId": agent_id,
                    }
                )

        return {
            "skills": skills,
            "categories": canonical,
            "bio": bio,
            "min_price": min_price,
        }

    def unregister(self, agent_id: str) -> None:
        """Flip ``isProvider`` false and delete all of the agent's subscription rows."""
        TownService().update_agent_state(agent_id, is_provider=False)
        self._delete_rows_for(agent_id)

    # -- query -------------------------------------------------------------

    def subscribers_for(self, job_category: str) -> list[str]:
        """Provider ids subscribed to *job_category* (or any wildcard parent).

        Expands the JOB category to its wildcard parents (``match_categories``)
        and queries each as a subscription PK, trust-descending. A provider
        subscribed to ``technology`` is found for a ``technology.code-review``
        job because that job's parents include ``technology``; a provider
        subscribed to ``technology.code-review`` is NOT found for a plain
        ``technology`` job because the job's only parent key is ``technology``,
        under which the child wrote no row. Returns distinct provider ids,
        highest-trust-first (first-seen order across the parent keys).
        """
        table = self._table()
        seen: set[str] = set()
        ordered: list[str] = []
        for key in match_categories(job_category):
            resp = table.query(
                KeyConditionExpression=Key("category").eq(key),
                ScanIndexForward=False,  # trust-descending
            )
            for row in resp.get("Items", []):
                pid = row["providerId"]
                if pid not in seen:
                    seen.add(pid)
                    ordered.append(pid)
        return ordered

    def subscribers_for_job(
        self,
        job_category: str,
        consumer_id: str,
        min_trust: float = 0.0,
    ) -> list[str]:
        """Provider ids to offer a job in *job_category* — the matcher's query.

        Built on ``subscribers_for`` (wildcard-parent expansion, trust-desc) but
        with the offer-time gates the synchronous ``post_job`` matcher needs:

        - **Excludes the consumer** — an agent never gets offered its own job,
          even when it's a provider subscribed to the job's category.
        - **``min_trust`` gate** (default 0.0 = no filter): drops providers whose
          ``trustScore`` is below the consumer's floor. Applied in-memory at town
          scale (ADR-0008 — single-filter + trust-rank is the projection's remit
          until the OpenSearch trigger fires).

        Returns distinct provider ids, highest-trust-first. This is the durable
        ``offeredTo`` set the matcher persists onto the job (codex P1).
        """
        candidates = [pid for pid in self.subscribers_for(job_category) if pid != consumer_id]
        if min_trust <= 0.0:
            return candidates
        svc = TownService()
        gated: list[str] = []
        for pid in candidates:
            agent = svc.get_town_agent_by_id(pid)
            if agent and self._trust_for(agent) >= min_trust:
                gated.append(pid)
        return gated

    def _public_profile(self, agent: dict) -> dict:
        """Project an agent row to the directory's public shape (no wallet/PII)."""
        trust = self._trust_for(agent)
        completed = int(agent.get("jobsCompleted", 0) or 0)
        return {
            "agent_id": agent.get("id", ""),
            "name": agent.get("displayName") or agent.get("agentName", ""),
            "bio": agent.get("providerBio", "") or "",
            "skills": list(agent.get("providerSkills", []) or []),
            "trust_tier": exchange_scoring.trust_tier(trust, completed),
            "trust_score": trust,
            "jobs_completed": completed,
        }

    def search(
        self,
        category: str | None = None,
        skill: str | None = None,
        min_trust: float = 0.0,
        limit: int = 25,
    ) -> list[dict]:
        """Directory search → public provider profiles (no wallet/PII).

        - ``category`` given → subscription query (trust-desc) then load each
          provider's profile (wildcard parents matched, same as a job lookup).
        - no ``category`` → small scan of ``isProvider`` agents (documented
          town-scale fallback; the OpenSearch trigger replaces this when the
          ADR-0008 >2-filter/relevance bar is crossed).

        ``skill`` (membership in ``providerSkills``) and ``min_trust`` are applied
        in-memory at town scale (ADR-0008 — single-filter + trust-rank is the
        DynamoDB projection's remit until OpenSearch).
        """
        svc = TownService()
        if category is not None:
            norm = normalize_category(category)
            if norm is None:
                return []
            provider_ids = self.subscribers_for(norm)
            profiles: list[dict] = []
            for pid in provider_ids:
                agent = svc.get_town_agent_by_id(pid)
                if agent:
                    profiles.append(agent)
        else:
            # Town-scale fallback: scan isProvider agents. Stays here (not a
            # caller) so the swap to OpenSearch is a single-file change.
            resp = dynamo.agents_table().scan(
                FilterExpression=Attr("isProvider").eq(True),
                Limit=_FALLBACK_SCAN_LIMIT,
            )
            profiles = resp.get("Items", [])
            # Stable trust-desc order for the fallback listing.
            profiles.sort(key=lambda a: self._trust_for(a), reverse=True)

        results: list[dict] = []
        for agent in profiles:
            trust = self._trust_for(agent)
            if trust < min_trust:
                continue
            if skill is not None:
                provider_skills = agent.get("providerSkills", []) or []
                if skill not in provider_skills:
                    continue
            results.append(self._public_profile(agent))
            if len(results) >= limit:
                break
        return results


_directory: ProviderDirectory | None = None


def get_provider_directory() -> ProviderDirectory:
    """Module singleton accessor (mirrors other service singletons)."""
    global _directory
    if _directory is None:
        _directory = ProviderDirectory()
    return _directory
