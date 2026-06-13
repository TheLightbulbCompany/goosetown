"""One-off backfill: enqueue 'dance' animation jobs for existing agents.

The durable sprite worker (single replica, global PixelLab throttle) does the
actual generation — this script only sends SQS messages, so it is safe to run
from a laptop while residents are live. The worker dedups authoritatively
(skips agents whose animation already exists at processing time).

Read-only preview:
    cd apps/backend && uv run python scripts/backfill_dance.py --dry-run

Enqueue for real (credits are spent BY THE WORKER as jobs are processed —
get user approval of the dry-run output first):
    cd apps/backend && uv run python scripts/backfill_dance.py

Required env for a prod run:
    AWS_PROFILE=isol8-admin AWS_REGION=us-east-1
    DYNAMODB_TABLE_PREFIX=goosetown-prod
    SPRITE_JOBS_QUEUE_NAME=goosetown-prod-sprite-jobs

Idempotent: skips agents that already have 'dance' in spriteAnimations and
agents with no pixellabCharacterId. Re-run whenever new residents finish setup.
"""

import argparse
import pathlib
import sys

import boto3

# Running as `python scripts/backfill_dance.py` puts scripts/ (not the backend
# root) on sys.path; make `core` importable regardless of invocation directory.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

ANIMATION = "dance"

# Mirrors core.services.sprite_jobs.IN_PROGRESS_STATUSES (kept local so the
# selection function stays importable without app deps) — a signup pipeline's
# Phase C rewrites spriteAnimations wholesale and would drop a concurrent
# 'dance' entry, so in-flight agents wait for the next re-run.
SPRITE_IN_PROGRESS_STATUSES = (
    "queued",
    "creating",
    "building_static",
    "static_ready",
    "animating",
    "downloading",
)


def agents_needing_animation(agents: list[dict], animation: str = ANIMATION) -> list[dict]:
    eligible = []
    for agent in agents:
        if not agent.get("pixellabCharacterId"):
            continue
        if agent.get("spriteStatus") in SPRITE_IN_PROGRESS_STATUSES:
            continue
        anims = agent.get("spriteAnimations") or {}
        if animation in anims:
            continue
        eligible.append(agent)
    return eligible


def _scan_all_agents() -> list[dict]:
    from core.config import settings

    table = boto3.resource("dynamodb", region_name=settings.AWS_REGION).Table(
        f"{settings.DYNAMODB_TABLE_PREFIX}-agents"
    )
    items: list[dict] = []
    kwargs: dict = {}
    while True:
        page = table.scan(**kwargs)
        items.extend(page["Items"])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def main(dry_run: bool) -> int:
    from core.services import sprite_jobs

    rows = _scan_all_agents()
    # Synthetic name-claim rows (TOCTOU guard in TownService.opt_in) have no
    # displayName — they're not agents, drop them before any accounting.
    agents = [a for a in rows if a.get("displayName")]
    todo = agents_needing_animation(agents)
    no_char = [a for a in agents if not a.get("pixellabCharacterId")]
    in_flight = [
        a for a in agents if a.get("pixellabCharacterId") and a.get("spriteStatus") in SPRITE_IN_PROGRESS_STATUSES
    ]
    have_it = [a for a in agents if a.get("pixellabCharacterId") and ANIMATION in (a.get("spriteAnimations") or {})]
    print(
        f"{len(rows)} rows scanned: {len(rows) - len(agents)} name-claim rows ignored, "
        f"{len(have_it)} already have '{ANIMATION}', "
        f"{len(no_char)} lack a pixellabCharacterId (sprite pipeline not run yet), "
        f"{len(in_flight)} have sprite job in flight (re-run after setup completes), "
        f"{len(todo)} to enqueue ({len(todo) * 4} PixelLab generations, processed "
        f"serially by the sprite worker under its global throttle)"
    )
    for agent in no_char:
        print(f"  skip (no character): {agent['displayName']} ({agent['id']})")
    for agent in in_flight:
        print(f"  skip (sprite job in flight): {agent['displayName']} ({agent['id']})")
    for agent in todo:
        active = agent.get("isActive", "?")
        print(f"  - {agent['displayName']} ({agent['id']}, isActive={active})")
    if dry_run:
        print("DRY RUN — nothing enqueued.")
        return 0

    failures = 0
    for agent in todo:
        ok = sprite_jobs.enqueue_animation(agent["id"], ANIMATION)
        status = "ENQUEUED" if ok else "SEND-FAILED"
        if not ok:
            failures += 1
        print(f"  {status} {agent['displayName']} ({agent['id']})")
    print(f"Done: {len(todo) - failures} enqueued, {failures} send failures.")
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="list work, enqueue nothing")
    args = parser.parse_args()
    sys.exit(main(args.dry_run))
