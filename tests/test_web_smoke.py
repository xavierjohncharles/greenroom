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


def test_index_serves_the_dashboard():
    """`/` is the server-rendered pipeline board from step 4, not a JSON signpost."""
    with TestClient(app) as client:
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "Greenroom" in r.text


def test_reserved_log_keys_do_not_break_the_request_path():
    """Regression: extra={"created": n} raised KeyError inside Logger.makeRecord and
    returned a 500 from /admin/sync-targets. A log line must never be able to fail a
    request, so reserved keys are renamed rather than raised."""
    from greenroom.obs import get_logger

    log = get_logger("test.reserved")
    for reserved in ("created", "message", "module", "filename", "process"):
        log.info("should not raise", extra={reserved: "x"})
