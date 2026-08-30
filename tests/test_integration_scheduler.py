"""The Scheduler end to end: claim a job, pass the gates, send, advance the pipeline.

Gmail and Calendar are stubbed — the point here is the orchestration and the gates, and
the real wrappers have their own containment tests. Firestore is real, because the
state transitions and the cap are the things being asserted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from greenroom.agents.scheduler import Scheduler
from greenroom.config import get_config
from greenroom.state.models import JobStatus, JobType, TargetStatus

pytestmark = pytest.mark.integration

MONDAY_10AM = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)  # 10:00 BST, inside the window
SATURDAY = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)  # a Saturday, comfortably ahead of "now"


@dataclass
class FakeSent:
    message_id: str
    thread_id: str
    dry_run: bool = True


class FakeGmail:
    def __init__(self):
        self.sent: list[dict] = []
        self.fail_next = False

    def send_new(self, *, to, subject, body_text, attachments=None):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("gmail exploded")
        self.sent.append({"to": to, "subject": subject, "kind": "new"})
        return FakeSent(f"msg-{len(self.sent)}", f"thread-{len(self.sent)}")

    def send_reply(
        self, *, to, subject, body_text, thread_id, in_reply_to, references=None, attachments=None
    ):
        self.sent.append({"to": to, "subject": subject, "kind": "reply"})
        return FakeSent(f"msg-{len(self.sent)}", thread_id)


class FakeCalendar:
    def __init__(self):
        self.created: list[str] = []

    def create_event(self, *, summary, description, slot, attendee_email, idempotency_key):
        from greenroom.tools.calendar import BookedEvent

        self.created.append(idempotency_key)
        return BookedEvent(idempotency_key, "https://cal/x", slot.start, True)


@pytest.fixture
def scheduler(repo, queue):
    return Scheduler(
        repo=repo,
        queue=queue,
        config=get_config(),
        gmail=FakeGmail(),
        calendar=FakeCalendar(),
        dry_run=True,
    )


@pytest.fixture
def seeded_target(repo, real_config_dir):
    from greenroom.config import load_targets

    target = load_targets(real_config_dir / "targets.csv").targets[0]
    doc = repo.upsert_target(target)
    repo.set_status(doc.target_id, TargetStatus.RESEARCHED)
    return doc


def _queue_pitch(queue, target_id: str):
    return queue.enqueue(
        job_type=JobType.SEND_PITCH,
        idempotency_key=f"send_pitch:{target_id}",
        target_id=target_id,
        payload={"subject": "Beat ID x your union", "body": "Hello."},
    )[0]


# ------------------------------------------------------------------ happy path


async def test_sending_a_pitch_advances_the_whole_pipeline(
    scheduler, queue, repo, seeded_target, monkeypatch
):
    monkeypatch.setattr("greenroom.agents.scheduler.evaluate_send_gate", _always_open)
    job = _queue_pitch(queue, seeded_target.target_id)

    tally = await scheduler.run_due_jobs(limit=5)

    assert tally["done"] == 1
    assert queue.get(job.job_id).status == JobStatus.DONE

    target = repo.get_target(seeded_target.target_id)
    assert target.status == TargetStatus.PITCHED
    assert target.thread_id, "the thread id must be recorded for containment"

    thread = repo.get_thread(target.thread_id)
    assert thread is not None and thread.target_id == target.target_id

    assert len(scheduler.gmail.sent) == 1
    assert scheduler.gmail.sent[0]["to"] == target.email


async def test_sending_a_pitch_queues_the_whole_follow_up_ladder(
    scheduler, queue, seeded_target, monkeypatch
):
    """Queued up front so a crash between follow-ups cannot lose the rest."""
    monkeypatch.setattr("greenroom.agents.scheduler.evaluate_send_gate", _always_open)
    _queue_pitch(queue, seeded_target.target_id)
    await scheduler.run_due_jobs(limit=5)

    queued = queue.list_by_status(JobStatus.QUEUED)
    kinds = sorted(j.job_type for j in queued)
    assert kinds == ["close_thread", "send_follow_up", "send_follow_up"]
    assert all(j.run_after > datetime.now(UTC) for j in queued), "none may fire today"


# ------------------------------------------------------------------ gates


async def test_nothing_sends_outside_the_send_window(
    scheduler, queue, repo, seeded_target, monkeypatch
):
    """A job blocked by the clock is requeued, not failed.

    Uses an explicitly closed window rather than the shipped policy, which is widened by
    hand from time to time to run live tests outside office hours."""
    from greenroom.state.repo import SendGate

    monkeypatch.setattr(
        "greenroom.agents.scheduler.evaluate_send_gate",
        lambda repo, *, policy, now=None, dry_run=False: SendGate(
            False, "Saturday is outside the send window (weekdays only)"
        ),
    )
    job = _queue_pitch(queue, seeded_target.target_id)

    tally = await scheduler.run_due_jobs(limit=5, now=SATURDAY)

    assert tally["blocked"] == 1
    assert tally["done"] == 0
    assert scheduler.gmail.sent == []
    assert repo.get_target(seeded_target.target_id).status == TargetStatus.RESEARCHED

    after = queue.get(job.job_id)
    assert after.status == JobStatus.QUEUED, "must be retried, not failed"
    assert after.attempts == 0, "a closed window must not burn a retry attempt"


async def test_the_kill_switch_stops_a_send_that_would_otherwise_go(
    scheduler, queue, repo, seeded_target
):
    _queue_pitch(queue, seeded_target.target_id)
    repo.set_paused(True, reason="demo stop")

    # MONDAY_10AM is inside the send window and under the cap, so the only thing that
    # can stop this send is the kill switch.
    tally = await scheduler.run_due_jobs(limit=5, now=MONDAY_10AM)

    assert tally["blocked"] == 1
    assert scheduler.gmail.sent == []


async def test_the_daily_cap_blocks_further_sends(
    scheduler, queue, repo, seeded_target, monkeypatch
):
    monkeypatch.setattr("greenroom.agents.scheduler.evaluate_send_gate", _always_open)
    cap = get_config().policy.operations.max_sends_per_day
    for _ in range(cap):
        repo.reserve_send_slot(cap=cap)

    _queue_pitch(queue, seeded_target.target_id)
    tally = await scheduler.run_due_jobs(limit=5)

    assert tally["blocked"] == 1
    assert scheduler.gmail.sent == []


async def test_a_failed_send_returns_its_reserved_slot(
    scheduler, queue, repo, seeded_target, monkeypatch
):
    """Otherwise a flaky API would silently eat the day's send budget."""
    monkeypatch.setattr("greenroom.agents.scheduler.evaluate_send_gate", _always_open)
    scheduler.gmail.fail_next = True
    _queue_pitch(queue, seeded_target.target_id)

    before = repo.sends_today()
    tally = await scheduler.run_due_jobs(limit=5)

    assert tally["failed"] == 1
    assert repo.sends_today() == before, "the burnt slot must be given back"


# ------------------------------------------------------------------ follow-ups


async def test_a_follow_up_is_dropped_if_the_target_already_replied(
    scheduler, queue, repo, seeded_target, monkeypatch
):
    """The most embarrassing thing an outreach agent can do is nudge someone who
    answered yesterday."""
    monkeypatch.setattr("greenroom.agents.scheduler.evaluate_send_gate", _always_open)
    _queue_pitch(queue, seeded_target.target_id)
    await scheduler.run_due_jobs(limit=5)
    sent_after_pitch = len(scheduler.gmail.sent)

    repo.set_status(seeded_target.target_id, TargetStatus.REPLIED, reason="they answered")

    # Bring the day-3 follow-up forward, as the tick would once its time came.
    for job in queue.list_by_status(JobStatus.QUEUED):
        if job.job_type == JobType.SEND_FOLLOW_UP:
            queue._col().document(job.job_id).update(
                {"run_after": datetime.now(UTC) - timedelta(seconds=1)}
            )

    await scheduler.run_due_jobs(limit=5)

    assert len(scheduler.gmail.sent) == sent_after_pitch, "no nudge after a reply"


# ------------------------------------------------------------------ booking


async def test_booking_a_call_is_idempotent_and_marks_the_target_booked(
    scheduler, queue, repo, seeded_target
):
    repo.set_status(seeded_target.target_id, TargetStatus.PITCHED)
    repo.set_status(seeded_target.target_id, TargetStatus.REPLIED)

    start = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)
    for _ in range(2):  # the same booking enqueued twice, as a retry would
        queue.enqueue(
            job_type=JobType.BOOK_CALL,
            idempotency_key=f"book:{seeded_target.target_id}:2026-09-15T10:00",
            target_id=seeded_target.target_id,
            payload={
                "start": start.isoformat(),
                "end": (start + timedelta(minutes=30)).isoformat(),
            },
        )

    await scheduler.run_due_jobs(limit=5)

    assert len(scheduler.calendar.created) == 1, "one booking, not two"
    assert repo.get_target(seeded_target.target_id).status == TargetStatus.BOOKED


# ------------------------------------------------------------------ helpers


def _always_open(repo, *, policy, now=None, dry_run=False):
    from greenroom.state.repo import SendGate

    return SendGate(True, "", 0, 999)
