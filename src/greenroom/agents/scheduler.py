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

import inspect
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from greenroom.config import AppConfig
from greenroom.obs import get_logger, set_log_context
from greenroom.settings import get_settings
from greenroom.state.models import (
    DraftDoc,
    DraftStatus,
    JobDoc,
    JobType,
    TargetStatus,
    ThreadDoc,
    TrustMode,
    utcnow,
)
from greenroom.state.repo import Repo, evaluate_send_gate
from greenroom.tools.images import RateLimited

log = get_logger(__name__)


class SendBlocked(RuntimeError):
    """A send job could not run right now. Not a failure — it stays queued."""


# Conditions that mean "not now" rather than "broken". A job stopped by one of these is
# requeued without burning a retry attempt. The distinction matters: five quota blips
# would otherwise kill a poster job that was never faulty, exactly as a closed send
# window would once have killed a pitch.
BLOCKING_ERRORS: tuple[type[BaseException], ...] = (SendBlocked, RateLimited)


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

        self._handlers: dict[JobType, Callable[[JobDoc], Any]] = {
            JobType.RESEARCH_TARGET: self._handle_research_target,
            JobType.GENERATE_POSTER: self._handle_generate_poster,
            JobType.DRAFT_PITCH: self._handle_draft_pitch,
            JobType.SEND_PITCH: self._handle_send_pitch,
            JobType.SEND_REPLY: self._handle_send_reply,
            JobType.SEND_FOLLOW_UP: self._handle_send_follow_up,
            JobType.BOOK_CALL: self._handle_book_call,
            JobType.CLOSE_THREAD: self._handle_close_thread,
            JobType.RENEW_WATCH: self._handle_renew_watch,
        }

    # -- the loop ----------------------------------------------------------
    async def run_due_jobs(self, *, limit: int = 10, now: datetime | None = None) -> dict[str, int]:
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
                # Handlers that drive an ADK agent are coroutines; the plain ones are
                # not. Awaiting conditionally keeps both kinds in one registry.
                if inspect.isawaitable(result):
                    result = await result
            except BLOCKING_ERRORS as exc:
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

    # -- handlers: reasoning -----------------------------------------------
    async def _handle_research_target(self, job: JobDoc) -> dict:
        """Run the Researcher, store the result, and queue the draft."""
        from greenroom.agents.researcher import research

        target = self._require_target(job)
        doc = await research(target)

        self.repo._col("targets").document(target.target_id).update(
            {"research": doc.model_dump(), "updated_at": utcnow()}
        )
        self.repo.append_event(
            kind="researched",
            target_id=target.target_id,
            detail={
                "hook": (doc.best_hook or "(none found)")[:300],
                "source": doc.hook_source,
                "venue": doc.venue_name,
                "confidence": doc.confidence,
            },
        )
        self.repo.set_status(target.target_id, TargetStatus.RESEARCHED, reason="research complete")

        # Poster and draft are independent: a failed poster must not block the pitch.
        # An email with no poster is a slightly plainer email; an email that never sends
        # because an image model was busy is a lost target.
        self.queue.enqueue(
            job_type=JobType.GENERATE_POSTER,
            idempotency_key=f"poster:{target.target_id}",
            target_id=target.target_id,
        )
        self.queue.enqueue(
            job_type=JobType.DRAFT_PITCH,
            idempotency_key=f"draft_pitch:{target.target_id}",
            target_id=target.target_id,
        )
        return {"confidence": doc.confidence, "has_hook": bool(doc.best_hook)}

    def _handle_generate_poster(self, job: JobDoc) -> dict:
        """Generate the poster and record it on the target."""
        from greenroom.tools.images import make_poster

        target = self._require_target(job)
        research = target.research or {}

        # NOT gated by dry-run — the third time this distinction has come up, after the
        # Gmail labels and the watch. Dry-run means "contact nobody", and generating an
        # image contacts nobody. Gating it produced twenty drafts with no posters and a
        # pipeline reporting complete success, which is worse than either outcome.
        #
        # It does cost money. That is a different concern from safety and gets its own
        # switch rather than borrowing this one.
        poster = make_poster(
            target_id=target.target_id,
            organisation=target.organisation,
            venue=research.get("venue_name", "") or target.venue_notes[:40],
            # Passed whole: the prompt module decides whether it fits or falls back.
            date_line=research.get("freshers_timing", ""),
            dry_run=not get_settings().generate_posters,
        )
        self.repo._col("targets").document(target.target_id).update(
            {"poster_url": poster.public_url or poster.gcs_uri, "updated_at": utcnow()}
        )
        return {"model": poster.model, "bytes": len(poster.png), "uri": poster.gcs_uri}

    async def _handle_draft_pitch(self, job: JobDoc) -> dict:
        """Run the Writer, then route the draft according to the target's trust mode.

        This is the trust dial's teeth. In `review` nothing is queued at all — the draft
        waits on the dashboard. In `veto` a send job is queued for 30 minutes' time and
        can still be stopped. Only `autopilot` queues an immediate send.
        """
        from greenroom.agents.schemas import ResearchDoc
        from greenroom.agents.writer import write_pitch

        target = self._require_target(job)
        research_doc = ResearchDoc.model_validate(target.research) if target.research else None

        draft, problems = await write_pitch(
            target=target,
            research=research_doc,
            config=self.config,
            decisions=self.repo.recent_decisions(limit=10),
            style_memo=self._style_memo(),
        )

        mode = TrustMode(target.mode)
        # A draft that breaks a hard copy rule goes to a human regardless of mode. Earned
        # autonomy is permission to skip review, not permission to send something broken.
        if problems:
            mode = TrustMode.REVIEW

        doc = DraftDoc(
            draft_id=uuid.uuid4().hex,
            target_id=target.target_id,
            kind="pitch",
            subject=draft.subject,
            body=draft.body,
            original_subject=draft.subject,
            original_body=draft.body,
            mode_at_draft=mode,
            copy_problems=problems,
            hook_used=draft.hook_used,
            reasoning=draft.reasoning,
            auto_send_at=(
                utcnow() + timedelta(minutes=self.config.policy.operations.veto_window_minutes)
                if mode == TrustMode.VETO
                else None
            ),
        )
        self.repo.create_draft(doc)
        self.repo.append_event(
            kind="drafted",
            target_id=target.target_id,
            detail={
                "subject": draft.subject,
                "words": len(draft.body.split()),
                "hook_used": draft.hook_used[:200],
                "why": draft.reasoning[:300],
                "mode": str(mode),
                "problems": problems,
            },
        )

        if mode == TrustMode.AUTOPILOT:
            self.enqueue_send_for_draft(doc)
            self.repo.resolve_draft(doc.draft_id, status=DraftStatus.APPROVED)

        return {
            "draft_id": doc.draft_id,
            "mode": str(mode),
            "problems": problems,
            "words": len(draft.body.split()),
        }

    def enqueue_send_for_draft(self, draft: DraftDoc) -> str:
        """Turn an approved draft into a send job.

        The idempotency key is the draft id, so approving twice — a double-clicked
        button, a retried request — produces one email.
        """
        job, _ = self.queue.enqueue(
            job_type=JobType.SEND_PITCH if draft.kind == "pitch" else JobType.SEND_REPLY,
            idempotency_key=f"send:{draft.draft_id}",
            target_id=draft.target_id,
            thread_id=draft.thread_id,
            payload={"subject": draft.subject, "body": draft.body, "draft_id": draft.draft_id},
        )
        return job.job_id

    def _style_memo(self) -> str:
        snapshot = self.repo._col("control").document("style_memo").get()
        return str((snapshot.to_dict() or {}).get("memo", "")) if snapshot.exists else ""

    def _require_target(self, job: JobDoc):
        target = self.repo.get_target(job.target_id or "")
        if target is None:
            raise RuntimeError(f"target {job.target_id} not found")
        return target

    # -- handlers: side effects --------------------------------------------
    def _handle_send_pitch(self, job: JobDoc) -> dict:
        """Send the first email to a target and open its thread."""
        self._assert_may_send()
        target = self._require_target(job)

        subject = job.payload["subject"]
        body = job.payload["body"]

        # Attach the poster if one was generated. Fetched at send time rather than held
        # in the job payload, so a re-run picks up the current poster and the job
        # documents stay small.
        attachments = self._poster_attachment(target)

        self._reserve()
        try:
            sent = self.gmail.send_new(
                to=target.email, subject=subject, body_text=body, attachments=attachments
            )
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

        draft_id = job.payload.get("draft_id")
        if draft_id:
            self.repo._col("drafts").document(draft_id).update(
                {
                    "status": DraftStatus.SENT.value,
                    "thread_id": sent.thread_id,
                    "sent_message_id": sent.message_id,
                    "updated_at": utcnow(),
                }
            )

        return {"message_id": sent.message_id, "thread_id": sent.thread_id, "dry_run": sent.dry_run}

    def _poster_attachment(self, target) -> list[tuple[str, bytes, str]]:
        """Read the stored poster back for attaching. Never fatal: no poster, no attachment."""
        if not target.poster_url:
            return []
        try:
            from google.cloud import storage

            settings = get_settings()
            client = storage.Client(project=settings.google_cloud_project)
            blob = client.bucket(settings.poster_bucket).blob(f"posters/{target.target_id}.png")
            return [("beatid-poster.png", blob.download_as_bytes(), "image/png")]
        except Exception as exc:
            log.warning("could not attach poster", extra={"error": str(exc)[:200]})
            return []

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

    async def _handle_send_follow_up(self, job: JobDoc) -> dict:
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

        # Follow-ups are queued at pitch time with EMPTY bodies, so that a nudge is not
        # written a week before it is sent and cannot go out stale. As written, that
        # also meant it would have gone out blank. The body is drafted here, when the
        # job actually runs, against the thread as it stands now.
        subject = job.payload.get("subject") or ""
        body = job.payload.get("body") or ""
        if not body.strip():
            subject, body = await self._draft_follow_up(target, job, thread)
        if not body.strip():
            raise RuntimeError("no follow-up body could be drafted; refusing to send blank")

        self._reserve()
        try:
            sent = self.gmail.send_reply(
                to=target.email,
                subject=subject,
                body_text=body,
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

    async def _draft_follow_up(self, target, job: JobDoc, thread) -> tuple[str, str]:
        """Draft a nudge for a thread that has gone quiet.

        Short, and deliberately not a re-pitch: someone who ignored the first email will
        not read a longer second one.
        """
        from google.adk.agents import LlmAgent

        from greenroom.agents.runtime import run_agent
        from greenroom.models import GEMINI_MODEL

        day = job.payload.get("day", 3)
        brand = self.config.brand
        subject = (thread.subject if thread else "") or "Following up"
        final = "This is the last follow-up before the thread is closed." if day >= 7 else ""

        agent = LlmAgent(
            name="follow_up_writer",
            model=GEMINI_MODEL,
            description="Writes a short follow-up to an unanswered pitch. Holds no tools.",
            instruction=(
                "You write a brief follow-up to a cold email that went unanswered. "
                "Under 60 words. Do not re-pitch, do not repeat what the first email "
                "said, do not apologise for following up, and never write 'just "
                "checking in'. Add one small new reason to reply, or ask one direct "
                "question. Plain text, British English, no signature block."
            ),
        )

        body = await run_agent(
            agent,
            f"You are {brand.sender_name} at {brand.company_name}.\n"
            f"You emailed {target.organisation} {day} days ago and heard nothing.\n"
            f"Original subject: {subject}\n"
            f"What we do: {brand.pitch}\n{final}\n"
            "Write the follow-up.",
        )
        reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        return reply_subject, body.strip()

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
