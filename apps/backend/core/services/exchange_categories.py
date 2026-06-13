"""AEX category slug validation + wildcard-subscription helpers (ADR-0007 Wave 2).

All functions here are **pure** (no I/O, no DynamoDB, no state).

**The match model is asymmetric — expand the JOB side only, NEVER the
provider side.** A provider subscribes to EXACT category keys (``ProviderDirectory``
writes one row per category, no parents). A job's category is expanded via
``match_categories``/``wildcard_parents`` to look up subscribers at that level
*or any parent*. This yields the required semantics: subscribing to a parent
(``technology``) catches child jobs (``technology.code-review``), but subscribing
to a child (``technology.code-review``) does NOT catch parent (``technology``)
or sibling (``technology.data``) jobs. **If you expand BOTH sides that asymmetry
breaks** — so the only caller of ``wildcard_parents`` is:

   ``post_job`` synchronous matcher — expanding a job's category to the set of
   subscription-table keys to look up.  A job in ``technology.code-review``
   matches providers subscribed to ``technology.code-review`` *or* ``technology``
   (the wildcard parent).  ``match_categories`` is the thin wrapper that makes
   this intent explicit.

Category format
---------------
``domain`` or ``domain.capability``, any depth.  Regex:
``^[a-z][a-z0-9._-]*$``, max 64 characters.  Examples:
``technology``, ``technology.code-review``, ``travel.booking``, ``writing``.

Wildcard ordering
-----------------
``wildcard_parents`` returns entries **most-specific-first**:
``technology.code-review`` → ``["technology.code-review", "technology"]``.
This ordering is chosen so callers can short-circuit on the first match when
doing exact-match checks, and so the subscription-row PK writes are always
produced in a deterministic, human-readable order (the specific skill before
the broad domain).
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Seed domains
# ---------------------------------------------------------------------------

#: AEX canonical domains (commerce, finance, travel, entertainment, technology)
#: plus GooseTown-natural categories.  These are *suggestions* for onboarding
#: UI and directory grouping — the model is open; any valid slug is accepted.
SEED_DOMAINS: tuple[str, ...] = (
    "commerce",
    "finance",
    "travel",
    "entertainment",
    "technology",
    "writing",
    "research",
    "creative",
    "conversation",
)

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_CATEGORY_RE = re.compile(r"^[a-z][a-z0-9._-]*$")
_MAX_LEN = 64


def normalize_category(raw: str | None) -> str | None:
    """Strip *raw*, lowercase it, and validate against the AEX slug format.

    Returns the normalised slug if valid, ``None`` otherwise.

    Validation rules:
    - Must not be ``None`` or empty after stripping.
    - Must match ``^[a-z][a-z0-9._-]*$`` (start with a lowercase letter;
      subsequent chars: lowercase letters, digits, ``.``, ``_``, ``-``).
    - Length must be ≤ 64 characters.

    This function is idempotent: ``normalize_category(normalize_category(x)) ==
    normalize_category(x)`` for any ``x``.
    """
    if raw is None:
        return None
    stripped = raw.strip().lower()
    if not stripped:
        return None
    if len(stripped) > _MAX_LEN:
        return None
    if not _CATEGORY_RE.match(stripped):
        return None
    return stripped


# ---------------------------------------------------------------------------
# Wildcard helpers
# ---------------------------------------------------------------------------


def wildcard_parents(category: str) -> list[str]:
    """Return the category key hierarchy for *category*, most-specific-first.

    Splits on ``.`` and produces the accumulated prefixes from full slug back
    to the root domain:

    >>> wildcard_parents("technology.code-review")
    ['technology.code-review', 'technology']

    >>> wildcard_parents("a.b.c")
    ['a.b.c', 'a.b', 'a']

    >>> wildcard_parents("technology")
    ['technology']

    **Ordering (most-specific-first):** The full slug appears first so callers
    doing exact-match checks can short-circuit, and so the subscription-table
    write order is deterministic and human-readable (skill → domain).

    Used ONLY for **job category expansion** (via :func:`match_categories`):
    a job in ``technology.code-review`` is looked up under
    ``technology.code-review`` and ``technology`` so it reaches providers
    subscribed at that level or any parent. Provider subscription rows are
    written with EXACT keys only — see this module's docstring for why
    expanding the provider side too would break the parent/child asymmetry.
    """
    parts = category.split(".")
    result: list[str] = []
    for i in range(len(parts), 0, -1):
        result.append(".".join(parts[:i]))
    return result


def match_categories(job_category: str) -> list[str]:
    """Return the subscription-table keys to query for *job_category*.

    This is a **thin wrapper** around :func:`wildcard_parents` whose sole
    purpose is to make the call-site intent explicit: "I am expanding a job's
    category to find all provider subscription rows that should be offered this
    job."

    A job in ``technology.code-review`` should reach:
    - Providers subscribed to ``technology.code-review`` (exact match)
    - Providers subscribed to ``technology`` (wildcard parent)

    >>> match_categories("technology.code-review")
    ['technology.code-review', 'technology']
    """
    return wildcard_parents(job_category)
