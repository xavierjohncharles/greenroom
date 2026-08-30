"""What happens on every tick, and once a morning.

Cloud Scheduler hits /tick hourly. The tick is deliberately a sequence of small,
independently-safe steps rather than one transaction: if the morning brief fails, the
follow-ups still go out. Each step reports its own outcome so a failure is visible in
the response and in Cloud Trace rather than silently skipped.

https://docs.cloud.google.com/scheduler/docs/creating
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from greenroom.config import get_config
from greenroom.obs import get_logger
from greenroom.state.models import DraftStatus, TargetStatus, utcnow

log = get_logger(__name__)

BRIEF_DOC = "morning_brief"
WATCH_DOC = "gmail_watch"

# Renew the Gmail watch well inside its 7-day expiry. Gmail stops delivering silently,
# so the cost of renewing too often is a wasted API call and the cost of renewing too
# late is an inbound pipeline that has quietly stopped.
WATCH_RENEW_AFTER_HOURS = 24


async def expire_veto_windows(repo, scheduler) -> dict[str, Any]:
    """Send any draft whose veto window has elapsed.

    This is the whole of `veto` mode: the human had 30 minutes to stop it and did not,
    so silence becomes consent. Escalations never reach this code — they are forced to
    review mode when they are created, so their `auto_send_at` is never set.
    """
    now = utcnow()
    sent = 0
    for draft in repo.list_drafts(status=DraftStatus.PENDING, limit=100):
        if draft.auto_send_at is None or draft.is_escalation:
            continue
        auto_at = draft.auto_send_at
        if auto_at.tzinfo is None:
            auto_at = auto_at.replace(tzinfo=UTC)
        if auto_at > now:
            continue

        repo.resolve_draft(draft.draft_id, status=DraftStatus.APPROVED)
        from greenroom.state.models import DecisionKind

        repo.record_decision(
            target_id=draft.target_id,
            thread_id=draft.thread_id,
            kind=DecisionKind.AUTO_SENT,
            draft_before=draft.original_body,
            draft_after=draft.body,
            note="veto window elapsed with no objection",
        )
        scheduler.enqueue_send_for_draft(draft)
        sent += 1
        log.info("veto window expired, sending", extra={"target_id": draft.target_id})

    return {"released": sent}


async def renew_watch_if_due(repo, scheduler, *, topic: str) -> dict[str, Any]:
    """Re-register the Gmail watch. Expires after 7 days; we renew daily."""
    snapshot = repo._col("control").document(WATCH_DOC).get()
    last = (snapshot.to_dict() or {}).get("renewed_at") if snapshot.exists else None
    if last is not None:
        last_dt = last if last.tzinfo else last.replace(tzinfo=UTC)
        age_hours = (utcnow() - last_dt).total_seconds() / 3600
        if age_hours < WATCH_RENEW_AFTER_HOURS:
            return {"renewed": False, "age_hours": round(age_hours, 1)}

    result = scheduler.gmail.start_watch(topic)
    repo._col("control").document(WATCH_DOC).set(
        {
            "renewed_at": utcnow(),
            "history_id": result.get("historyId"),
            "expiration": result.get("expiration"),
        }
    )
    log.info("gmail watch renewed", extra={"expiration": result.get("expiration")})
    return {"renewed": True, "expiration": result.get("expiration")}


def should_write_brief(repo, *, now: datetime | None = None, hour: int = 8) -> bool:
    """True once per day, at or after the brief hour in UK time.

    Checked against the stored brief rather than the clock alone, so an hourly tick
    produces one brief a day however many times it runs, and a missed 08:00 tick still
    produces a brief at 09:00 rather than skipping the day.
    """
    tz = ZoneInfo(get_config().policy.operations.send_window.timezone)
    local = (now or utcnow()).astimezone(tz)
    if local.hour < hour:
        return False

    snapshot = repo._col("control").document(BRIEF_DOC).get()
    if not snapshot.exists:
        return True
    written = (snapshot.to_dict() or {}).get("for_date")
    return written != local.date().isoformat()


def gather_brief_facts(repo) -> dict[str, Any]:
    """Everything the brief is built from. Pure reads, no model involved.

    Separated from the writing so the numbers in the brief are counted, not narrated:
    a language model asked to both tally and summarise will occasionally do neither
    accurately, and "3 threads need you" has to be true.
    """
    targets = repo.list_targets(limit=500)
    pending = repo.list_drafts(status=DraftStatus.PENDING, limit=100)
    by_id = {t.target_id: t for t in targets}

    counts: dict[str, int] = {}
    for t in targets:
        counts[str(t.status)] = counts.get(str(t.status), 0) + 1

    escalations = [d for d in pending if d.is_escalation]
    awaiting = [d for d in pending if not d.is_escalation]

    from google.cloud import firestore

    since = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    recent = [
        e.to_dict()
        for e in repo._col("events")
        .where(filter=firestore.FieldFilter("created_at", ">=", since))
        .limit(200)
        .stream()
    ]
    quarantined = [e for e in recent if e.get("kind") == "quarantined"]

    return {
        "counts": counts,
        "total_targets": len(targets),
        "escalations": [
            {
                "organisation": (
                    by_id[d.target_id].organisation if d.target_id in by_id else d.target_id
                ),
                "reason": d.escalation_reason,
                "rule": d.policy_rule,
            }
            for d in escalations
        ],
        "awaiting_approval": [
            {
                "organisation": (
                    by_id[d.target_id].organisation if d.target_id in by_id else d.target_id
                ),
                "subject": d.subject,
            }
            for d in awaiting
        ],
        "quarantined_today": len(quarantined),
        "sends_today": repo.sends_today(),
        "events_today": len(recent),
    }


async def write_morning_brief(repo) -> dict[str, Any]:
    """Generate and store the morning brief."""
    from google.adk.agents import LlmAgent

    from greenroom.agents.runtime import run_agent
    from greenroom.models import GEMINI_MODEL

    facts = gather_brief_facts(repo)

    brief_agent = LlmAgent(
        name="morning_brief",
        model=GEMINI_MODEL,
        description="Writes the daily brief for the founder.",
        instruction=(
            "You write a short morning brief for a founder whose outreach agent ran "
            "overnight. Three or four sentences, no headings, no bullet points, no "
            "greeting. Lead with anything that needs a decision from him, then what "
            "happened, then what is coming. Use only the numbers given to you and do "
            "not invent any. If nothing needs him, say so plainly and briefly — a brief "
            "that manufactures urgency to seem useful will be ignored by the second week."
        ),
    )

    summary = await run_agent(brief_agent, f"Facts:\n{facts}")
    tz = ZoneInfo(get_config().policy.operations.send_window.timezone)
    today = utcnow().astimezone(tz).date().isoformat()

    repo._col("control").document(BRIEF_DOC).set(
        {"for_date": today, "summary": summary.strip(), "facts": facts, "written_at": utcnow()}
    )
    log.info("morning brief written", extra={"for_date": today})
    return {"written": True, "for_date": today}


def load_brief(repo) -> dict[str, Any]:
    snapshot = repo._col("control").document(BRIEF_DOC).get()
    return snapshot.to_dict() or {} if snapshot.exists else {}


async def close_stale_threads(repo) -> dict[str, Any]:
    """Belt and braces for the close_thread jobs.

    Those jobs are queued at pitch time and should always fire. This catches a target
    whose close job was lost — cancelled, dead, or never queued because of a crash
    between sending and scheduling.
    """
    policy = get_config().policy.operations
    now = utcnow()
    closed = 0

    for target in repo.list_targets(status=TargetStatus.PITCHED, limit=200):
        changed = target.last_status_change
        if changed.tzinfo is None:
            changed = changed.replace(tzinfo=UTC)
        if (now - changed).days < policy.close_after_days:
            continue
        repo.set_status(
            target.target_id, TargetStatus.CLOSED_NO_REPLY, reason="no reply, closed by tick"
        )
        closed += 1

    return {"closed": closed}


async def run_tick(repo, queue, scheduler, *, limit: int = 10, topic: str = "") -> dict[str, Any]:
    """The whole tick. Each step is isolated: one failing does not stop the rest."""
    from greenroom.jobs.inbound import reconcile_owned_threads

    results: dict[str, Any] = {}

    async def step(name: str, coro):
        try:
            results[name] = await coro
        except Exception as exc:
            log.error("tick step failed", extra={"step": name, "error": str(exc)})
            results[name] = {"error": str(exc)[:300]}

    results["jobs"] = await scheduler.run_due_jobs(limit=limit)
    await step("inbound", reconcile_owned_threads())
    await step("veto", expire_veto_windows(repo, scheduler))
    await step("stale", close_stale_threads(repo))

    if topic:
        await step("watch", renew_watch_if_due(repo, scheduler, topic=topic))

    if should_write_brief(repo):
        await step("brief", write_morning_brief(repo))
        from greenroom.agents.style import regenerate

        await step("style_memo", _memo_result(repo, regenerate))

    return results


async def _memo_result(repo, regenerate) -> dict[str, Any]:
    memo = await regenerate(repo)
    return {"regenerated": memo is not None, "chars": len(memo or "")}

