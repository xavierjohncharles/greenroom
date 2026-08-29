"""Job queue rules that need no database.

The claim predicate is pulled out as a pure function precisely so the crash-recovery
behaviour can be asserted without staging a real crash.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from greenroom.jobs.queue import BACKOFF_SECONDS, _is_runnable, backoff_for, job_id_for
from greenroom.state.models import JobStatus

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


# ------------------------------------------------------------------ idempotency key


def test_same_key_gives_the_same_id():
    assert job_id_for("send_pitch:goldsmiths") == job_id_for("send_pitch:goldsmiths")


def test_different_keys_give_different_ids():
    assert job_id_for("send_pitch:a") != job_id_for("send_pitch:b")


def test_id_is_a_valid_firestore_document_id():
    """Keys contain slashes and can be long; document ids may not be."""
    job_id = job_id_for("send_reply:thread/with/slashes:" + "x" * 500)
    assert "/" not in job_id
    assert 0 < len(job_id) <= 1500


# ------------------------------------------------------------------ backoff


def test_backoff_grows_then_plateaus():
    delays = [backoff_for(i).total_seconds() for i in range(1, 8)]
    assert delays[:4] == list(BACKOFF_SECONDS)
    assert delays[4:] == [BACKOFF_SECONDS[-1]] * 3, "should cap, not grow forever"


def test_backoff_handles_attempt_zero():
    assert backoff_for(0) == backoff_for(1)


# ------------------------------------------------------------------ runnable


def test_queued_job_whose_time_has_come_is_runnable():
    assert _is_runnable({"status": JobStatus.QUEUED.value, "run_after": NOW}, NOW)


def test_queued_job_scheduled_for_later_is_not_runnable():
    """A follow-up queued for day 3 must not fire today."""
    future = {"status": JobStatus.QUEUED.value, "run_after": NOW + timedelta(days=3)}
    assert not _is_runnable(future, NOW)


def test_failed_job_waits_for_its_backoff():
    assert not _is_runnable(
        {"status": JobStatus.FAILED.value, "run_after": NOW + timedelta(seconds=30)}, NOW
    )
    assert _is_runnable(
        {"status": JobStatus.FAILED.value, "run_after": NOW - timedelta(seconds=1)}, NOW
    )


def test_running_job_with_a_live_lease_is_not_stealable():
    """Two Cloud Run instances must not both run the same send."""
    held = {
        "status": JobStatus.RUNNING.value,
        "lease_expires_at": NOW + timedelta(minutes=4),
        "run_after": NOW,
    }
    assert not _is_runnable(held, NOW)


def test_running_job_with_an_expired_lease_is_reclaimable():
    """This is the crash recovery: a worker that died leaves a lapsing lease."""
    crashed = {
        "status": JobStatus.RUNNING.value,
        "lease_expires_at": NOW - timedelta(seconds=1),
        "run_after": NOW,
    }
    assert _is_runnable(crashed, NOW)


def test_finished_jobs_are_never_runnable():
    for status in (JobStatus.DONE, JobStatus.DEAD, JobStatus.CANCELLED):
        assert not _is_runnable({"status": status.value, "run_after": NOW}, NOW)


def test_naive_datetimes_are_treated_as_utc():
    """Firestore returns aware datetimes, but a hand-built fixture may not."""
    naive = {"status": JobStatus.QUEUED.value, "run_after": datetime(2026, 8, 30, 11, 0)}
    assert _is_runnable(naive, NOW)
