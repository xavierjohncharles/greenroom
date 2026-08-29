"""The Scheduler: executes jobs, and owns the send window, daily cap and kill switch.

**This one is deliberately not an LlmAgent.** Every other agent in Greenroom reasons —
the Researcher, Writer, Gatekeeper and Negotiator all need judgement. The Scheduler
does not. It decides whether the clock says 09:00, whether a counter is under 25, and
whether a flag is set. Putting a language model in that loop would add latency, cost
and non-determinism to the one component whose entire job is to be predictable, and
would make "why did it send at 3am?" an unanswerable question. It is a plain worker,
and that is the point.

What it *does* get is the same tool scoping as the reasoning agents: it holds the send
and calendar-create tools, and the read-side agents do not. It is the only component in
the system that can cause an email to leave the building.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from greenroom.config import AppConfig
from greenroom.obs import get_logger, set_log_context
from greenroom.state.models import (
    JobDoc,
    JobType,
    TargetStatus,
    ThreadDoc,
    utcnow,
)
from greenroom.state.repo import Repo, evaluate_send_gate

log = get_logger(__name__)


class SendBlocked(RuntimeError):
    """A send job could not run right now. Not a failure — it stays queued."""


class Scheduler:
    def __init__(
        self,
        *,
        repo: Repo,
        queue: Any,
        config: AppConfig,
        gmail: Any,
        calendar: Any,
        dry_run: bool = True,
    ) -> None:
        self.repo = repo
        self.queue = queue
        self.config = config
        self.gmail = gmail
        self.calendar = calendar
        self.dry_run = dry_run

        self._handlers: dict[JobType, Callable[[JobDoc], dict]] = {
            JobType.SEND_PITCH: self._handle_send_pitch,
            JobType.SEND_REPLY: self._handle_send_reply,
            JobType.SEND_FOLLOW_UP: self._handle_send_follow_up,
            JobType.BOOK_CALL: self._handle_book_call,
            JobType.CLOSE_THREAD: self._handle_close_thread,
            JobType.RENEW_WATCH: self._handle_renew_watch,
        }

    # -- the loop ----------------------------------------------------------
    def run_due_jobs(self, *, limit: int = 10, now: datetime | None = None) -> dict[str, int]:
        """Claim and run up to `limit` due jobs. Returns a tally for the tick log."""
        now = now or utcnow()
        tally = {"claimed": 0, "done": 0, "failed": 0, "blocked": 0}

        for claim in self.queue.claim_next(limit=limit, now=now):
            job = claim.job
            tally["claimed"] += 1
            set_log_context(target_id=job.target_id, thread_id=job.thread_id, job_id=job.job_id)

            handler = self._handlers.get(JobType(job.job_type))
            if handler is None:
                self.queue.fail(job.job_id, f"no handler registered for {job.job_type}")
                tally["failed"] += 1
                continue

            try:
                result = handler(job)
            except SendBlocked as exc:
                # Not a failure: the world simply is not ready yet. Put it back
                # without burning an attempt against max_attempts.
                self._requeue(job, str(exc), now=now)
                tally["blocked"] += 1
                log.info("job blocked, requeued", extra={"reason": str(exc)})
            except Exception as exc:
                self.queue.fail(job.job_id, f"{type(exc).__name__}: {exc}")
                tally["failed"] += 1
                log.error("job failed", extra={"error": str(exc)})
            else:
                self.queue.complete(job.job_id, result)
                tally["done"] += 1

        return tally

    def _requeue(self, job: JobDoc, reason: str, *, now: datetime) -> None:
        """Return a blocked job to the queue without counting it as an attempt.

        A job blocked by the send window would otherwise burn all five attempts
        overnight and be dead by morning.
        """
        from greenroom.state.models import JobStatus

        self.queue._col().document(job.job_id).update(
            {
                "status": JobStatus.QUEUED.value,
                "attempts": max(job.attempts - 1, 0),
                "leased_by": None,
                "lease_expires_at": None,
                "last_error": reason,
                "run_after": now + timedelta(minutes=15),
                "updated_at": now,
            }
        )

    # -- gates -------------------------------------------------------------
    def _assert_may_send(self, now: datetime | None = None) -> None:
        gate = evaluate_send_gate(
            self.repo, policy=self.config.policy, now=now, dry_run=self.dry_run
        )
        if not gate.allowed:
            raise SendBlocked(gate.reason)

    def _reserve(self) -> None:
        ops = self.config.policy.operations
        if not self.repo.reserve_send_slot(cap=ops.max_sends_per_day, tz=ops.send_window.timezone):
            raise SendBlocked(f"daily cap reached ({ops.max_sends_per_day})")

    def _release(self) -> None:
        self.repo.release_send_slot(tz=self.config.policy.operations.send_window.timezone)

    # -- handlers ----------------------------------------------------------
    def _handle_send_pitch(self, job: JobDoc) -> dict:
        """Send the first email to a target and open its thread."""
        self._assert_may_send()
        target = self.repo.get_target(job.target_id or "")
        if target is None:
            raise RuntimeError(f"target {job.target_id} not found")

        subject = job.payload["subject"]
        body = job.payload["body"]

        self._reserve()
        try:
            sent = self.gmail.send_new(to=target.email, subject=subject, body_text=body)
        except Exception:
            self._release()
            raise

        self.repo.create_thread(
            ThreadDoc(
                gmail_thread_id=sent.thread_id,
                target_id=target.target_id,
                subject=subject,
                last_message_at=utcnow(),
                last_outbound_at=utcnow(),
            )
        )
        self.repo._col("targets").document(target.target_id).update(
            {"thread_id": sent.thread_id, "updated_at": utcnow()}
        )
        self.repo.set_status(target.target_id, TargetStatus.PITCHED, reason="pitch sent")
        self._schedule_follow_ups(target_id=target.target_id, thread_id=sent.thread_id)

        return {"message_id": sent.message_id, "thread_id": sent.thread_id, "dry_run": sent.dry_run}

    def _handle_send_reply(self, job: JobDoc) -> dict:
        self._assert_may_send()
        target = self.repo.get_target(job.target_id or "")
        if target is None:
            raise RuntimeError(f"target {job.target_id} not found")

        self._reserve()
        try:
            sent = self.gmail.send_reply(
                to=target.email,
                subject=job.payload["subject"],
                body_text=job.payload["body"],
                thread_id=job.thread_id or "",
                in_reply_to=job.payload.get("in_reply_to", ""),
                references=job.payload.get("references"),
            )
        except Exception:
            self._release()
            raise

        if job.thread_id:
            self.repo._col("threads").document(job.thread_id).update(
                {"last_outbound_at": utcnow(), "last_message_at": utcnow(), "updated_at": utcnow()}
            )
        return {"message_id": sent.message_id, "dry_run": sent.dry_run}

    def _handle_send_follow_up(self, job: JobDoc) -> dict:
        """Send a nudge — unless the target already replied, in which case drop it.

        The check matters: a follow-up scheduled three days ago must not fire at a
        contact who answered yesterday. That is the single most embarrassing thing an
        outreach agent can do.
        """
        target = self.repo.get_target(job.target_id or "")
        if target is None:
            raise RuntimeError(f"target {job.target_id} not found")

        if TargetStatus(target.status) != TargetStatus.PITCHED:
            log.info(
                "follow-up cancelled, target has moved on",
                extra={"status": str(target.status)},
            )
            return {"skipped": True, "reason": f"status is {target.status}"}

        thread = self.repo.get_thread(job.thread_id or "")
        if thread is not None and thread.closed:
            return {"skipped": True, "reason": "thread closed"}

        self._assert_may_send()
        self._reserve()
        try:
            sent = self.gmail.send_reply(
                to=target.email,
                subject=job.payload["subject"],
                body_text=job.payload["body"],
                thread_id=job.thread_id or "",
                in_reply_to=job.payload.get("in_reply_to", ""),
            )
        except Exception:
            self._release()
            raise

        if job.thread_id:
            self.repo._col("threads").document(job.thread_id).update(
                {
                    "follow_ups_sent": (thread.follow_ups_sent + 1) if thread else 1,
                    "last_outbound_at": utcnow(),
                    "updated_at": utcnow(),
                }
            )
        return {"message_id": sent.message_id, "dry_run": sent.dry_run}

    def _handle_book_call(self, job: JobDoc) -> dict:
        """Create the call. Not gated by the send window — a booking is a confirmation
        of something already agreed, and the calendar invite should not wait until 9am."""
        from greenroom.tools.calendar import Slot

        target = self.repo.get_target(job.target_id or "")
        if target is None:
            raise RuntimeError(f"target {job.target_id} not found")

        start = datetime.fromisoformat(job.payload["start"])
        end = datetime.fromisoformat(job.payload["end"])
        booked = self.calendar.create_event(
            summary=job.payload.get("summary", f"Beat ID x {target.organisation}"),
            description=job.payload.get("description", ""),
            slot=Slot(start, end),
            attendee_email=target.email,
            idempotency_key=job.idempotency_key,
        )
        self.repo.set_status(target.target_id, TargetStatus.BOOKED, reason="call booked")
        return {"event_id": booked.event_id, "link": booked.html_link, "dry_run": booked.dry_run}

    def _handle_close_thread(self, job: JobDoc) -> dict:
        target = self.repo.get_target(job.target_id or "")
        if target is None:
            raise RuntimeError(f"target {job.target_id} not found")
        if TargetStatus(target.status) != TargetStatus.PITCHED:
            return {"skipped": True, "reason": f"status is {target.status}"}

        self.repo.set_status(
            target.target_id, TargetStatus.CLOSED_NO_REPLY, reason="no reply after follow-ups"
        )
        if job.thread_id:
            self.repo._col("threads").document(job.thread_id).update(
                {"closed": True, "updated_at": utcnow()}
            )
        return {"closed": True}

    def _handle_renew_watch(self, job: JobDoc) -> dict:
        """Gmail's watch expires after 7 days; renew it well inside that."""
        topic = job.payload["topic"]
        response = self.gmail.start_watch(topic)
        return {"history_id": response.get("historyId"), "expiration": response.get("expiration")}

    # -- scheduling --------------------------------------------------------
    def _schedule_follow_ups(self, *, target_id: str, thread_id: str) -> None:
        """Queue the follow-up ladder and the eventual close, all at once.

        Queued up front rather than one at a time so the whole future of a thread is
        visible in the jobs collection the moment it starts — and so a crash between
        follow-ups cannot lose the rest of the sequence.
        """
        ops = self.config.policy.operations
        now = utcnow()

        for day in ops.follow_up_days:
            self.queue.enqueue(
                job_type=JobType.SEND_FOLLOW_UP,
                idempotency_key=f"follow_up:{thread_id}:day{day}",
                target_id=target_id,
                thread_id=thread_id,
                run_after=now + timedelta(days=day),
                payload={"day": day, "subject": "", "body": ""},
            )

        self.queue.enqueue(
            job_type=JobType.CLOSE_THREAD,
            idempotency_key=f"close:{thread_id}",
            target_id=target_id,
            thread_id=thread_id,
            run_after=now + timedelta(days=ops.close_after_days),
        )


def utc_now() -> datetime:
    return datetime.now(UTC)
