"""Firestore document shapes and the enums the pipeline runs on.

Everything Greenroom durably knows lives in one of these. ADK sessions are throwaway
working memory; these documents are the system of record, which is what makes a
crashed worker safe to re-run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- enums


class TargetStatus(StrEnum):
    QUEUED = "queued"
    RESEARCHED = "researched"
    PITCHED = "pitched"
    REPLIED = "replied"
    NEGOTIATING = "negotiating"
    BOOKED = "booked"
    ESCALATED = "escalated"
    DECLINED = "declined"
    CLOSED_NO_REPLY = "closed_no_reply"


TERMINAL_STATUSES = frozenset(
    {TargetStatus.BOOKED, TargetStatus.DECLINED, TargetStatus.CLOSED_NO_REPLY}
)


class TrustMode(StrEnum):
    """How much autonomy a target has earned. Escalations ignore this and go to review."""

    REVIEW = "review"
    VETO = "veto"
    AUTOPILOT = "autopilot"


TRUST_LADDER: tuple[TrustMode, ...] = (TrustMode.REVIEW, TrustMode.VETO, TrustMode.AUTOPILOT)


class JobType(StrEnum):
    RESEARCH_TARGET = "research_target"
    DRAFT_PITCH = "draft_pitch"
    GENERATE_POSTER = "generate_poster"
    SEND_PITCH = "send_pitch"
    SEND_REPLY = "send_reply"
    SEND_FOLLOW_UP = "send_follow_up"
    BOOK_CALL = "book_call"
    CLOSE_THREAD = "close_thread"
    RENEW_WATCH = "renew_watch"


# Job types that put mail in front of a human being. These are the ones the send
# window, the daily cap and the kill switch apply to.
SENDING_JOBS = frozenset({JobType.SEND_PITCH, JobType.SEND_REPLY, JobType.SEND_FOLLOW_UP})


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"  # will be retried
    DEAD = "dead"  # out of attempts, needs a human
    CANCELLED = "cancelled"


class Intent(StrEnum):
    """Gatekeeper output. The Negotiator never sees raw email, only this plus quotes."""

    INTERESTED = "interested"
    QUESTION = "question"
    COUNTER_OFFER = "counter_offer"
    NOT_NOW = "not_now"
    DECLINE = "decline"
    OUT_OF_OFFICE = "out_of_office"
    UNRELATED = "unrelated"


class DraftStatus(StrEnum):
    """A draft's journey past a human.

    `pending` is the whole point of review mode: nothing leaves the building while a
    draft sits here.
    """

    PENDING = "pending"
    APPROVED = "approved"
    EDITED = "edited"
    REJECTED = "rejected"
    SENT = "sent"
    EXPIRED = "expired"  # a veto window elapsed and it sent itself


class DecisionKind(StrEnum):
    APPROVED = "approved"
    EDITED = "edited"
    REJECTED = "rejected"
    AUTO_SENT = "auto_sent"  # veto window expired, or autopilot


# --------------------------------------------------------------------------- docs


class FirestoreDoc(BaseModel):
    model_config = ConfigDict(extra="allow", use_enum_values=True)

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class TargetDoc(FirestoreDoc):
    """One organisation in the pipeline. Document id is Target.key."""

    target_id: str
    organisation: str
    contact_name: str = ""
    email: str
    venue_notes: str = ""
    tier: int = 3
    context: str = ""

    status: TargetStatus = TargetStatus.QUEUED
    mode: TrustMode = TrustMode.REVIEW

    # Set by the Researcher.
    research: dict | None = None
    poster_url: str | None = None

    # Trust dial bookkeeping.
    clean_approvals: int = 0

    thread_id: str | None = None
    last_status_change: datetime = Field(default_factory=utcnow)
    status_reason: str = ""


class ThreadDoc(FirestoreDoc):
    """One email conversation. Document id is the Gmail threadId.

    `gmail_thread_id` is the containment anchor: the read tools will not fetch a
    thread that is not recorded here or carrying the greenroom label.
    """

    gmail_thread_id: str
    target_id: str
    subject: str = ""
    last_message_at: datetime | None = None
    last_outbound_at: datetime | None = None
    last_inbound_at: datetime | None = None
    follow_ups_sent: int = 0
    closed: bool = False
    # Gmail's historyId high-water mark, so a push notification only fetches new mail.
    last_history_id: str | None = None


class MessageDoc(FirestoreDoc):
    """One email, inbound or outbound. Document id is the Gmail message id."""

    gmail_message_id: str
    gmail_thread_id: str
    target_id: str
    direction: str  # "inbound" | "outbound"
    from_addr: str = ""
    to_addr: str = ""
    subject: str = ""
    body_text: str = ""
    rfc822_message_id: str = ""

    # Gatekeeper verdict, present on inbound messages only.
    intent: Intent | None = None
    quarantined: bool = False
    quarantine_reason: str = ""
    injection_flags: list[str] = Field(default_factory=list)


class JobDoc(FirestoreDoc):
    """A durable, idempotent unit of side effect.

    Document id is derived from `idempotency_key`, so enqueueing the same logical
    action twice is a no-op rather than a double send. `lease_expires_at` is what makes
    a crashed worker recoverable: the job returns to the queue on its own.
    """

    job_id: str
    idempotency_key: str
    job_type: JobType
    status: JobStatus = JobStatus.QUEUED

    target_id: str | None = None
    thread_id: str | None = None
    payload: dict = Field(default_factory=dict)

    attempts: int = 0
    max_attempts: int = 5
    last_error: str = ""

    # Earliest this job may run: scheduling a follow-up, or exponential backoff.
    run_after: datetime = Field(default_factory=utcnow)

    # Worker lease. A running job whose lease has expired is reclaimable.
    leased_by: str | None = None
    lease_expires_at: datetime | None = None

    result: dict = Field(default_factory=dict)
    completed_at: datetime | None = None


class DecisionDoc(FirestoreDoc):
    """A human verdict on a draft. Feeds the trust dial and the style memo."""

    decision_id: str
    target_id: str
    thread_id: str | None = None
    job_id: str | None = None
    kind: DecisionKind
    draft_before: str = ""
    draft_after: str = ""
    diff: str = ""
    note: str = ""


class DraftDoc(FirestoreDoc):
    """An email waiting on a human, or recently past one.

    Kept as its own document rather than as a job payload so the dashboard has
    something stable to render and edit, and so the approval history survives the job
    being completed.
    """

    draft_id: str
    target_id: str
    thread_id: str | None = None
    kind: str = "pitch"  # "pitch" | "reply" | "follow_up"

    subject: str = ""
    body: str = ""
    # What the agent originally wrote, preserved even after a human edits `body`.
    original_subject: str = ""
    original_body: str = ""

    status: DraftStatus = DraftStatus.PENDING
    mode_at_draft: TrustMode = TrustMode.REVIEW
    copy_problems: list[str] = Field(default_factory=list)

    hook_used: str = ""
    reasoning: str = ""

    # Set when mode is veto: the moment this sends itself unless stopped.
    auto_send_at: datetime | None = None
    # Escalations are always review, whatever the target's mode says.
    is_escalation: bool = False
    escalation_reason: str = ""
    policy_rule: str = ""

    resolved_at: datetime | None = None
    sent_message_id: str | None = None


class EventDoc(FirestoreDoc):
    """An append-only audit line. Everything the agent did, in order."""

    event_id: str
    kind: str
    target_id: str | None = None
    thread_id: str | None = None
    job_id: str | None = None
    detail: dict = Field(default_factory=dict)
