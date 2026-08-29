"""Firestore repository: targets, threads, decisions, events, and the control plane.

The control plane is three small things that together decide whether a send may happen
at all — the kill switch, the daily counter, and the send window. They live in
Firestore rather than in config so they can be changed while the agent is running,
which is the entire point of a kill switch.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from greenroom.config import Target
from greenroom.obs import get_logger
from greenroom.state.machine import assert_transition
from greenroom.state.models import (
    DecisionDoc,
    DecisionKind,
    EventDoc,
    TargetDoc,
    TargetStatus,
    ThreadDoc,
    TrustMode,
    utcnow,
)

log = get_logger(__name__)

PAUSE_DOC = "pause"
COUNTER_PREFIX = "sends-"


@dataclass(frozen=True)
class SendGate:
    """Why a send was or was not allowed. Rendered verbatim on the dashboard."""

    allowed: bool
    reason: str = ""
    sends_today: int = 0
    cap: int = 0


class Repo:
    def __init__(self, db: Any, *, namespace: str = "") -> None:
        self.db = db
        self.namespace = namespace

    def _col(self, name: str):
        return self.db.collection(f"{self.namespace}{name}" if self.namespace else name)

    # -- targets -----------------------------------------------------------
    def upsert_target(
        self, target: Target, *, default_mode: TrustMode = TrustMode.REVIEW
    ) -> TargetDoc:
        """Create a target from a config row, or leave an existing one alone.

        Deliberately non-destructive: re-running the sync after editing targets.csv
        must not reset the status or trust level of a target already in flight.
        """
        doc_ref = self._col("targets").document(target.key)
        snapshot = doc_ref.get()
        if snapshot.exists:
            existing = TargetDoc.model_validate(snapshot.to_dict())
            # Refresh only the fields that come from the CSV.
            doc_ref.update(
                {
                    "organisation": target.organisation,
                    "contact_name": target.contact_name,
                    "venue_notes": target.venue_notes,
                    "tier": target.tier,
                    "context": target.context,
                    "updated_at": utcnow(),
                }
            )
            return existing

        doc = TargetDoc(
            target_id=target.key,
            organisation=target.organisation,
            contact_name=target.contact_name,
            email=target.email,
            venue_notes=target.venue_notes,
            tier=target.tier,
            context=target.context,
            mode=default_mode,
        )
        doc_ref.set(doc.model_dump())
        log.info("target created", extra={"target_id": target.key, "org": target.organisation})
        return doc

    def get_target(self, target_id: str) -> TargetDoc | None:
        snapshot = self._col("targets").document(target_id).get()
        return TargetDoc.model_validate(snapshot.to_dict()) if snapshot.exists else None

    def list_targets(
        self, *, status: TargetStatus | None = None, limit: int = 500
    ) -> list[TargetDoc]:
        from google.cloud import firestore

        query = self._col("targets")
        if status is not None:
            query = query.where(filter=firestore.FieldFilter("status", "==", str(status)))
        return [TargetDoc.model_validate(s.to_dict()) for s in query.limit(limit).stream()]

    def set_status(self, target_id: str, nxt: TargetStatus, *, reason: str = "") -> TargetDoc:
        """Move a target, refusing any transition the state machine does not allow."""
        doc_ref = self._col("targets").document(target_id)
        snapshot = doc_ref.get()
        if not snapshot.exists:
            raise KeyError(f"target {target_id} not found")

        current = TargetDoc.model_validate(snapshot.to_dict())
        assert_transition(TargetStatus(current.status), nxt)

        doc_ref.update(
            {
                "status": str(nxt),
                "status_reason": reason,
                "last_status_change": utcnow(),
                "updated_at": utcnow(),
            }
        )
        log.info(
            "target status changed",
            extra={"target_id": target_id, "from": str(current.status), "to": str(nxt)},
        )
        self.append_event(
            kind="status_change", target_id=target_id, detail={"to": str(nxt), "reason": reason}
        )
        return TargetDoc.model_validate(doc_ref.get().to_dict())

    def set_mode(self, target_id: str, mode: TrustMode, *, reason: str = "") -> None:
        self._col("targets").document(target_id).update({"mode": str(mode), "updated_at": utcnow()})
        log.info("trust mode changed", extra={"target_id": target_id, "mode": str(mode)})
        self.append_event(
            kind="mode_change", target_id=target_id, detail={"mode": str(mode), "reason": reason}
        )

    # -- threads -----------------------------------------------------------
    def create_thread(self, thread: ThreadDoc) -> ThreadDoc:
        self._col("threads").document(thread.gmail_thread_id).set(thread.model_dump())
        return thread

    def get_thread(self, thread_id: str) -> ThreadDoc | None:
        snapshot = self._col("threads").document(thread_id).get()
        return ThreadDoc.model_validate(snapshot.to_dict()) if snapshot.exists else None

    def owned_thread_ids(self) -> frozenset[str]:
        """The containment anchor: every thread Greenroom created.

        The Gmail read tools take this set and refuse anything outside it.
        """
        return frozenset(s.id for s in self._col("threads").stream())

    # -- decisions ---------------------------------------------------------
    def record_decision(
        self,
        *,
        target_id: str,
        kind: DecisionKind,
        thread_id: str | None = None,
        job_id: str | None = None,
        draft_before: str = "",
        draft_after: str = "",
        diff: str = "",
        note: str = "",
    ) -> DecisionDoc:
        decision = DecisionDoc(
            decision_id=uuid.uuid4().hex,
            target_id=target_id,
            thread_id=thread_id,
            job_id=job_id,
            kind=kind,
            draft_before=draft_before,
            draft_after=draft_after,
            diff=diff,
            note=note,
        )
        self._col("decisions").document(decision.decision_id).set(decision.model_dump())
        log.info("decision recorded", extra={"target_id": target_id, "kind": str(kind)})
        return decision

    def recent_decisions(self, *, limit: int = 20) -> list[DecisionDoc]:
        from google.cloud import firestore

        query = (
            self._col("decisions")
            .order_by("created_at", direction=firestore.Query.DESCENDING)
            .limit(limit)
        )
        return [DecisionDoc.model_validate(s.to_dict()) for s in query.stream()]

    # -- events ------------------------------------------------------------
    def append_event(
        self,
        *,
        kind: str,
        target_id: str | None = None,
        thread_id: str | None = None,
        job_id: str | None = None,
        detail: dict | None = None,
    ) -> None:
        event = EventDoc(
            event_id=uuid.uuid4().hex,
            kind=kind,
            target_id=target_id,
            thread_id=thread_id,
            job_id=job_id,
            detail=detail or {},
        )
        self._col("events").document(event.event_id).set(event.model_dump())

    # -- control plane -----------------------------------------------------
    def is_paused(self) -> tuple[bool, str]:
        snapshot = self._col("control").document(PAUSE_DOC).get()
        if not snapshot.exists:
            return False, ""
        data = snapshot.to_dict() or {}
        return bool(data.get("paused")), str(data.get("reason", ""))

    def set_paused(self, paused: bool, *, reason: str = "") -> None:
        """The kill switch. Checked before every send, with no way to bypass it."""
        self._col("control").document(PAUSE_DOC).set(
            {"paused": paused, "reason": reason, "updated_at": utcnow()}
        )
        log.warning("kill switch toggled", extra={"paused": paused, "reason": reason})
        self.append_event(kind="pause_toggled", detail={"paused": paused, "reason": reason})

    def sends_today(self, *, day: date | None = None, tz: str = "Europe/London") -> int:
        day = day or datetime.now(ZoneInfo(tz)).date()
        snapshot = self._col("control").document(f"{COUNTER_PREFIX}{day.isoformat()}").get()
        return int((snapshot.to_dict() or {}).get("count", 0)) if snapshot.exists else 0

    def reserve_send_slot(
        self, *, cap: int, day: date | None = None, tz: str = "Europe/London"
    ) -> bool:
        """Atomically take one of today's send slots. False if the cap is reached.

        The slot is reserved *before* the send, not after. Under concurrency, counting
        afterwards lets two instances both pass the check and both send; reserving
        first means the worst case is a burnt slot on a failed send, which
        `release_send_slot` gives back anyway. Erring toward under-sending is the right
        direction for a cap whose purpose is not embarrassing a real company.
        """
        from google.cloud import firestore

        day = day or datetime.now(ZoneInfo(tz)).date()
        doc_ref = self._col("control").document(f"{COUNTER_PREFIX}{day.isoformat()}")

        @firestore.transactional
        def _reserve(transaction) -> bool:
            snapshot = doc_ref.get(transaction=transaction)
            count = int((snapshot.to_dict() or {}).get("count", 0)) if snapshot.exists else 0
            if count >= cap:
                return False
            transaction.set(
                doc_ref,
                {"count": count + 1, "day": day.isoformat(), "updated_at": utcnow()},
                merge=True,
            )
            return True

        return _reserve(self.db.transaction())

    def release_send_slot(self, *, day: date | None = None, tz: str = "Europe/London") -> None:
        """Give back a slot reserved for a send that then failed."""
        from google.cloud import firestore

        day = day or datetime.now(ZoneInfo(tz)).date()
        doc_ref = self._col("control").document(f"{COUNTER_PREFIX}{day.isoformat()}")

        @firestore.transactional
        def _release(transaction) -> None:
            snapshot = doc_ref.get(transaction=transaction)
            count = int((snapshot.to_dict() or {}).get("count", 0)) if snapshot.exists else 0
            transaction.set(
                doc_ref, {"count": max(count - 1, 0), "updated_at": utcnow()}, merge=True
            )

        _release(self.db.transaction())


# --------------------------------------------------------------------------- window


def in_send_window(now: datetime, window: Any) -> tuple[bool, str]:
    """Is `now` inside the configured send window? Pure, so it is trivially testable.

    Kept out of Repo because it needs no database and is the rule most likely to be
    questioned in a demo ("what stops it emailing at 3am?").
    """
    local = now.astimezone(ZoneInfo(window.timezone))
    if local.weekday() not in window.weekdays:
        return False, f"{local:%A} is outside the send window (weekdays only)"
    if not (window.start_hour <= local.hour < window.end_hour):
        return (
            False,
            f"{local:%H:%M} {window.timezone} is outside "
            f"{window.start_hour:02d}:00-{window.end_hour:02d}:00",
        )
    return True, ""


def evaluate_send_gate(
    repo: Repo, *, policy: Any, now: datetime | None = None, dry_run: bool = False
) -> SendGate:
    """The complete set of conditions that must hold before any mail goes out.

    Order matters: the kill switch is checked first so that flipping it stops
    everything immediately, regardless of window or cap.
    """
    now = now or datetime.now(UTC)
    ops = policy.operations

    paused, reason = repo.is_paused()
    if paused:
        return SendGate(False, f"globally paused: {reason or 'no reason given'}")

    in_window, why = in_send_window(now, ops.send_window)
    if not in_window:
        return SendGate(False, why)

    sent = repo.sends_today(tz=ops.send_window.timezone)
    if sent >= ops.max_sends_per_day:
        return SendGate(
            False,
            f"daily cap reached ({sent}/{ops.max_sends_per_day})",
            sent,
            ops.max_sends_per_day,
        )

    return SendGate(True, "dry-run" if dry_run else "", sent, ops.max_sends_per_day)
