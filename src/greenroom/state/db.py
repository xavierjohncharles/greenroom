"""Firestore client and collection names.

https://docs.cloud.google.com/firestore/docs/quickstart-servers
https://docs.cloud.google.com/firestore/docs/samples/firestore-data-get-dataset

Firestore must be in **Native mode**. Datastore mode is a one-way choice per project:
  gcloud firestore databases create --location=eur3 --type=firestore-native

The client is lazily constructed and cached so a container that never touches
Firestore (a local dry-run, a config-only test) does not need credentials.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from greenroom.settings import get_settings

if TYPE_CHECKING:
    from google.cloud.firestore import Client

# --- Collections ------------------------------------------------------------
# One constant per collection so a rename is a single edit and typos are import errors.
TARGETS = "targets"
THREADS = "threads"
MESSAGES = "messages"
JOBS = "jobs"
EVENTS = "events"
DECISIONS = "decisions"
CONTROL = "control"  # singleton docs: kill switch, daily counters, style memo

ALL_COLLECTIONS = (TARGETS, THREADS, MESSAGES, JOBS, EVENTS, DECISIONS, CONTROL)


@lru_cache(maxsize=1)
def get_db() -> Client:
    """Process-wide Firestore client. Uses Application Default Credentials."""
    from google.cloud import firestore

    settings = get_settings()
    if not settings.google_cloud_project:
        raise RuntimeError(
            "GOOGLE_CLOUD_PROJECT is not set — cannot open Firestore. "
            "Set it in .env for local dev, or it is injected automatically on Cloud Run."
        )
    return firestore.Client(
        project=settings.google_cloud_project,
        database=settings.firestore_database,
    )


def ping() -> dict[str, object]:
    """Cheap round-trip used by /healthz to prove Firestore is reachable.

    Reads a single control document rather than writing, so a health check can never
    mutate state or burn a write quota.
    """
    db = get_db()
    snapshot = db.collection(CONTROL).document("health").get()
    return {
        "reachable": True,
        "project": get_settings().google_cloud_project,
        "database": get_settings().firestore_database,
        "health_doc_exists": snapshot.exists,
    }
