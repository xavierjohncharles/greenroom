from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def real_config_dir() -> Path:
    """The repo's actual config/ directory — these must always be valid."""
    return REPO_ROOT / "config"


@pytest.fixture
def tmp_config(tmp_path: Path, real_config_dir: Path) -> Path:
    """A writable copy of the real config, for mutating into invalid states."""
    import shutil

    dest = tmp_path / "config"
    shutil.copytree(real_config_dir, dest)
    return dest


@pytest.fixture(scope="session")
def firestore_db():
    """Real Firestore client, or skip. Integration tests run against the live database
    in a throwaway namespace — a fake queue would only prove the fake works."""
    from greenroom.settings import get_settings

    if not get_settings().google_cloud_project:
        pytest.skip("GOOGLE_CLOUD_PROJECT not set; skipping Firestore integration tests")
    try:
        from greenroom.state.db import get_db

        db = get_db()
        db.collection("control").document("health").get()  # fail fast on bad credentials
        return db
    except Exception as exc:  # noqa: BLE001 - we want any auth failure to skip, not error
        pytest.skip(f"Firestore unreachable: {exc}")


@pytest.fixture
def namespace() -> str:
    """A unique collection prefix per test, so nothing touches the real pipeline."""
    import uuid

    return f"test_{uuid.uuid4().hex[:10]}_"


@pytest.fixture
def queue(firestore_db, namespace):
    from greenroom.jobs.queue import JobQueue

    q = JobQueue(firestore_db, namespace=namespace, lease_seconds=2)
    yield q
    _purge(firestore_db, namespace)


@pytest.fixture
def repo(firestore_db, namespace):
    from greenroom.state.repo import Repo

    r = Repo(firestore_db, namespace=namespace)
    yield r
    _purge(firestore_db, namespace)


def _purge(db, namespace: str) -> None:
    """Delete every namespaced collection this test created."""
    for name in ("jobs", "targets", "threads", "messages", "decisions", "events", "control"):
        for doc in db.collection(f"{namespace}{name}").limit(500).stream():
            doc.reference.delete()
