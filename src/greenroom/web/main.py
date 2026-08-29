"""Cloud Run entrypoint: the agent runtime and the dashboard in one service.

https://docs.cloud.google.com/run/docs/quickstarts/build-and-deploy/deploy-python-service
https://docs.cloud.google.com/run/docs/container-contract   (must listen on $PORT, 0.0.0.0)

Design note — why not `adk deploy cloud_run` / `get_fast_api_app()`:
Greenroom needs /inbound (Pub/Sub push), /tick (Cloud Scheduler) and the dashboard on
the same service, which the ADK-generated app does not expose. ADK's own docs also say
the bundled web UI is not meant for production. So we own the FastAPI app and drive
agents in-process through `google.adk.runners.Runner`, which is the supported path.
Logged in BUILDLOG.md.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from greenroom import __version__
from greenroom.config import ConfigError, get_config
from greenroom.models import GEMINI_MODEL
from greenroom.obs import configure_logging, get_logger
from greenroom.settings import get_settings

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()
    # Fail fast and loudly on bad config: a container that boots with an invalid
    # policy.yaml is worse than one that refuses to boot at all.
    try:
        config = get_config()
        log.info(
            "config loaded",
            extra={
                "targets": len(config.targets.targets),
                "policy_version": config.policy.version,
                "dry_run": settings.dry_run,
            },
        )
    except ConfigError as exc:
        log.error("config failed validation", extra={"error": str(exc)})
        raise

    log.info(
        "greenroom starting",
        extra={
            "version": __version__,
            "model": GEMINI_MODEL,
            "project": settings.google_cloud_project or "(unset)",
            "location": settings.google_cloud_location,
            "dry_run": settings.dry_run,
        },
    )
    yield
    log.info("greenroom shutting down")


app = FastAPI(
    title="Greenroom",
    description="Autonomous booking-and-partnership agent for small event brands.",
    version=__version__,
    lifespan=lifespan,
)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    """Liveness + config check. Deliberately does not touch Firestore or Gemini,
    so it stays fast and cannot fail for reasons outside this container."""
    settings = get_settings()
    try:
        config = get_config()
        config_ok, config_error = True, None
    except ConfigError as exc:
        config_ok, config_error = False, str(exc)
        config = None

    return {
        "status": "ok" if config_ok else "degraded",
        "version": __version__,
        "model": GEMINI_MODEL,
        "dry_run": settings.dry_run,
        "project": settings.google_cloud_project or None,
        "location": settings.google_cloud_location,
        "config_ok": config_ok,
        "config_error": config_error,
        "targets_loaded": len(config.targets.targets) if config else 0,
    }


@app.get("/readyz")
async def readyz() -> JSONResponse:
    """Readiness: proves Firestore is actually reachable from this container."""
    from greenroom.state import ping

    try:
        return JSONResponse({"status": "ok", "firestore": ping()})
    except Exception as exc:
        log.error("firestore unreachable", extra={"error": str(exc)})
        return JSONResponse(
            {"status": "unavailable", "firestore": {"reachable": False, "error": str(exc)}},
            status_code=503,
        )


@app.get("/hello")
async def hello() -> dict[str, Any]:
    """Step 1 round-trip proof: ADK -> Gemini 3.5 Flash -> Cloud Run."""
    from greenroom.agents.hello import say_hello

    try:
        reply = await say_hello()
        return {"status": "ok", "model": GEMINI_MODEL, "reply": reply}
    except Exception as exc:
        log.error("hello agent failed", extra={"error": str(exc)})
        return JSONResponse(
            {"status": "error", "model": GEMINI_MODEL, "error": str(exc)}, status_code=502
        )


@app.get("/")
async def index() -> dict[str, str]:
    """Dashboard lands at step 4. Until then, a signpost."""
    return {
        "service": "greenroom",
        "version": __version__,
        "dashboard": "coming at build step 4",
        "health": "/healthz",
        "ready": "/readyz",
        "round_trip_proof": "/hello",
    }


if __name__ == "__main__":  # local convenience; Cloud Run uses the Dockerfile CMD
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
