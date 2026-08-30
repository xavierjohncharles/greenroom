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

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from greenroom import __version__
from greenroom.config import ConfigError, get_config
from greenroom.models import GEMINI_MODEL
from greenroom.obs import configure_logging, get_logger
from greenroom.settings import get_settings
from greenroom.web.dashboard import router as dashboard_router
from greenroom.web.inbound import router as inbound_router

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


@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness + config check. Deliberately does not touch Firestore or Gemini,
    so it stays fast and cannot fail for reasons outside this container.

    Named /health, not /healthz: Cloud Run's front end answers /healthz itself with a
    404 before the request ever reaches the container. Cost us ten confusing minutes.
    """
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


@app.post("/tick")
async def tick(request: Request) -> dict[str, Any]:
    """Cloud Scheduler's heartbeat: run whatever is due.

    Full tick behaviour — follow-ups, veto expiry, the morning brief — lands at step 7.
    For now it drains the job queue, which is what makes the dashboard's Approve button
    actually result in an email.
    """
    from greenroom.jobs.tick import run_tick
    from greenroom.web.auth import is_unlocked
    from greenroom.web.deps import get_queue, get_repo, get_scheduler
    from greenroom.web.inbound import PushAuthError, verify_tick_token

    # /tick runs agents and can cause mail to be sent, so it is not open. Cloud Scheduler
    # authenticates with an OIDC token; a human holding the dashboard cookie may also
    # trigger it, which is what makes "run the tick" a thing you can do on camera.
    if not is_unlocked(request):
        try:
            verify_tick_token(request)
        except PushAuthError as exc:
            log.warning("tick rejected", extra={"reason": str(exc)})
            return JSONResponse({"status": "forbidden", "reason": str(exc)}, status_code=403)

    settings = get_settings()
    topic = (
        f"projects/{settings.google_cloud_project}/topics/{settings.pubsub_topic}"
        if settings.google_cloud_project
        else ""
    )

    results = await run_tick(
        get_repo(),
        get_queue(),
        get_scheduler(),
        limit=int(request.query_params.get("limit", 10)),
        topic=topic,
    )
    log.info("tick complete", extra=results)
    return {"status": "ok", **results}


@app.post("/admin/watch")
async def register_watch(request: Request) -> dict[str, Any]:
    """Register (or renew) the Gmail push watch, scoped to the greenroom label.

    Gmail expires a watch after 7 days, so the hourly tick renews it too. Exposed here
    so it can be kicked manually during setup and on camera.
    """
    from greenroom.web.auth import is_unlocked
    from greenroom.web.deps import get_scheduler

    if not is_unlocked(request):
        return JSONResponse({"status": "forbidden"}, status_code=403)

    settings = get_settings()
    topic = f"projects/{settings.google_cloud_project}/topics/{settings.pubsub_topic}"
    gmail = get_scheduler().gmail
    gmail.ensure_labels()
    result = gmail.start_watch(topic)
    return {"status": "ok", "topic": topic, **result}


app.include_router(inbound_router)
app.include_router(dashboard_router)


if __name__ == "__main__":  # local convenience; Cloud Run uses the Dockerfile CMD
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
