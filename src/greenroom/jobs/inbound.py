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


def _same_address(candidate: str, mailbox: str) -> bool:
    """True if a From header refers to `mailbox`. Compares the parsed address only."""
    from email.utils import parseaddr

    _name, addr = parseaddr(candidate or "")
    return bool(addr) and addr.strip().lower() == mailbox.strip().lower()


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

    try:
        records = gmail.history_since(start)
    except Exception as exc:
        # Gmail returns 404 when a startHistoryId is too old or was never valid — its
        # history window is finite, and a bogus id (ours came from a synthetic Pub/Sub
        # test message) never existed at all. Retrying cannot fix either case, so
        # Pub/Sub would redeliver until the retention window expired.
        #
        # The documented recovery is a full resync. We do the scoped equivalent: reset
        # the baseline and walk the threads we own. That also happens to be the
        # belt-and-braces path for label-scoped watches not firing on replies into an
        # existing thread, so it earns its place twice.
        if "notFound" in str(exc) or "404" in str(exc):
            log.warning(
                "gmail history unavailable, falling back to thread reconciliation",
                extra={"start_history_id": start, "error": str(exc)[:200]},
            )
            set_last_history_id(repo, history_id)
            return await reconcile_owned_threads(owned=owned)
        raise

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


async def reconcile_owned_threads(*, owned: frozenset[str] | None = None) -> dict[str, Any]:
    """Walk every thread Greenroom owns and process anything not already recorded.

    Strictly bounded by our own threads, so it can never read anything outside
    Greenroom's footprint no matter how it is triggered. Used as the recovery path when
    Gmail history is unavailable, and by the hourly tick as a safety net.
    """
    repo, _, scheduler = _deps()
    owned = owned if owned is not None else repo.owned_thread_ids()

    processed = quarantined = skipped = 0
    for thread_id in owned:
        try:
            messages = scheduler.gmail.get_thread(thread_id, owned_thread_ids=owned)
        except Exception as exc:
            log.warning(
                "could not read owned thread", extra={"thread_id": thread_id, "error": str(exc)}
            )
            continue

        for message in messages:
            if repo._col("messages").document(message.message_id).get().exists:
                continue
            outcome = await process_message(
                message_id=message.message_id, thread_id=thread_id, owned=owned
            )
            if outcome == "quarantined":
                quarantined += 1
            elif outcome == "processed":
                processed += 1
            else:
                skipped += 1

    log.info(
        "thread reconciliation complete",
        extra={"threads": len(owned), "processed": processed, "quarantined": quarantined},
    )
    return {
        "processed": processed,
        "quarantined": quarantined,
        "skipped": skipped,
        "via": "reconciliation",
    }


async def process_message(*, message_id: str, thread_id: str, owned: frozenset[str]) -> str:
    """Screen, store and route one inbound message. Returns what happened to it."""
    from greenroom.agents.gatekeeper import screen

    repo, _queue, scheduler = _deps()
    settings = get_settings()

    # Claim the message atomically. Pub/Sub is at-least-once, and this ran as a
    # check-then-write: three retried notifications arrived together, all three read
    # "not seen yet" before any of them wrote, and all three drafted a reply to the same
    # email. The read was fine; the gap between read and write was not.
    #
    # `create()` fails if the document exists, so exactly one caller wins the race. The
    # claim is written BEFORE the Gatekeeper runs, so a crash mid-screening leaves a
    # claimed-but-unprocessed message rather than a duplicate reply — the safer failure.
    from google.api_core import exceptions as gcp_exc

    message_ref = repo._col("messages").document(message_id)
    try:
        message_ref.create(
            {
                "gmail_message_id": message_id,
                "gmail_thread_id": thread_id,
                "direction": "inbound",
                "processing": True,
                "created_at": utcnow(),
                "updated_at": utcnow(),
            }
        )
    except gcp_exc.AlreadyExists:
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
    # were replies. Parsed rather than substring-matched, for two reasons: an unset
    # mailbox made `"" in from_addr` true for EVERY message, silently disabling the whole
    # inbound pipeline including quarantine; and a substring test would also match a
    # lookalike sender like "admin@beatidapp.com.evil.example".
    if not settings.agent_mailbox:
        raise RuntimeError(
            "GREENROOM_MAILBOX is not set; refusing to process inbound mail without "
            "knowing which address is our own"
        )
    if _same_address(inbound_msg.from_addr, settings.agent_mailbox):
        return "skipped"

    try:
        verdict = await screen(
            subject=inbound_msg.subject, sender=inbound_msg.from_addr, body=inbound_msg.body_text
        )
    except Exception:
        # Release the claim: a screening failure is transient and the message has not
        # been dealt with. Leaving the claim would silently drop a real reply.
        message_ref.delete()
        raise

    message_ref.set(
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
        | {"processing": False}
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

    repo.append_event(
        kind="policy_evaluated",
        target_id=target_id,
        thread_id=thread_id,
        detail={
            "inside_policy": outcome.verdict.inside,
            "rules": outcome.policy_rule or "none breached",
            "reason": outcome.escalation_reason or "inside the envelope",
            "why": outcome.draft.reasoning[:300],
        },
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
