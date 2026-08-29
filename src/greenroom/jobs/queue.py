"""The durable job queue. Every side effect in Greenroom is one of these.

https://docs.cloud.google.com/firestore/docs/manage-data/transactions

Three properties, and each one exists because of a specific way this could go wrong at
2am on a live inbox:

  * **Idempotent enqueue.** The document id is derived from the idempotency key, so
    enqueueing the same logical action twice is a no-op. Pub/Sub is at-least-once, and
    a redelivered inbound notification must not produce a second email.

  * **Leased claiming.** Claiming is a Firestore transaction that stamps a worker id
    and a lease expiry. Two Cloud Run instances cannot claim the same job.

  * **Self-healing after a crash.** A worker that dies mid-job leaves a `running` job
    with an expiring lease. Once it lapses the job is claimable again, attempts is
    already incremented, and it retries with backoff until `max_attempts`, then goes
    `dead` for a human rather than looping forever.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from greenroom.obs import get_logger
from greenroom.state.models import JobDoc, JobStatus, JobType, utcnow

log = get_logger(__name__)

# How long a worker may hold a job before it is considered crashed. Comfortably longer
# than the slowest job (a Gemini research call plus an Imagen render) and comfortably
# shorter than the hourly tick, so a crash self-heals within one tick.
DEFAULT_LEASE_SECONDS = 300

# Retry backoff, in seconds, indexed by attempt number. Deliberately short at first —
# most failures here are transient API blips — then long enough to survive an outage.
BACKOFF_SECONDS = (30, 120, 600, 1800)


def job_id_for(idempotency_key: str) -> str:
    """Deterministic document id for an idempotency key.

    Hashed rather than used directly because keys contain characters Firestore does not
    allow in a document id (slashes, most obviously) and can exceed the length limit.
    """
    return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:40]


def backoff_for(attempt: int) -> timedelta:
    """Delay before the next attempt. Caps at the last entry rather than growing."""
    idx = min(max(attempt - 1, 0), len(BACKOFF_SECONDS) - 1)
    return timedelta(seconds=BACKOFF_SECONDS[idx])


@dataclass(frozen=True)
class ClaimResult:
    job: JobDoc
    worker_id: str


class JobQueue:
    """Firestore-backed job queue.

    `namespace` prefixes collection names so integration tests can run against the real
    database without touching the real pipeline.
    """

    def __init__(self, db: Any, *, namespace: str = "", lease_seconds: int = DEFAULT_LEASE_SECONDS):
        self.db = db
        self.namespace = namespace
        self.lease_seconds = lease_seconds

    @property
    def collection_name(self) -> str:
        return f"{self.namespace}jobs" if self.namespace else "jobs"

    def _col(self):
        return self.db.collection(self.collection_name)

    # -- enqueue -----------------------------------------------------------
    def enqueue(
        self,
        *,
        job_type: JobType,
        idempotency_key: str,
        payload: dict | None = None,
        target_id: str | None = None,
        thread_id: str | None = None,
        run_after: datetime | None = None,
        max_attempts: int = 5,
    ) -> tuple[JobDoc, bool]:
        """Create a job, or return the existing one. Returns (job, created).

        Uses a `create` rather than a `set`, so a concurrent duplicate loses the race
        cleanly instead of overwriting a job that may already be running.
        """
        from google.api_core import exceptions as gcp_exc

        job_id = job_id_for(idempotency_key)
        doc_ref = self._col().document(job_id)

        job = JobDoc(
            job_id=job_id,
            idempotency_key=idempotency_key,
            job_type=job_type,
            payload=payload or {},
            target_id=target_id,
            thread_id=thread_id,
            run_after=run_after or utcnow(),
            max_attempts=max_attempts,
        )

        try:
            doc_ref.create(job.model_dump())
        except gcp_exc.AlreadyExists:
            existing = doc_ref.get()
            log.info(
                "job already enqueued, skipping duplicate",
                extra={
                    "job_id": job_id,
                    "job_type": str(job_type),
                    "idempotency_key": idempotency_key,
                },
            )
            return JobDoc.model_validate(existing.to_dict()), False

        log.info(
            "job enqueued",
            extra={"job_id": job_id, "job_type": str(job_type), "target_id": target_id},
        )
        return job, True

    # -- claim -------------------------------------------------------------
    def claim_next(
        self,
        *,
        worker_id: str | None = None,
        job_types: list[JobType] | None = None,
        limit: int = 1,
        now: datetime | None = None,
    ) -> list[ClaimResult]:
        """Atomically claim up to `limit` runnable jobs.

        Runnable means: queued or failed, `run_after` has passed — or running with an
        expired lease, which is how a crashed worker's job comes back.
        """
        from google.cloud import firestore

        now = now or utcnow()
        worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        claimed: list[ClaimResult] = []

        # Fetch a generous candidate window: the transaction below re-checks every
        # condition, so a stale read here costs a retry, never a double-claim.
        query = self._col().where(
            filter=firestore.FieldFilter(
                "status",
                "in",
                [JobStatus.QUEUED.value, JobStatus.FAILED.value, JobStatus.RUNNING.value],
            )
        )
        if job_types:
            query = query.where(
                filter=firestore.FieldFilter("job_type", "in", [str(t) for t in job_types])
            )

        for snapshot in query.limit(limit * 5).stream():
            if len(claimed) >= limit:
                break
            job = self._try_claim(snapshot.reference, worker_id=worker_id, now=now)
            if job is not None:
                claimed.append(ClaimResult(job=job, worker_id=worker_id))

        return claimed

    def _try_claim(self, doc_ref, *, worker_id: str, now: datetime) -> JobDoc | None:
        from google.cloud import firestore

        lease_seconds = self.lease_seconds

        @firestore.transactional
        def _claim(transaction) -> dict | None:
            snapshot = doc_ref.get(transaction=transaction)
            if not snapshot.exists:
                return None
            data = snapshot.to_dict()

            if not _is_runnable(data, now):
                return None

            update = {
                "status": JobStatus.RUNNING.value,
                "leased_by": worker_id,
                "lease_expires_at": now + timedelta(seconds=lease_seconds),
                "attempts": int(data.get("attempts", 0)) + 1,
                "updated_at": now,
            }
            transaction.update(doc_ref, update)
            return {**data, **update}

        claimed = _claim(self.db.transaction())
        if claimed is None:
            return None

        job = JobDoc.model_validate(claimed)
        log.info(
            "job claimed",
            extra={
                "job_id": job.job_id,
                "job_type": str(job.job_type),
                "attempt": job.attempts,
                "worker": worker_id,
            },
        )
        return job

    # -- finish ------------------------------------------------------------
    def complete(self, job_id: str, result: dict | None = None) -> None:
        self._col().document(job_id).update(
            {
                "status": JobStatus.DONE.value,
                "result": result or {},
                "leased_by": None,
                "lease_expires_at": None,
                "last_error": "",
                "completed_at": utcnow(),
                "updated_at": utcnow(),
            }
        )
        log.info("job done", extra={"job_id": job_id})

    def fail(self, job_id: str, error: str, *, now: datetime | None = None) -> JobStatus:
        """Record a failure. Retries with backoff, or goes `dead` when out of attempts.

        `dead` is a deliberate state rather than an endless retry: a job that has failed
        five times needs a human, and quietly retrying it forever would hide that.
        """
        now = now or utcnow()
        doc_ref = self._col().document(job_id)
        snapshot = doc_ref.get()
        if not snapshot.exists:
            raise KeyError(f"job {job_id} not found")

        data = snapshot.to_dict()
        attempts = int(data.get("attempts", 0))
        max_attempts = int(data.get("max_attempts", 5))
        exhausted = attempts >= max_attempts
        status = JobStatus.DEAD if exhausted else JobStatus.FAILED

        doc_ref.update(
            {
                "status": status.value,
                "last_error": error[:2000],
                "leased_by": None,
                "lease_expires_at": None,
                "run_after": now + backoff_for(attempts),
                "updated_at": now,
            }
        )
        log.warning(
            "job failed",
            extra={
                "job_id": job_id,
                "attempt": attempts,
                "max_attempts": max_attempts,
                "status": status.value,
                "error": error[:200],
            },
        )
        return status

    def cancel(self, job_id: str, reason: str = "") -> None:
        self._col().document(job_id).update(
            {
                "status": JobStatus.CANCELLED.value,
                "last_error": reason,
                "leased_by": None,
                "lease_expires_at": None,
                "updated_at": utcnow(),
            }
        )

    def get(self, job_id: str) -> JobDoc | None:
        snapshot = self._col().document(job_id).get()
        return JobDoc.model_validate(snapshot.to_dict()) if snapshot.exists else None

    def list_by_status(self, status: JobStatus, *, limit: int = 100) -> list[JobDoc]:
        from google.cloud import firestore

        query = (
            self._col()
            .where(filter=firestore.FieldFilter("status", "==", status.value))
            .limit(limit)
        )
        return [JobDoc.model_validate(s.to_dict()) for s in query.stream()]


def _is_runnable(data: dict, now: datetime) -> bool:
    """Pure predicate, so the claim rules can be unit tested without Firestore."""
    status = data.get("status")
    run_after = _as_aware(data.get("run_after"))

    if status in (JobStatus.QUEUED.value, JobStatus.FAILED.value):
        return run_after is None or run_after <= now

    if status == JobStatus.RUNNING.value:
        # Only reclaimable once the previous worker's lease has lapsed.
        lease = _as_aware(data.get("lease_expires_at"))
        return lease is not None and lease <= now

    return False


def _as_aware(value: Any) -> datetime | None:
    """Firestore hands back timezone-aware datetimes; tests may hand back naive ones."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return None
