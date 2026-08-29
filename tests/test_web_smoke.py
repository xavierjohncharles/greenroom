"""Smoke test: the container boots, config validates at startup, and /healthz answers.

Deliberately does not hit Gemini or Firestore — those need credentials and are proven
by /hello and /readyz against the deployed service.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from greenroom.web.main import app


def test_healthz_reports_the_mandated_model():
    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["model"] == "gemini-3.5-flash"
        assert body["config_ok"] is True
        assert body["targets_loaded"] >= 1


def test_dry_run_defaults_to_true():
    """Forgetting to set the flag must never result in real mail being sent."""
    with TestClient(app) as client:
        assert client.get("/health").json()["dry_run"] is True


def test_index_is_reachable():
    with TestClient(app) as client:
        r = client.get("/")
        assert r.status_code == 200
        assert r.json()["service"] == "greenroom"
