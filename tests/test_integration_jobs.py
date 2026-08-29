"""Idempotency, leasing and crash recovery, against real Firestore.

Run against the live database in a throwaway namespace. These are the claims that
matter most for "a crashed worker must be safely re-runnable", and a hand-written fake
queue would only ever prove that the fake works.
"""

from __future__ import annotations

import time
from datetime import timedelta

import pytest

from greenroom.state.models import JobStatus, JobType, TargetStatus, TrustMode, utcnow

pytestmark = pytest.mark.integration


# ------------------------------------------------------------------ idempotency


def test_enqueueing_the_same_key_twice_creates_one_job(queue):
    """Pub/Sub is at-least-once. A redelivered notification must not send twice."""
    key = "send_pitch:goldsmiths-su"
    first, created_first = queue.enqueue(job_type=JobType.SEND_PITCH, idempotency_key=key)
    second, created_second = queue.enqueue(job_type=JobType.SEND_PITCH, idempotency_key=key)

    assert created_first is True
    assert created_second is False, "the duplicate must not create a second job"
    assert first.job_id == second.job_id


def test_duplicate_enqueue_does_not_clobber_a_running_job(queue):
    """The duplicate loses the race cleanly rather than resetting state mid-flight."""
    key = "send_pitch:in-flight"
    queue.enqueue(job_type=JobType.SEND_PITCH, idempotency_key=key)
    claimed = queue.claim_next(limit=1)
    assert claimed

    _, created = queue.enqueue(job_type=JobType.SEND_PITCH, idempotency_key=key)
    assert created is False

    still = queue.get(claimed[0].job.job_id)
    assert still.status == JobStatus.RUNNING, "the running job must be untouched"
    assert still.attempts == 1


def test_different_keys_are_different_jobs(queue):
    a, _ = queue.enqueue(job_type=JobType.SEND_PITCH, idempotency_key="pitch:a")
    b, _ = queue.enqueue(job_type=JobType.SEND_PITCH, idempotency_key="pitch:b")
    assert a.job_id != b.job_id


# ------------------------------------------------------------------ leasing


def test_a_job_can_only_be_claimed_once(queue):
    """Two Cloud Run instances racing for the same send."""
    queue.enqueue(job_type=JobType.SEND_PITCH, idempotency_key="only-once")

    first = queue.claim_next(worker_id="worker-a", limit=1)
    second = queue.claim_next(worker_id="worker-b", limit=1)

    assert len(first) == 1
    assert second == [], "a second worker must not get the same job"


def test_claiming_increments_attempts(queue):
    queue.enqueue(job_type=JobType.SEND_PITCH, idempotency_key="attempts")
    claimed = queue.claim_next(limit=1)
    assert claimed[0].job.attempts == 1


def test_a_job_scheduled_for_the_future_is_not_claimed(queue):
    """A day-3 follow-up must not fire the moment it is queued."""
    queue.enqueue(
        job_type=JobType.SEND_FOLLOW_UP,
        idempotency_key="future",
        run_after=utcnow() + timedelta(days=3),
    )
    assert queue.claim_next(limit=5) == []


# ------------------------------------------------------------------ crash recovery


def test_a_crashed_worker_releases_its_job(queue):
    """The core failure-tolerance claim: kill a worker mid-job and the job comes back
    on its own, with no operator intervention. The queue fixture uses a 2s lease."""
    queue.enqueue(job_type=JobType.SEND_PITCH, idempotency_key="crash-me")

    crashed = queue.claim_next(worker_id="doomed", limit=1)
    assert len(crashed) == 1
    # The worker now dies without calling complete() or fail().

    assert queue.claim_next(worker_id="survivor", limit=1) == [], "lease still live"

    time.sleep(2.5)

    recovered = queue.claim_next(worker_id="survivor", limit=1)
    assert len(recovered) == 1, "the job must return to the queue once the lease lapses"
    assert recovered[0].job.job_id == crashed[0].job.job_id
    assert recovered[0].job.attempts == 2, "the retry counts as an attempt"


def test_failure_backs_off_then_dies_for_a_human(queue):
    """Five failures is a signal, not something to retry forever in silence."""
    job, _ = queue.enqueue(job_type=JobType.SEND_PITCH, idempotency_key="doomed", max_attempts=2)

    queue.claim_next(limit=1)
    assert queue.fail(job.job_id, "SMTP exploded") == JobStatus.FAILED

    queue.get(job.job_id)
    queue._col().document(job.job_id).update({"run_after": utcnow() - timedelta(seconds=1)})
    queue.claim_next(limit=1)
    assert queue.fail(job.job_id, "SMTP exploded again") == JobStatus.DEAD

    dead = queue.get(job.job_id)
    assert dead.status == JobStatus.DEAD
    assert "exploded" in dead.last_error


def test_completing_a_job_clears_its_lease(queue):
    job, _ = queue.enqueue(job_type=JobType.SEND_PITCH, idempotency_key="finish-me")
    queue.claim_next(limit=1)
    queue.complete(job.job_id, {"message_id": "abc"})

    done = queue.get(job.job_id)
    assert done.status == JobStatus.DONE
    assert done.leased_by is None
    assert done.result["message_id"] == "abc"
    assert queue.claim_next(limit=5) == [], "a done job is never claimed again"


# ------------------------------------------------------------------ caps & kill switch


def test_the_daily_cap_is_enforced_atomically(repo):
    """Reserving before sending is what stops two instances both passing the check."""
    assert repo.reserve_send_slot(cap=3) is True
    assert repo.reserve_send_slot(cap=3) is True
    assert repo.reserve_send_slot(cap=3) is True
    assert repo.reserve_send_slot(cap=3) is False, "the fourth send must be refused"
    assert repo.sends_today() == 3


def test_a_failed_send_gives_its_slot_back(repo):
    repo.reserve_send_slot(cap=2)
    assert repo.sends_today() == 1
    repo.release_send_slot()
    assert repo.sends_today() == 0


def test_the_kill_switch_reports_its_reason(repo):
    assert repo.is_paused() == (False, "")
    repo.set_paused(True, reason="Xavier hit stop during the demo")
    paused, reason = repo.is_paused()
    assert paused is True
    assert "demo" in reason


def test_the_send_gate_refuses_everything_when_paused(repo, real_config_dir):
    """The kill switch is checked first, so it overrides window and cap alike."""
    from datetime import UTC, datetime

    from greenroom.config import load_policy
    from greenroom.state.repo import evaluate_send_gate

    policy = load_policy(real_config_dir / "policy.yaml")
    monday_10am = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)

    assert evaluate_send_gate(repo, policy=policy, now=monday_10am).allowed is True

    repo.set_paused(True, reason="stop")
    gate = evaluate_send_gate(repo, policy=policy, now=monday_10am)
    assert gate.allowed is False
    assert "paused" in gate.reason


# ------------------------------------------------------------------ target state


def test_target_status_transitions_are_enforced_in_firestore(repo, real_config_dir):
    from greenroom.config import load_targets
    from greenroom.state.machine import InvalidTransition

    target = load_targets(real_config_dir / "targets.csv").targets[0]
    doc = repo.upsert_target(target)
    assert doc.status == TargetStatus.QUEUED
    assert doc.mode == TrustMode.REVIEW, "targets must start under review"

    repo.set_status(doc.target_id, TargetStatus.RESEARCHED)
    with pytest.raises(InvalidTransition):
        repo.set_status(doc.target_id, TargetStatus.BOOKED)

    assert repo.get_target(doc.target_id).status == TargetStatus.RESEARCHED


def test_resyncing_targets_does_not_reset_one_in_flight(repo, real_config_dir):
    """Editing targets.csv mid-campaign must not throw away progress."""
    from greenroom.config import load_targets

    target = load_targets(real_config_dir / "targets.csv").targets[0]
    repo.upsert_target(target)
    repo.set_status(target.key, TargetStatus.RESEARCHED)
    repo.set_mode(target.key, TrustMode.VETO)

    repo.upsert_target(target)  # a second sync, as happens on every deploy

    after = repo.get_target(target.key)
    assert after.status == TargetStatus.RESEARCHED, "status must survive a resync"
    assert after.mode == TrustMode.VETO, "earned trust must survive a resync"
