"""What happens to an inbound reply, in order.

    notification → fetch (scoped) → dedupe → Gatekeeper → quarantine? → Negotiator
                                                                     → policy verdict
                                                                     → draft or escalate

The ordering is the security design. The Gatekeeper runs before anything else sees the
message, and if it quarantines, the pipeline stops there — the Negotiator is never
invoked, so attacker-controlled text never reaches an agent that drafts replies.

Nothing here sends. Everything that leaves the building leaves as a job, executed by the
Scheduler, which re-checks the recipient against targets.csv.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from greenroom.config import get_config
from greenroom.obs import get_logger, set_log_context
from greenroom.settings import get_settings
from greenroom.state.models import (
    DraftDoc,
    DraftStatus,
    Intent,
    MessageDoc,
    TargetStatus,
    TrustMode,
    utcnow,
)

log = get_logger(__name__)

HISTORY_DOC = "gmail_history"

# Intents that need a drafted reply. A decline or an autoreply does not.
NEEDS_REPLY = {Intent.INTERESTED.value, Intent.QUESTION.value, Intent.COUNTER_OFFER.value}

# How an inbound intent moves the target through the pipeline.
_STATUS_FOR_INTENT = {
    Intent.INTERESTED.value: TargetStatus.REPLIED,
    Intent.QUESTION.value: TargetStatus.NEGOTIATING,
    Intent.COUNTER_OFFER.value: TargetStatus.NEGOTIATING,
    Intent.NOT_NOW.value: TargetStatus.DECLINED,
    Intent.DECLINE.value: TargetStatus.DECLINED,
}


def _deps():
    from greenroom.web.deps import get_queue, get_repo, get_scheduler

    return get_repo(), get_queue(), get_scheduler()


def get_last_history_id(repo) -> str:
    snapshot = repo._col("control").document(HISTORY_DOC).get()
    return str((snapshot.to_dict() or {}).get("history_id", "")) if snapshot.exists else ""


def set_last_history_id(repo, history_id: str) -> None:
    repo._col("control").document(HISTORY_DOC).set(
        {"history_id": str(history_id), "updated_at": utcnow()}, merge=True
    )


async def process_history(*, history_id: str, pubsub_message_id: str = "") -> dict[str, Any]:
    """Fetch and process everything new since our last high-water mark.

    Uses the *stored* history id rather than the one in the notification: notifications
    can arrive out of order, and replaying from our own mark is idempotent while
    replaying from theirs can skip messages.
    """
    repo, _, scheduler = _deps()
    settings = get_settings()
    gmail = scheduler.gmail

    start = get_last_history_id(repo)
    if not start:
        # First notification after a watch is registered: nothing to replay from, so
        # adopt this point as the mark. Missing one message here is preferable to
        # re-reading the entire mailbox history.
        set_last_history_id(repo, history_id)
        log.info("history baseline set", extra={"history_id": history_id})
        return {"processed": 0, "reason": "baseline set"}

    owned = repo.owned_thread_ids()
    records = gmail.history_since(start)

    seen: set[str] = set()
    processed = quarantined = skipped = 0

    for record in records:
        for added in record.get("messagesAdded", []) or []:
            message = added.get("message", {})
            message_id, thread_id = message.get("id"), message.get("threadId")
            if not message_id or message_id in seen:
                continue
            seen.add(message_id)

            if thread_id not in owned:
                # Containment: a message in a thread Greenroom did not create is never
                # fetched, never read, never passed to an agent.
                log.info("ignoring message outside our threads", extra={"thread_id": thread_id})
                skipped += 1
                continue

            outcome = await process_message(message_id=message_id, thread_id=thread_id, owned=owned)
            if outcome == "quarantined":
                quarantined += 1
            elif outcome == "processed":
                processed += 1
            else:
                skipped += 1

    set_last_history_id(repo, history_id)
    log.info(
        "inbound history processed",
        extra={
            "processed": processed,
            "quarantined": quarantined,
            "skipped": skipped,
            "pubsub_id": pubsub_message_id,
            "mailbox": settings.agent_mailbox,
        },
    )
    return {"processed": processed, "quarantined": quarantined, "skipped": skipped}


async def process_message(*, message_id: str, thread_id: str, owned: frozenset[str]) -> str:
    """Screen, store and route one inbound message. Returns what happened to it."""
    from greenroom.agents.gatekeeper import screen

    repo, _queue, scheduler = _deps()
    settings = get_settings()

    # Dedupe first: Pub/Sub is at-least-once, and running the Gatekeeper twice on the
    # same message would be wasteful; drafting twice would be worse.
    if repo._col("messages").document(message_id).get().exists:
        log.info("duplicate inbound message ignored", extra={"gmail_message_id": message_id})
        return "duplicate"

    thread = repo.get_thread(thread_id)
    if thread is None:
        return "skipped"

    target = repo.get_target(thread.target_id)
    if target is None:
        return "skipped"

    set_log_context(target_id=target.target_id, thread_id=thread_id)

    messages = scheduler.gmail.get_thread(thread_id, owned_thread_ids=owned)
    inbound_msg = next((m for m in messages if m.message_id == message_id), None)
    if inbound_msg is None:
        return "skipped"

    # Our own sends land in our own threads and would otherwise be screened as if they
    # were replies.
    if settings.agent_mailbox.lower() in inbound_msg.from_addr.lower():
        return "skipped"

    verdict = await screen(
        subject=inbound_msg.subject, sender=inbound_msg.from_addr, body=inbound_msg.body_text
    )

    repo._col("messages").document(message_id).set(
        MessageDoc(
            gmail_message_id=message_id,
            gmail_thread_id=thread_id,
            target_id=target.target_id,
            direction="inbound",
            from_addr=inbound_msg.from_addr,
            to_addr=inbound_msg.to_addr,
            subject=inbound_msg.subject,
            body_text=inbound_msg.body_text,
            rfc822_message_id=inbound_msg.rfc822_message_id,
            intent=verdict.intent,
            quarantined=verdict.is_injection,
            quarantine_reason=verdict.quarantine_reason,
            injection_flags=verdict.injection_flags,
        ).model_dump()
    )
    repo._col("threads").document(thread_id).update(
        {"last_inbound_at": utcnow(), "last_message_at": utcnow(), "updated_at": utcnow()}
    )

    # ---- quarantine: the pipeline stops here -----------------------------
    if verdict.is_injection:
        scheduler.gmail.add_label(thread_id, settings.label_quarantine)
        repo.append_event(
            kind="quarantined",
            target_id=target.target_id,
            thread_id=thread_id,
            detail={"flags": verdict.injection_flags, "reason": verdict.quarantine_reason},
        )
        log.warning(
            "inbound quarantined",
            extra={"flags": verdict.injection_flags, "reason": verdict.quarantine_reason},
        )
        # Escalate the target so a human is told, but never draft a reply to an attack.
        if TargetStatus(target.status) not in {
            TargetStatus.BOOKED,
            TargetStatus.DECLINED,
            TargetStatus.CLOSED_NO_REPLY,
        }:
            repo.set_status(
                target.target_id,
                TargetStatus.ESCALATED,
                reason=f"quarantined inbound: {verdict.quarantine_reason}",
            )
        return "quarantined"

    repo.append_event(
        kind="inbound_classified",
        target_id=target.target_id,
        thread_id=thread_id,
        detail={"intent": verdict.intent, "summary": verdict.summary[:400]},
    )

    # ---- route -----------------------------------------------------------
    next_status = _STATUS_FOR_INTENT.get(verdict.intent)
    if next_status is not None:
        try:
            repo.set_status(target.target_id, next_status, reason=f"reply: {verdict.intent}")
        except Exception as exc:
            # An illegal transition is a real signal, not a reason to drop the reply.
            log.warning("could not advance status", extra={"error": str(exc)})

    if verdict.intent not in NEEDS_REPLY:
        return "processed"

    await draft_reply(
        target_id=target.target_id,
        thread_id=thread_id,
        verdict=verdict,
        in_reply_to=inbound_msg.rfc822_message_id,
        subject=inbound_msg.subject,
    )
    return "processed"


async def draft_reply(
    *, target_id: str, thread_id: str, verdict: Any, in_reply_to: str, subject: str
) -> DraftDoc:
    """Run the Negotiator and create a draft — pending, escalated, or auto-sending."""
    from greenroom.agents.negotiator import negotiate

    repo, _, scheduler = _deps()
    config = get_config()
    target = repo.get_target(target_id)

    slots: list[str] = []
    try:
        slots = [
            s.to_human(config.policy.meetings.timezone) for s in scheduler.calendar.propose_slots()
        ]
    except Exception as exc:
        # No slots is a survivable degradation: the Negotiator is told to invent none.
        log.warning("could not read free/busy", extra={"error": str(exc)})

    outcome = await negotiate(
        target=target,
        verdict=verdict,
        config=config,
        slots=slots,
        decisions=repo.recent_decisions(limit=10),
    )

    # Escalations are always review, whatever autonomy this target has earned.
    mode = TrustMode.REVIEW if outcome.should_escalate else TrustMode(target.mode)

    draft = repo.create_draft(
        DraftDoc(
            draft_id=uuid.uuid4().hex,
            target_id=target_id,
            thread_id=thread_id,
            kind="reply",
            subject=subject if subject.lower().startswith("re:") else f"Re: {subject}",
            body=outcome.draft.body,
            original_subject=subject,
            original_body=outcome.draft.body,
            mode_at_draft=mode,
            reasoning=outcome.draft.reasoning,
            is_escalation=outcome.should_escalate,
            escalation_reason=outcome.escalation_reason,
            policy_rule=outcome.policy_rule,
            auto_send_at=(
                datetime.now(UTC) + timedelta(minutes=config.policy.operations.veto_window_minutes)
                if mode == TrustMode.VETO
                else None
            ),
        )
    )

    if outcome.should_escalate:
        scheduler.gmail.add_label(thread_id, get_settings().label_escalated)
        repo.set_status(
            target_id, TargetStatus.ESCALATED, reason=f"outside policy: {outcome.policy_rule}"
        )
        repo.append_event(
            kind="escalated",
            target_id=target_id,
            thread_id=thread_id,
            detail={"rule": outcome.policy_rule, "reason": outcome.escalation_reason},
        )
    elif mode == TrustMode.AUTOPILOT:
        # The reply is inside policy and this target has earned immediate sending.
        repo._col("drafts").document(draft.draft_id).update(
            {"status": DraftStatus.APPROVED.value, "updated_at": utcnow()}
        )
        draft.thread_id = thread_id
        _queue_reply(draft, in_reply_to=in_reply_to)

    return draft


def _queue_reply(draft: DraftDoc, *, in_reply_to: str) -> None:
    from greenroom.state.models import JobType

    _, queue, _ = _deps()
    queue.enqueue(
        job_type=JobType.SEND_REPLY,
        idempotency_key=f"send:{draft.draft_id}",
        target_id=draft.target_id,
        thread_id=draft.thread_id,
        payload={
            "subject": draft.subject,
            "body": draft.body,
            "in_reply_to": in_reply_to,
            "draft_id": draft.draft_id,
        },
    )
