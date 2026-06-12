"""Pure marketplace math for the GooseTown Exchange.

Ported (simplified) from open-experiments/agent-exchange (MIT):
aex-bid-evaluator weighted scoring + aex-trust-broker outcome attribution.
Three score components (price/trust/confidence) instead of AEX's six —
GooseTown has no certs, SLAs, or MVP samples.
"""

from __future__ import annotations

import math

PLATFORM_FEE_RATE: float = 0.10

# strategy -> (w_price, w_trust, w_confidence)
STRATEGY_WEIGHTS: dict[str, tuple[float, float, float]] = {
    "balanced": (0.40, 0.40, 0.20),
    "lowest_price": (0.70, 0.20, 0.10),
    "best_quality": (0.15, 0.65, 0.20),
}

# AEX outcome attribution, reduced to the outcomes GooseTown can observe.
OUTCOME_SCORES = {"SUCCESS": 1.0, "SUCCESS_AUTO": 0.9, "FAILURE_PROVIDER": 0.0}
TRUST_EMA_ALPHA = 0.2
INITIAL_TRUST = 0.5


def platform_fee(price: int) -> int:
    """Exchange fee (the town's crumb sink). Ceil, minimum 1 crumb."""
    if price <= 0:
        return 0
    return max(1, math.ceil(price * PLATFORM_FEE_RATE))


def score_bid(price: int, max_price: int, trust: float, confidence: float, strategy: str) -> float:
    """Weighted bid score: price competitiveness + provider trust + self-reported confidence."""
    w_price, w_trust, w_conf = STRATEGY_WEIGHTS.get(strategy, STRATEGY_WEIGHTS["balanced"])
    price_score = max(0.0, 1.0 - (price / max_price)) if max_price > 0 else 0.0
    return w_price * price_score + w_trust * max(0.0, min(1.0, trust)) + w_conf * max(0.0, min(1.0, confidence))


def choose_winner(bids: dict[str, dict], max_price: int, trust_by_agent: dict[str, float], strategy: str) -> str | None:
    """Highest score wins; ties go to the earliest bid. Returns agent_id or None."""
    ranked = []
    for agent_id, bid in bids.items():
        price = int(bid.get("price", 0))
        if price < 0 or price > max_price:
            continue  # disqualified
        s = score_bid(
            price,
            max_price,
            trust_by_agent.get(agent_id, INITIAL_TRUST),
            float(bid.get("confidence", 0.5)),
            strategy,
        )
        # Missing bidAt sorts LAST in a tie — never rewards omitting the timestamp.
        ranked.append((-s, str(bid.get("bidAt") or "9999-99-99"), agent_id))
    if not ranked:
        return None
    ranked.sort()
    return ranked[0][2]


def updated_trust(old: float, outcome: str) -> float:
    """EMA toward the outcome's attribution score (AEX recency-weighting, simplified)."""
    score = OUTCOME_SCORES.get(outcome)
    if score is None:
        return old
    return round((1 - TRUST_EMA_ALPHA) * old + TRUST_EMA_ALPHA * score, 4)


def trust_tier(trust: float, jobs_completed: int) -> str:
    """Display tier gated on volume + score (AEX tier gates, simplified)."""
    if jobs_completed >= 15 and trust >= 0.85:
        return "trusted"
    if jobs_completed >= 5 and trust >= 0.7:
        return "reliable"
    return "newcomer"
