"""Inbound end to end: a real injection email, and a real counter-offer.

Gmail is stubbed (there is no live mailbox in CI) but the Gatekeeper, the Negotiator and
the policy evaluator are real, and Firestore is real. These are the two paths the whole
system exists to get right:

  * an attack must stop at the Gatekeeper and never reach an agent that drafts replies
  * a genuine out-of-policy counter-offer must produce an escalation citing the rule
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fixtures.inbound_emails import ALL_FIXTURES  # noqa: E402

from greenroom.state.models import (  # noqa: E402
    DraftStatus,
    TargetStatus,
    ThreadDoc,
    TrustMode,
)

pytestmark = pytest.mark.integration

THREAD_ID = "thread-inbound-1"
MESSAGE_ID = "msg-inbound-1"


def fixture(key: str):
    return next(f for f in ALL_FIXTURES if f.key == key)


@dataclass
class StubInbound:
    message_id: str
    thread_id: str
    history_id: str
    from_addr: str
    to_addr: str
    subject: str
    body_text: str
    rfc822_message_id: str
    internal_date_ms: int
    label_ids: tuple


class StubGmail:
    """Returns one message and records every label applied. Cannot send."""

    def __init__(self, message: StubInbound):
        self.message = message
        self.labels: list[tuple[str, str]] = []

    def get_thread(self, thread_id, *, owned_thread_ids):
        assert thread_id in owned_thread_ids, "containment: refused to read a foreign thread"
        return [self.message]

    def add_label(self, thread_id, label_name):
        self.labels.append((thread_id, label_name))


class StubCalendar:
    def propose_slots(self, **_):
        from datetime import UTC, datetime, timedelta

        from greenroom.tools.calendar import Slot

        start = datetime(2026, 9, 10, 10, 0, tzinfo=UTC)
        return [Slot(start, start + timedelta(minutes=30))]


@pytest.fixture
def wired(repo, queue, monkeypatch, real_config_dir):
    """A target with an open thread, and the inbound pipeline pointed at the namespace."""
    from greenroom.agents.scheduler import Scheduler
    from greenroom.config import get_config, load_targets
    from greenroom.web import deps

    target = load_targets(real_config_dir / "targets.csv").targets[0]
    doc = repo.upsert_target(target)
    repo.set_status(doc.target_id, TargetStatus.RESEARCHED)
    repo.set_status(doc.target_id, TargetStatus.PITCHED)
    repo.create_thread(
        ThreadDoc(gmail_thread_id=THREAD_ID, target_id=doc.target_id, subject="Beat ID")
    )

    def build(fixture_key: str, body: str | None = None):
        f = fixture(fixture_key)
        message = StubInbound(
            message_id=MESSAGE_ID,
            thread_id=THREAD_ID,
            history_id="1",
            from_addr=doc.email,
            to_addr="admin@beatidapp.com",
            subject=f.subject,
            body_text=body if body is not None else f.body,
            rfc822_message_id="<orig@su.ac.uk>",
            internal_date_ms=0,
            label_ids=(),
        )
        gmail = StubGmail(message)
        scheduler = Scheduler(
            repo=repo,
            queue=queue,
            config=get_config(),
            gmail=gmail,
            calendar=StubCalendar(),
            dry_run=True,
        )
        monkeypatch.setattr(deps, "get_repo", lambda: repo)
        monkeypatch.setattr(deps, "get_queue", lambda: queue)
        monkeypatch.setattr(deps, "get_scheduler", lambda: scheduler)
        return doc, gmail

    return build


# ------------------------------------------------------------------ the attack path


async def test_an_injection_is_quarantined_and_never_reaches_the_negotiator(wired, repo, queue):
    """The whole security design in one test: an attack stops at the Gatekeeper.

    If this fails, attacker-controlled text is reaching an agent that writes replies.

    This one asserts a real model call on a deliberately subtle attack — the fixture the
    regex layer cannot see — so it is inherently non-deterministic and has flaked once in
    a full run. That is worth stating rather than hiding: the two-layer design exists
    precisely because a single model judgement on borderline text is not reliable enough
    to stand alone. The blatant attacks are covered by `prescreen`, which cannot flake.
    """
    target, gmail = wired("subtle_embedded_instruction")

    from greenroom.jobs.inbound import process_message

    outcome = await process_message(
        message_id=MESSAGE_ID, thread_id=THREAD_ID, owned=frozenset({THREAD_ID})
    )

    assert outcome == "quarantined"

    stored = repo._col("messages").document(MESSAGE_ID).get().to_dict()
    assert stored["quarantined"] is True
    assert stored["injection_flags"], "the reason must be recorded for the quarantine page"
    assert stored["quarantine_reason"]

    assert (THREAD_ID, "greenroom/quarantine") in gmail.labels
    assert repo.get_target(target.target_id).status == TargetStatus.ESCALATED

    assert repo.list_drafts() == [], "no reply may be drafted to an attack"
    assert queue.list_by_status.__self__ is queue  # sanity
    from greenroom.state.models import JobStatus

    assert queue.list_by_status(JobStatus.QUEUED) == [], "and certainly no send queued"


async def test_the_blatant_injection_is_also_quarantined(wired, repo):
    target, gmail = wired("ignore_previous_and_exfiltrate")

    from greenroom.jobs.inbound import process_message

    assert (
        await process_message(
            message_id=MESSAGE_ID, thread_id=THREAD_ID, owned=frozenset({THREAD_ID})
        )
        == "quarantined"
    )
    assert repo.list_drafts() == []
    assert (THREAD_ID, "greenroom/quarantine") in gmail.labels


async def test_a_thread_we_do_not_own_is_never_read(wired, repo):
    """Containment: the read side refuses anything outside Greenroom's own threads."""
    wired("interested")
    from greenroom.jobs.inbound import process_message

    outcome = await process_message(
        message_id=MESSAGE_ID, thread_id="someone-elses-thread", owned=frozenset({THREAD_ID})
    )
    assert outcome == "skipped"
    assert repo.list_drafts() == []


# ------------------------------------------------------------------ the genuine path


async def test_a_counter_offer_below_the_floor_escalates_citing_the_rule(wired, repo):
    """An offer under the floor is drafted, held for a human, and cites the rule.

    The amount is derived from the configured floor rather than written into the fixture.
    When the floor moved from 850 to 500, the fixture's "budget is 600" stopped being a
    below-floor offer and started being an acceptable one — the test failed for a reason
    that had nothing to do with the behaviour it was checking.
    """
    from greenroom.config import get_config

    floor = int(get_config().policy.fee.floor)
    offer = floor - 200
    target, gmail = wired(
        "counter_below_floor",
        body=(
            f"We'd be interested but our entertainment budget is {offer}. "
            "Is that something you could work with?\n\nSam"
        ),
    )

    from greenroom.jobs.inbound import process_message

    assert (
        await process_message(
            message_id=MESSAGE_ID, thread_id=THREAD_ID, owned=frozenset({THREAD_ID})
        )
        == "processed"
    )

    drafts = repo.list_drafts()
    assert len(drafts) == 1
    draft = drafts[0]

    assert draft.is_escalation is True
    assert draft.status == DraftStatus.PENDING, "an escalation never sends itself"
    assert draft.mode_at_draft == TrustMode.REVIEW
    from greenroom.config import get_config

    floor = int(get_config().policy.fee.floor)
    assert "fee.floor" in draft.policy_rule
    assert str(floor) in draft.policy_rule, "cite the configured number, not a paraphrase"
    assert draft.body, "a recommended reply is still drafted for the human to approve"

    assert (THREAD_ID, "greenroom/escalated") in gmail.labels
    assert repo.get_target(target.target_id).status == TargetStatus.ESCALATED


async def test_an_escalation_stays_in_review_even_on_autopilot(wired, repo):
    """Earned autonomy is permission to skip review on ordinary replies, never on a
    decision that falls outside the envelope."""
    target, _ = wired("free_event_request")
    repo.set_mode(target.target_id, TrustMode.AUTOPILOT)

    from greenroom.jobs.inbound import process_message
    from greenroom.state.models import JobStatus

    await process_message(message_id=MESSAGE_ID, thread_id=THREAD_ID, owned=frozenset({THREAD_ID}))

    draft = repo.list_drafts()[0]
    assert draft.is_escalation is True
    assert draft.mode_at_draft == TrustMode.REVIEW
    assert draft.status == DraftStatus.PENDING

    from greenroom.web.deps import get_queue

    assert get_queue().list_by_status(JobStatus.QUEUED) == [], "autopilot must not send this"


async def test_a_decline_closes_without_drafting_a_reply(wired, repo):
    """Not everything deserves a reply. Escalating a 'no thanks' wastes the founder's
    attention, which is the scarce resource this system exists to protect."""
    target, _ = wired("decline")

    from greenroom.jobs.inbound import process_message

    await process_message(message_id=MESSAGE_ID, thread_id=THREAD_ID, owned=frozenset({THREAD_ID}))

    assert repo.get_target(target.target_id).status == TargetStatus.DECLINED
    assert repo.list_drafts() == []


async def test_a_duplicate_delivery_is_processed_once(wired, repo):
    """Pub/Sub is at-least-once. A redelivered reply must not draft twice."""
    wired("question_whats_included")

    from greenroom.jobs.inbound import process_message

    first = await process_message(
        message_id=MESSAGE_ID, thread_id=THREAD_ID, owned=frozenset({THREAD_ID})
    )
    second = await process_message(
        message_id=MESSAGE_ID, thread_id=THREAD_ID, owned=frozenset({THREAD_ID})
    )

    assert first == "processed"
    assert second == "duplicate"
    assert len(repo.list_drafts()) == 1, "exactly one draft, not two"


async def test_concurrent_deliveries_of_the_same_message_draft_once(wired, repo):
    """Regression: three retried Pub/Sub notifications arrived together, all three read
    "not seen yet" before any wrote, and all three drafted a reply to the same email.
    The dedupe was a check-then-write; it is now an atomic create()."""
    import asyncio

    wired("question_whats_included")
    from greenroom.jobs.inbound import process_message

    outcomes = await asyncio.gather(
        *[
            process_message(
                message_id=MESSAGE_ID, thread_id=THREAD_ID, owned=frozenset({THREAD_ID})
            )
            for _ in range(3)
        ]
    )

    assert outcomes.count("processed") == 1, f"exactly one must process, got {outcomes}"
    assert outcomes.count("duplicate") == 2
    assert len(repo.list_drafts()) == 1, "one email, one draft"


async def test_a_screening_failure_releases_the_claim(wired, repo, monkeypatch):
    """A claim that outlives a transient failure would silently drop a real reply."""
    wired("interested")
    from greenroom.jobs import inbound

    async def boom(**_kwargs):
        raise RuntimeError("gemini unavailable")

    monkeypatch.setattr("greenroom.agents.gatekeeper.screen", boom)
    with pytest.raises(RuntimeError):
        await inbound.process_message(
            message_id=MESSAGE_ID, thread_id=THREAD_ID, owned=frozenset({THREAD_ID})
        )

    assert not repo._col("messages").document(MESSAGE_ID).get().exists, (
        "the claim must be released so a retry can reprocess"
    )
