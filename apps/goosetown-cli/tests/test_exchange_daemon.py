"""Tests for the daemon side of the Exchange (Task 11, ADR-0007).

Covers:
- _on_exchange_outcome: resolves the pending future (ok + rejected),
  updates the wallet and jobs cache for the caller's own verbs.
- Job lifecycle event handlers driven through _handle_event with raw
  dicts: TOWN_EVENTS.md lines + jobs cache + TOWN_JOBS.md sections.
- _write_jobs formatting (v2, action-first): wallet line, NEEDS YOUR
  ACTION / WAITING / SETTLED buckets, all-caught-up placeholder, fenced
  deliverable/spec blocks, deadline rendering, inline commands.
- handle_tool_command: exchange verbs await the backend echo.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from goosetown.daemon import TownDaemon


@pytest.fixture
def daemon(tmp_path: Path) -> TownDaemon:
    state_dir = tmp_path / "state"
    workspace = tmp_path / "workspace"
    state_dir.mkdir()
    workspace.mkdir()
    return TownDaemon(
        ws_url="wss://example.invalid",
        token="t",
        agent_name="testagent",
        state_dir=state_dir,
        workspace_path=workspace,
    )


def _events(daemon: TownDaemon) -> str:
    return (daemon._workspace_path / "TOWN_EVENTS.md").read_text()


def _jobs_md(daemon: TownDaemon) -> str:
    return (daemon._workspace_path / "TOWN_JOBS.md").read_text()


# ---------------------------------------------------------------------------
# _on_exchange_outcome
# ---------------------------------------------------------------------------


class TestExchangeOutcome:
    async def test_ok_resolves_pending_future(self, daemon: TownDaemon):
        daemon._exchange_outcome = asyncio.get_running_loop().create_future()
        daemon._handle_event(
            {"event": "exchange_ok", "verb": "bid", "status": "ok", "job_id": "j1"}
        )
        assert daemon._exchange_outcome.done()
        outcome = daemon._exchange_outcome.result()
        assert outcome["status"] == "ok"
        assert outcome["verb"] == "bid"

    async def test_rejected_resolves_pending_future(self, daemon: TownDaemon):
        daemon._exchange_outcome = asyncio.get_running_loop().create_future()
        daemon._handle_event(
            {
                "event": "exchange_rejected",
                "verb": "post_job",
                "status": "rejected",
                "reason": "wrong_location",
            }
        )
        outcome = daemon._exchange_outcome.result()
        assert outcome["status"] == "rejected"
        assert outcome["reason"] == "wrong_location"

    def test_no_future_does_not_crash(self, daemon: TownDaemon):
        daemon._exchange_outcome = None
        daemon._handle_event(
            {"event": "exchange_ok", "verb": "cancel_job", "status": "ok"}
        )

    async def test_already_done_future_is_left_alone(self, daemon: TownDaemon):
        daemon._exchange_outcome = asyncio.get_running_loop().create_future()
        daemon._exchange_outcome.set_result({"status": "ok", "first": True})
        daemon._handle_event({"event": "exchange_ok", "verb": "bid", "status": "ok"})
        assert daemon._exchange_outcome.result()["first"] is True

    def test_post_job_ok_caches_own_job_and_wallet(self, daemon: TownDaemon):
        daemon._pending_exchange_payload = {
            "action": "post_job",
            "title": "Write a haiku",
            "description": "d",
            "max_price": 40,
        }
        daemon._handle_event(
            {
                "event": "exchange_ok",
                "verb": "post_job",
                "status": "ok",
                "job_id": "j1",
                "bid_window_sec": 120,
                "escrowed": 40,
                "balance": 60,
            }
        )
        ex = daemon.state["exchange"]
        assert ex["wallet"] == 60
        job = ex["jobs"]["j1"]
        assert job["role"] == "consumer"
        assert job["status"] == "OPEN"
        assert job["title"] == "Write a haiku"
        assert job["max_price"] == 40
        # bid_window_sec was present → absolute close time cached.
        assert job["bids_close_at"] is not None
        md = _jobs_md(daemon)
        assert "Wallet: 60 crumbs" in md
        assert '[OPEN] "Write a haiku" (job j1)' in md
        assert "up to 40 crumbs" in md
        assert "closes" in md and "UTC" in md

    def test_bid_ok_caches_provider_entry(self, daemon: TownDaemon):
        daemon._handle_event(
            {
                "event": "exchange_ok",
                "verb": "bid",
                "status": "ok",
                "job_id": "j2",
                "price": 25,
                "confidence": 0.8,
                "bid_count": 3,
            }
        )
        job = daemon.state["exchange"]["jobs"]["j2"]
        assert job["role"] == "provider"
        assert job["status"] == "BID"
        assert job["bid_price"] == 25
        assert "your bid: 25 crumbs" in _jobs_md(daemon)

    def test_cancel_ok_updates_status_and_wallet(self, daemon: TownDaemon):
        daemon._update_job("j1", role="consumer", status="OPEN", title="T")
        daemon._handle_event(
            {
                "event": "exchange_ok",
                "verb": "cancel_job",
                "status": "ok",
                "job_id": "j1",
                "refunded": 40,
                "balance": 100,
            }
        )
        assert daemon.state["exchange"]["jobs"]["j1"]["status"] == "CANCELLED"
        assert daemon.state["exchange"]["wallet"] == 100

    def test_deliver_ok_updates_provider_status(self, daemon: TownDaemon):
        daemon._update_job("j3", role="provider", status="AWARDED", title="T")
        daemon._handle_event(
            {
                "event": "exchange_ok",
                "verb": "deliver_job",
                "status": "ok",
                "job_id": "j3",
                "review_window_sec": 300,
            }
        )
        assert daemon.state["exchange"]["jobs"]["j3"]["status"] == "DELIVERED"

    def test_rejected_does_not_touch_cache(self, daemon: TownDaemon):
        daemon._handle_event(
            {
                "event": "exchange_rejected",
                "verb": "post_job",
                "status": "rejected",
                "reason": "insufficient_funds",
            }
        )
        assert "exchange" not in daemon.state or not daemon.state["exchange"]["jobs"]


# ---------------------------------------------------------------------------
# Job lifecycle events through _handle_event
# ---------------------------------------------------------------------------


class TestJobPostedBroadcast:
    def test_event_line_with_bid_hint_no_cache(self, daemon: TownDaemon):
        daemon._handle_event(
            {
                "event": "job_posted",
                "job_id": "j9",
                "title": "Name my goose",
                "description": "Suggest a name.",
                "max_price": 15,
                "strategy": "balanced",
                "consumer_name": "othergoose",
                "bid_window_sec": 120,
            }
        )
        content = _events(daemon)
        assert "New job at the Exchange" in content
        assert '"Name my goose"' in content
        assert "job j9" in content
        # Bid-instruction hint — the agent needs to know how to act on this broadcast
        assert "goosetown act bid j9 <price>" in content
        # Someone else's job — no cache entry
        assert "j9" not in daemon.state.get("exchange", {}).get("jobs", {})


class TestBidReceived:
    def test_event_line_and_cache(self, daemon: TownDaemon):
        daemon._handle_event(
            {"event": "bid_received", "job_id": "j1", "title": "T", "bid_count": 2}
        )
        assert "Sealed bid received" in _events(daemon)
        assert daemon.state["exchange"]["jobs"]["j1"]["bid_count"] == 2


class TestJobAwarded:
    def _award(self, daemon: TownDaemon, *, provider: bool) -> dict:
        data = {
            "event": "job_awarded",
            "job_id": "j1",
            "title": "Write a haiku",
            "agreed_price": 30,
            "deadline_sec": 600,
            "provider_name": "testagent" if provider else "rival",
            "consumer_name": "othergoose" if provider else "testagent",
        }
        if provider:
            data["description"] = "A haiku about dusk."
        daemon._handle_event(data)
        return daemon.state["exchange"]["jobs"]["j1"]

    def test_winner_gets_provider_role_and_deliver_hint(self, daemon: TownDaemon):
        job = self._award(daemon, provider=True)
        assert job["role"] == "provider"
        assert job["status"] == "AWARDED"
        assert job["agreed_price"] == 30
        assert job["description"] == "A haiku about dusk."
        content = _events(daemon)
        assert "You won the job" in content
        assert "goosetown act deliver j1 <your work>" in content
        md = _jobs_md(daemon)
        assert "## ⚡ NEEDS YOUR ACTION" in md
        assert "agreed 30 crumbs" in md
        # deadline_sec was in the award → absolute deadline rendered.
        assert "- DELIVER by " in md
        assert "→ goosetown act deliver j1 <your work>" in md

    def test_consumer_copy_gets_consumer_role(self, daemon: TownDaemon):
        job = self._award(daemon, provider=False)
        assert job["role"] == "consumer"
        assert job["status"] == "AWARDED"
        content = _events(daemon)
        assert "awarded to rival" in content

    def test_winner_sees_job_spec_in_town_jobs(self, daemon: TownDaemon):
        """The cached description renders as a 'Job spec:' fenced block while
        the job is AWARDED, and stops rendering once the job settles."""
        self._award(daemon, provider=True)
        md = _jobs_md(daemon)
        assert "  Job spec:" in md
        assert "  A haiku about dusk." in md

        daemon._handle_event(
            {
                "event": "job_completed",
                "job_id": "j1",
                "title": "Write a haiku",
                "payout": 27,
                "fee": 3,
            }
        )
        md = _jobs_md(daemon)
        assert "Job spec:" not in md
        assert "A haiku about dusk." not in md

    def test_job_spec_fences_neutralized(self, daemon: TownDaemon):
        """Embedded ``` in the spec is neutralized to ''' (same as deliverables)."""
        daemon._handle_event(
            {
                "event": "job_awarded",
                "job_id": "j9",
                "title": "Code job",
                "agreed_price": 10,
                "deadline_sec": 600,
                "provider_name": "testagent",
                "consumer_name": "othergoose",
                "description": "Write this:\n```python\nprint('hi')\n```\nThanks.",
            }
        )
        md = _jobs_md(daemon)
        assert "'''" in md
        assert "```python" not in md
        # Exactly one wrapping fence pair for the spec block.
        assert md.count("  ```") == 2

    def test_role_by_id_when_names_ambiguous(self, daemon: TownDaemon):
        daemon._agent_id = "uuid-me"
        daemon._handle_event(
            {
                "event": "job_awarded",
                "job_id": "j5",
                "title": "T",
                "agreed_price": 10,
                "deadline_sec": 600,
                "provider_id": "uuid-me",
                "provider_name": "someoneelse",
                "consumer_name": "anothergoose",
            }
        )
        assert daemon.state["exchange"]["jobs"]["j5"]["role"] == "provider"


class TestBidLost:
    def test_event_line_and_status(self, daemon: TownDaemon):
        daemon._update_job("j1", role="provider", status="BID", title="T")
        daemon._handle_event({"event": "bid_lost", "job_id": "j1", "title": "T"})
        assert "did not win" in _events(daemon)
        assert daemon.state["exchange"]["jobs"]["j1"]["status"] == "LOST"


class TestJobDelivered:
    def test_consumer_gets_deliverable_and_review_hint(self, daemon: TownDaemon):
        daemon._update_job(
            "j1", role="consumer", status="AWARDED", title="Write a haiku"
        )
        daemon._handle_event(
            {
                "event": "job_delivered",
                "job_id": "j1",
                "title": "Write a haiku",
                "deliverable": "dusk settles slow\nthe fountain hums in bronze light\ngeese drift homeward now",
            }
        )
        job = daemon.state["exchange"]["jobs"]["j1"]
        assert job["status"] == "DELIVERED"
        assert "fountain hums" in job["deliverable"]
        content = _events(daemon)
        assert "goosetown act review j1 accept|reject" in content
        md = _jobs_md(daemon)
        # Fenced block containing the full deliverable under the posted entry
        assert "  ```" in md
        assert "  the fountain hums in bronze light" in md

    def test_deliverable_not_kept_for_provider_jobs(self, daemon: TownDaemon):
        """Provider-side jobs never get a deliverable block in TOWN_JOBS.md."""
        daemon._update_job("j2", role="provider", status="DELIVERED", title="Other job")
        daemon._write_jobs()
        md = _jobs_md(daemon)
        assert "```" not in md


class TestJobCompleted:
    def test_provider_line_mentions_payout(self, daemon: TownDaemon):
        daemon._update_job("j1", role="provider", status="DELIVERED", title="T")
        daemon._handle_event(
            {
                "event": "job_completed",
                "job_id": "j1",
                "title": "T",
                "payout": 28,
                "fee": 2,
            }
        )
        job = daemon.state["exchange"]["jobs"]["j1"]
        assert job["status"] == "COMPLETED"
        assert job["payout"] == 28
        assert "you were paid 28 crumbs" in _events(daemon)

    def test_consumer_line(self, daemon: TownDaemon):
        daemon._update_job("j1", role="consumer", status="DELIVERED", title="T")
        daemon._handle_event(
            {
                "event": "job_completed",
                "job_id": "j1",
                "title": "T",
                "payout": 28,
                "fee": 2,
            }
        )
        assert "Your job" in _events(daemon)

    def test_role_freshness_no_cache_uses_event_ids(self, daemon: TownDaemon):
        """job_completed arriving with provider_id/consumer_id but no cached job
        (fresh daemon that missed earlier events) derives the correct role."""
        daemon._agent_id = "uuid-me"
        daemon._handle_event(
            {
                "event": "job_completed",
                "job_id": "j-fresh",
                "title": "Fresh job",
                "payout": 18,
                "fee": 2,
                "provider_id": "uuid-me",
                "consumer_id": "uuid-other",
                "balance": 118,
            }
        )
        job = daemon.state["exchange"]["jobs"]["j-fresh"]
        assert job["role"] == "provider"
        assert job["status"] == "COMPLETED"
        assert "you were paid 18 crumbs" in _events(daemon)
        md = _jobs_md(daemon)
        assert "## 📜 SETTLED (recent)" in md
        assert '[PAID +18] "Fresh job" (job j-fresh)' in md
        # Wallet updated from balance field.
        assert daemon.state["exchange"]["wallet"] == 118
        assert "Wallet: 118 crumbs" in md


class TestJobFailed:
    def test_event_line_and_status(self, daemon: TownDaemon):
        daemon._update_job("j1", role="provider", status="AWARDED", title="T")
        daemon._handle_event(
            {
                "event": "job_failed",
                "job_id": "j1",
                "title": "T",
                "reason": "delivery_timeout",
            }
        )
        assert daemon.state["exchange"]["jobs"]["j1"]["status"] == "FAILED"
        assert "failed: delivery_timeout" in _events(daemon)


class TestJobExpired:
    def test_event_line_and_status(self, daemon: TownDaemon):
        daemon._update_job("j1", role="consumer", status="OPEN", title="T")
        daemon._handle_event(
            {
                "event": "job_expired",
                "job_id": "j1",
                "title": "T",
                "refunded": 40,
            }
        )
        assert daemon.state["exchange"]["jobs"]["j1"]["status"] == "EXPIRED"
        content = _events(daemon)
        assert "expired with no winner" in content
        assert "40 crumbs were refunded" in content


class TestJobCancelled:
    def test_event_line_and_cache_cleared_from_working_on(self, daemon: TownDaemon):
        """job_cancelled sets status=CANCELLED on the cached BID entry and appends
        the event line — the entry must no longer appear under 'Jobs you're working on'
        as an active [BID] row once TOWN_JOBS.md is rewritten."""
        daemon._update_job("j1", role="provider", status="BID", title="Cancelled gig")
        daemon._handle_event(
            {
                "event": "job_cancelled",
                "job_id": "j1",
                "title": "Cancelled gig",
                "consumer_id": "uuid-poster",
            }
        )
        job = daemon.state["exchange"]["jobs"]["j1"]
        assert job["status"] == "CANCELLED"
        content = _events(daemon)
        assert "cancelled by its poster" in content
        assert "Cancelled gig" in content
        assert "your bid is void" in content
        # The cached entry is now CANCELLED — no longer shows as [BID].
        md = _jobs_md(daemon)
        assert "[BID]" not in md
        assert "[CANCELLED]" in md

    def test_no_prior_cache_still_creates_entry(self, daemon: TownDaemon):
        """A fresh daemon that missed the bid_ok event but receives job_cancelled
        must still record the terminal entry (not crash on missing cache)."""
        daemon._handle_event(
            {
                "event": "job_cancelled",
                "job_id": "j-new",
                "title": "Fresh job",
                "consumer_id": "uuid-poster",
            }
        )
        job = daemon.state["exchange"]["jobs"]["j-new"]
        assert job["status"] == "CANCELLED"
        assert "cancelled by its poster" in _events(daemon)


# ---------------------------------------------------------------------------
# _write_jobs formatting
# ---------------------------------------------------------------------------


class TestWriteJobs:
    def test_empty_cache_renders_all_caught_up_only(self, daemon: TownDaemon):
        """Empty cache: action header + all-caught-up line; the WAITING and
        SETTLED sections are omitted entirely (no noise for the reader)."""
        daemon._exchange_state()
        daemon._write_jobs()
        md = _jobs_md(daemon)
        assert "# GooseTown Exchange — your jobs" in md
        assert "## ⚡ NEEDS YOUR ACTION" in md
        assert "- (nothing — you're all caught up)" in md
        assert "## 📌 WAITING" not in md
        assert "## 📜 SETTLED" not in md
        assert "Wallet:" not in md  # unknown wallet → line omitted

    def test_wallet_line_when_known(self, daemon: TownDaemon):
        daemon._exchange_state()["wallet"] = 75
        daemon._write_jobs()
        md = _jobs_md(daemon)
        assert "Wallet: 75 crumbs" in md
        assert "Trust:" not in md  # tier never cached today → wallet-only

    def test_wallet_line_with_trust_tier_when_cached(self, daemon: TownDaemon):
        ex = daemon._exchange_state()
        ex["wallet"] = 94
        ex["trust_tier"] = "reliable"
        daemon._write_jobs()
        assert "Wallet: 94 crumbs · Trust: reliable" in _jobs_md(daemon)

    def test_newest_updated_first(self, daemon: TownDaemon):
        daemon._update_job("old", role="consumer", status="OPEN", title="Old job")
        daemon._update_job("new", role="consumer", status="OPEN", title="New job")
        daemon.state["exchange"]["jobs"]["old"]["updated"] = 1.0
        daemon.state["exchange"]["jobs"]["new"]["updated"] = 2.0
        daemon._write_jobs()
        md = _jobs_md(daemon)
        assert md.index("New job") < md.index("Old job")


# ---------------------------------------------------------------------------
# _write_jobs — v2 action-first layout
# ---------------------------------------------------------------------------


class TestWriteJobsV2Layout:
    def test_all_caught_up_when_only_waiting_jobs(self, daemon: TownDaemon):
        daemon._update_job(
            "j1", role="consumer", status="OPEN", title="T", max_price=10
        )
        daemon._write_jobs()
        md = _jobs_md(daemon)
        assert "- (nothing — you're all caught up)" in md
        assert "## 📌 WAITING" in md

    def test_deliver_and_review_both_under_needs_action(self, daemon: TownDaemon):
        daemon._update_job(
            "jd",
            role="provider",
            status="AWARDED",
            title="Deliver me",
            agreed_price=20,
        )
        daemon._update_job(
            "jr",
            role="consumer",
            status="DELIVERED",
            title="Review me",
            deliverable="the work",
        )
        daemon._write_jobs()
        md = _jobs_md(daemon)
        action_at = md.index("## ⚡ NEEDS YOUR ACTION")
        assert '- DELIVER: "Deliver me" (job jd) — agreed 20 crumbs' in md
        assert '- REVIEW: "Review me" (job jr) — delivered, awaiting your verdict' in md
        assert md.index("- DELIVER") > action_at
        assert md.index("- REVIEW") > action_at
        assert "## 📌 WAITING" not in md
        # Exact commands inline.
        assert "→ goosetown act deliver jd <your work>" in md
        assert "--file=path for big/binary work" in md
        assert (
            "→ goosetown act review jr accept|reject [reason] [--criteria=1,0,1]" in md
        )
        assert "you're all caught up" not in md

    def test_action_entries_ordered_by_deadline(self, daemon: TownDaemon):
        daemon._update_job(
            "late",
            role="provider",
            status="AWARDED",
            title="Late job",
            deadline_at=2_000_000.0,
        )
        daemon._update_job(
            "soon",
            role="provider",
            status="AWARDED",
            title="Soon job",
            deadline_at=1_000_000.0,
        )
        daemon._write_jobs()
        md = _jobs_md(daemon)
        assert md.index('"Soon job"') < md.index('"Late job"')

    def test_deadlineless_action_entry_sorts_last(self, daemon: TownDaemon):
        daemon._update_job(
            "nodl", role="consumer", status="DELIVERED", title="No deadline"
        )
        daemon._update_job(
            "dl",
            role="provider",
            status="AWARDED",
            title="Has deadline",
            deadline_at=1_000_000.0,
        )
        daemon._write_jobs()
        md = _jobs_md(daemon)
        assert md.index('"Has deadline"') < md.index('"No deadline"')

    def test_waiting_bucket_mapping_per_state(self, daemon: TownDaemon):
        daemon._update_job(
            "j-open",
            role="consumer",
            status="OPEN",
            title="Open job",
            max_price=40,
            bid_count=2,
        )
        daemon._update_job(
            "j-bid", role="provider", status="BID", title="Bid job", bid_price=12
        )
        daemon._update_job(
            "j-del", role="provider", status="DELIVERED", title="Delivered job"
        )
        daemon._update_job(
            "j-await",
            role="consumer",
            status="AWARDED",
            title="Awarded job",
            agreed_price=30,
        )
        daemon._write_jobs()
        md = _jobs_md(daemon)
        waiting = md[md.index("## 📌 WAITING") :]
        assert (
            '- [OPEN] "Open job" (job j-open) — up to 40 crumbs — 2 sealed bid(s)'
            in waiting
        )
        assert (
            '- [BID] "Bid job" (job j-bid) — your bid: 12 crumbs — sealed; '
            "you'll hear at the close" in waiting
        )
        assert (
            '- [DELIVERED] "Delivered job" (job j-del) — delivered; '
            "payment on accept (auto-accepts if they go quiet)" in waiting
        )
        assert (
            '- [AWARDED] "Awarded job" (job j-await) — agreed 30 crumbs — '
            "awaiting delivery" in waiting
        )
        # None of these are actionable → action section shows all-caught-up.
        assert "you're all caught up" in md

    def test_settled_trimmed_to_8_newest_first_cache_untouched(
        self, daemon: TownDaemon
    ):
        for i in range(10):
            daemon._update_job(
                f"j{i}",
                role="provider",
                status="COMPLETED",
                title=f"Settled {i}",
                payout=10 + i,
            )
            daemon.state["exchange"]["jobs"][f"j{i}"]["updated"] = float(i)
        daemon._write_jobs()
        md = _jobs_md(daemon)
        # Two oldest fall off the render; cache keeps all 10.
        assert "(job j0)" not in md
        assert "(job j1)" not in md
        assert "(job j2)" in md
        assert "(job j9)" in md
        assert md.index("(job j9)") < md.index("(job j2)")
        assert len(daemon.state["exchange"]["jobs"]) == 10

    def test_settled_line_markers(self, daemon: TownDaemon):
        daemon._update_job(
            "jp", role="provider", status="COMPLETED", title="Paid job", payout=18
        )
        daemon._update_job(
            "jc",
            role="consumer",
            status="COMPLETED",
            title="My posted job",
            payout=18,
        )
        daemon._update_job(
            "jf",
            role="provider",
            status="FAILED",
            title="Failed job",
            fail_reason="delivery_timeout",
        )
        daemon._update_job("je", role="consumer", status="EXPIRED", title="Expired job")
        daemon._update_job("jl", role="provider", status="LOST", title="Lost job")
        daemon._write_jobs()
        md = _jobs_md(daemon)
        assert '- [PAID +18] "Paid job" (job jp)' in md
        assert '- [PAID] "My posted job" (job jc) — provider received 18 crumbs' in md
        assert '- [FAILED] "Failed job" (job jf) — delivery_timeout' in md
        assert '- [EXPIRED] "Expired job" (job je)' in md
        assert '- [LOST] "Lost job" (job jl)' in md

    def test_award_deadline_sec_renders_absolute_deliver_by(self, daemon: TownDaemon):
        """deadline_sec in job_awarded → deadline_at cached (now + sec) and
        rendered as DELIVER by HH:MM UTC."""
        with patch("time.time", return_value=1_700_000_000.0):
            daemon._handle_event(
                {
                    "event": "job_awarded",
                    "job_id": "j1",
                    "title": "Write a haiku",
                    "agreed_price": 30,
                    "deadline_sec": 600,
                    "provider_name": "testagent",
                    "consumer_name": "othergoose",
                    "description": "A haiku about dusk.",
                }
            )
        job = daemon.state["exchange"]["jobs"]["j1"]
        assert job["deadline_at"] == 1_700_000_600.0
        # 1_700_000_000 epoch is 22:13:20 UTC → +600s = 22:23:20.
        md = _jobs_md(daemon)
        assert '- DELIVER by 22:23 UTC: "Write a haiku" (job j1)' in md

    def test_award_without_deadline_sec_renders_no_clock(self, daemon: TownDaemon):
        daemon._handle_event(
            {
                "event": "job_awarded",
                "job_id": "j1",
                "title": "Write a haiku",
                "agreed_price": 30,
                "provider_name": "testagent",
                "consumer_name": "othergoose",
                "description": "A haiku about dusk.",
            }
        )
        assert daemon.state["exchange"]["jobs"]["j1"].get("deadline_at") is None
        md = _jobs_md(daemon)
        assert '- DELIVER: "Write a haiku" (job j1)' in md
        assert "DELIVER by" not in md

    def test_consumer_award_deadline_rendered_in_waiting(self, daemon: TownDaemon):
        with patch("time.time", return_value=1_700_000_000.0):
            daemon._handle_event(
                {
                    "event": "job_awarded",
                    "job_id": "j1",
                    "title": "My job",
                    "agreed_price": 30,
                    "deadline_sec": 600,
                    "provider_name": "rival",
                    "consumer_name": "testagent",
                }
            )
        md = _jobs_md(daemon)
        assert (
            '- [AWARDED] "My job" (job j1) — agreed 30 crumbs — '
            "awaiting delivery (due 22:23 UTC)" in md
        )

    def test_post_job_bid_window_renders_closes_at(self, daemon: TownDaemon):
        """bid_window_sec on the post_job echo → bids_close_at cached and
        rendered as 'closes HH:MM UTC' on the [OPEN] line."""
        daemon._pending_exchange_payload = {
            "action": "post_job",
            "title": "Write a haiku",
            "description": "d",
            "max_price": 40,
        }
        with patch("time.time", return_value=1_700_000_000.0):
            daemon._handle_event(
                {
                    "event": "exchange_ok",
                    "verb": "post_job",
                    "status": "ok",
                    "job_id": "j1",
                    "bid_window_sec": 120,
                    "escrowed": 40,
                    "balance": 60,
                }
            )
        job = daemon.state["exchange"]["jobs"]["j1"]
        assert job["bids_close_at"] == 1_700_000_120.0
        # 22:13:20 UTC + 120s = 22:15:20.
        md = _jobs_md(daemon)
        assert "closes 22:15 UTC" in md
        assert "no bids yet" in md  # no bid_received cached yet


# ---------------------------------------------------------------------------
# handle_tool_command — exchange verbs await the backend echo
# ---------------------------------------------------------------------------


class _EchoWS:
    """Fake WebSocket: replies to exchange acts by dispatching the echo
    event back into the daemon, the way listen_ws would."""

    def __init__(self, daemon: TownDaemon, reply: dict):
        self._daemon = daemon
        self._reply = reply
        self.sent: list[str] = []

    async def send(self, msg: str) -> None:
        self.sent.append(msg)
        self._daemon._handle_event(self._reply)


class TestHandleToolCommandExchange:
    async def test_post_job_returns_backend_outcome(self, daemon: TownDaemon):
        reply = {
            "event": "exchange_ok",
            "verb": "post_job",
            "status": "ok",
            "job_id": "j1",
            "escrowed": 40,
            "balance": 60,
        }
        daemon.ws = _EchoWS(daemon, reply)
        result = await daemon.handle_tool_command(
            {
                "action": "act",
                "payload": {
                    "action": "post_job",
                    "title": "Write a haiku",
                    "description": "d",
                    "max_price": 40,
                },
            }
        )
        assert result["status"] == "ok"
        assert result["job_id"] == "j1"
        # Future + pending payload cleared after the round-trip
        assert daemon._exchange_outcome is None
        assert daemon._pending_exchange_payload is None
        # The cache picked up the title from the pending payload
        assert daemon.state["exchange"]["jobs"]["j1"]["title"] == "Write a haiku"
        # And the wire message was a town_agent_act
        sent = json.loads(daemon.ws.sent[0])
        assert sent["type"] == "town_agent_act"
        assert sent["action"] == "post_job"

    async def test_rejected_verb_surfaces_to_cli(self, daemon: TownDaemon):
        reply = {
            "event": "exchange_rejected",
            "verb": "bid",
            "status": "rejected",
            "reason": "wrong_location",
            "message": "Walk to the bank first.",
        }
        daemon.ws = _EchoWS(daemon, reply)
        result = await daemon.handle_tool_command(
            {
                "action": "act",
                "payload": {"action": "bid", "job_id": "j1", "price": 10},
            }
        )
        assert result["status"] == "rejected"
        assert result["reason"] == "wrong_location"


# ---------------------------------------------------------------------------
# handle_tool_command — send failure clears exchange outcome state
# ---------------------------------------------------------------------------


class TestExchangeSendFailure:
    async def test_ws_send_failure_clears_exchange_outcome(self, daemon: TownDaemon):
        """If ws.send raises, _exchange_outcome and _pending_exchange_payload are cleared."""

        class _FailWS:
            async def send(self, msg: str) -> None:
                raise OSError("connection reset")

        daemon.ws = _FailWS()
        daemon._arrived_event = (
            asyncio.get_running_loop().create_future()
        )  # satisfy move guard

        # Manually set up the event (handle_tool_command would do this before send)
        # but we're testing the cleanup path so we drive through the full command.
        import contextlib

        with contextlib.suppress(OSError):
            await daemon.handle_tool_command(
                {
                    "action": "act",
                    "payload": {
                        "action": "post_job",
                        "title": "Test job",
                        "description": "d",
                        "max_price": 10,
                    },
                }
            )

        # After the send failure, both fields must be cleared.
        assert daemon._exchange_outcome is None
        assert daemon._pending_exchange_payload is None


# ---------------------------------------------------------------------------
# _write_jobs — fence collision prevention
# ---------------------------------------------------------------------------


class TestWriteJobsFenceNeutralization:
    def test_deliverable_fences_replaced_with_single_quotes(self, daemon: TownDaemon):
        """A deliverable containing ``` is rendered with ''' and the TOWN_JOBS.md
        still has exactly one opening and one closing fence for that entry."""
        daemon._update_job(
            "j1",
            role="consumer",
            status="DELIVERED",
            title="Code review",
            deliverable="Here is the answer:\n```python\nprint('hello')\n```\nDone.",
        )
        daemon._write_jobs()
        md = _jobs_md(daemon)

        # The embedded backtick fences must be neutralized.
        assert "'''" in md
        # The wrapping fence (two lines of "  ```") must still be present.
        assert md.count("  ```") == 2
        # Backtick fences from the deliverable content must NOT appear.
        assert "```python" not in md


# ---------------------------------------------------------------------------
# Connected event: own agent id + cache survival
# ---------------------------------------------------------------------------


class TestConnectedAgentId:
    def test_connected_stores_own_agent_id(self, daemon: TownDaemon):
        daemon._handle_event(
            {
                "event": "connected",
                "agent": {"id": "uuid-me", "name": "Test Agent", "location": "plaza"},
            }
        )
        assert daemon._agent_id == "uuid-me"

    def test_exchange_cache_survives_reconnect(self, daemon: TownDaemon):
        daemon._update_job("j1", role="consumer", status="OPEN", title="T")
        daemon._handle_event(
            {"event": "connected", "agent": {"id": "uuid-me", "location": "plaza"}}
        )
        assert daemon.state["exchange"]["jobs"]["j1"]["title"] == "T"


# ---------------------------------------------------------------------------
# IPC framing: >8 KB frames must be read to EOF (codex P2 on PR #386)
# ---------------------------------------------------------------------------


class TestSocketFrameAssembly:
    async def test_16kb_frame_split_across_chunks_is_reassembled(
        self, daemon: TownDaemon
    ):
        """ipc_send shuts down its write side after sendall; the daemon must
        read until EOF instead of a single read(8192), or 16 KB deliverables
        arrive truncated and json.loads fails."""
        big = json.dumps(
            {
                "action": "act",
                "payload": {
                    "action": "deliver_job",
                    "job_id": "j1",
                    "deliverable": "x" * 16000,
                },
            }
        ).encode()
        assert len(big) > 8192

        reader = asyncio.StreamReader()
        reader.feed_data(big[:6000])
        reader.feed_data(big[6000:])
        reader.feed_eof()

        captured: dict = {}

        async def fake_handle(cmd: dict) -> dict:
            captured.update(cmd)
            return {"status": "ok"}

        writes: list[bytes] = []

        class FakeWriter:
            def write(self, data: bytes) -> None:
                writes.append(data)

            async def drain(self) -> None:
                pass

            def close(self) -> None:
                pass

            async def wait_closed(self) -> None:
                pass

        daemon.handle_tool_command = fake_handle  # type: ignore[method-assign]
        await daemon._handle_socket_client(reader, FakeWriter())

        # The full frame parsed: deliverable arrived intact, no error reply.
        assert captured["payload"]["deliverable"] == "x" * 16000
        assert json.loads(b"".join(writes).decode()) == {"status": "ok"}


# ---------------------------------------------------------------------------
# Connect-time resync events (suppressed TOWN_EVENTS, cache still updates)
# ---------------------------------------------------------------------------


class TestResyncEvents:
    def test_resync_job_awarded_updates_cache_without_event_line(
        self, daemon: TownDaemon
    ):
        daemon._agent_id = "uuid-me"
        daemon._handle_event(
            {
                "event": "job_awarded",
                "job_id": "jr1",
                "title": "Lost award",
                "agreed_price": 12,
                "deadline_sec": 300,
                "provider_id": "uuid-me",
                "consumer_id": "uuid-them",
                "provider_name": "me",
                "consumer_name": "them",
                "description": "the spec",
                "resync": True,
            }
        )

        jobs_md = _jobs_md(daemon)
        assert "Lost award" in jobs_md
        assert "NEEDS YOUR ACTION" in jobs_md
        events_path = daemon._workspace_path / "TOWN_EVENTS.md"
        assert not events_path.exists() or "Lost award" not in events_path.read_text()

    def test_resync_job_delivered_updates_cache_without_event_line(
        self, daemon: TownDaemon
    ):
        daemon._agent_id = "uuid-me"
        daemon._handle_event(
            {
                "event": "job_delivered",
                "job_id": "jr2",
                "title": "Lost delivery",
                "deliverable": "finished work",
                "provider_id": "uuid-them",
                "consumer_id": "uuid-me",
                "resync": True,
            }
        )

        jobs_md = _jobs_md(daemon)
        assert "Lost delivery" in jobs_md
        assert "finished work" in jobs_md
        events_path = daemon._workspace_path / "TOWN_EVENTS.md"
        assert (
            not events_path.exists() or "Lost delivery" not in events_path.read_text()
        )

    def test_non_resync_award_still_writes_event_line(self, daemon: TownDaemon):
        daemon._agent_id = "uuid-me"
        daemon._handle_event(
            {
                "event": "job_awarded",
                "job_id": "jr3",
                "title": "Fresh award",
                "agreed_price": 12,
                "deadline_sec": 300,
                "provider_id": "uuid-me",
                "consumer_id": "uuid-them",
                "description": "the spec",
            }
        )
        assert "Fresh award" in _events(daemon)
