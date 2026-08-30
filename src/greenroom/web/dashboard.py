"""The dashboard: pipeline board, draft approval, quarantine and settings.

Server-rendered Jinja, no JavaScript framework, no build step. It has to be readable on
a phone at a hackathon and it has to still work when the wifi is bad.

The approve/edit/reject form here is where the trust dial is actually driven: an
approval with no changes counts toward promotion, and any edit demotes immediately.
"""

from __future__ import annotations

import difflib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from greenroom.agents.schemas import ResearchDoc
from greenroom.config import get_config
from greenroom.models import GEMINI_MODEL
from greenroom.obs import get_logger
from greenroom.settings import get_settings
from greenroom.state.machine import demote, promote
from greenroom.state.models import (
    DecisionKind,
    DraftStatus,
    TargetStatus,
    TrustMode,
)
from greenroom.state.repo import in_send_window
from greenroom.web.auth import COOKIE_NAME, check_secret, is_unlocked
from greenroom.web.deps import get_queue, get_repo, get_scheduler

log = get_logger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _chrome(request: Request, nav: str) -> dict[str, Any]:
    """The bits every page needs: kill-switch banner, dry-run banner, pending count."""
    repo = get_repo()
    settings = get_settings()
    paused, reason = repo.is_paused()
    return {
        "request": request,
        "nav": nav,
        "paused": paused,
        "pause_reason": reason,
        "dry_run": settings.dry_run,
        "pending_count": len(repo.list_drafts(status=DraftStatus.PENDING, limit=50)),
    }


def _window_state() -> tuple[bool, str]:
    ops = get_config().policy.operations
    return in_send_window(datetime.now(UTC), ops.send_window)


# --------------------------------------------------------------------------- gate


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/"):
    return templates.TemplateResponse(
        request, "login.html", {**_chrome(request, "login"), "next": next, "error": None}
    )


@router.post("/login")
async def login(request: Request, secret: str = Form(...), next: str = Form("/")):
    if not check_secret(secret):
        return templates.TemplateResponse(
            request,
            "login.html",
            {**_chrome(request, "login"), "next": next, "error": "Wrong secret."},
            status_code=401,
        )
    response = RedirectResponse(url=next or "/", status_code=303)
    response.set_cookie(
        COOKIE_NAME, secret, httponly=True, samesite="lax", secure=True, max_age=60 * 60 * 24 * 7
    )
    return response


def _locked(request: Request) -> RedirectResponse | None:
    if is_unlocked(request):
        return None
    return RedirectResponse(url=f"/login?next={request.url.path}", status_code=303)


# --------------------------------------------------------------------------- board


@router.get("/", response_class=HTMLResponse)
async def board(request: Request):
    if (redirect := _locked(request)) is not None:
        return redirect

    repo, config = get_repo(), get_config()
    targets = sorted(repo.list_targets(), key=lambda t: (t.tier, t.organisation))
    counts = [
        (status.value, sum(1 for t in targets if t.status == status.value))
        for status in TargetStatus
    ]

    pending = repo.list_drafts(status=DraftStatus.PENDING, limit=50)
    by_id = {t.target_id: t for t in targets}
    # Jinja resolves dot access on dicts, so a plain merged dict is enough here and
    # avoids inventing a view model for one template.
    enriched = [
        {
            **draft.model_dump(),
            "organisation": (
                by_id[draft.target_id].organisation if draft.target_id in by_id else draft.target_id
            ),
        }
        for draft in pending
    ]

    window_open, window_reason = _window_state()
    ops = config.policy.operations

    from greenroom.jobs.tick import load_brief

    brief = load_brief(repo)

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            **_chrome(request, "board"),
            "brief": brief if brief.get("summary") else None,
            "targets": targets,
            "total": len(targets),
            "counts": [(s, c) for s, c in counts if c],
            "pending": enriched,
            "sends_today": repo.sends_today(tz=ops.send_window.timezone),
            "send_cap": ops.max_sends_per_day,
            "window_open": window_open,
            "window_reason": window_reason,
        },
    )


@router.get("/drafts", response_class=HTMLResponse)
async def drafts(request: Request):
    return await board(request)


# --------------------------------------------------------------------------- target


@router.get("/target/{target_id}", response_class=HTMLResponse)
async def target_view(request: Request, target_id: str):
    if (redirect := _locked(request)) is not None:
        return redirect

    repo = get_repo()
    target = repo.get_target(target_id)
    if target is None:
        return HTMLResponse("<h1>404</h1><p>No such target.</p>", status_code=404)

    research = ResearchDoc.model_validate(target.research) if target.research else None

    from google.cloud import firestore

    messages = [
        m.to_dict()
        for m in repo._col("messages")
        .where(filter=firestore.FieldFilter("target_id", "==", target_id))
        .limit(50)
        .stream()
    ]
    messages.sort(key=lambda m: m.get("created_at") or datetime.min.replace(tzinfo=UTC))

    events = [
        e.to_dict()
        for e in repo._col("events")
        .where(filter=firestore.FieldFilter("target_id", "==", target_id))
        .limit(100)
        .stream()
    ]
    events.sort(key=lambda e: e.get("created_at") or datetime.min.replace(tzinfo=UTC), reverse=True)

    return templates.TemplateResponse(
        request,
        "target.html",
        {
            **_chrome(request, "board"),
            "target": target,
            "research": research,
            "drafts": repo.list_drafts(target_id=target_id),
            "messages": messages,
            "events": events,
        },
    )


@router.post("/target/{target_id}/draft/{draft_id}")
async def resolve_draft(
    request: Request,
    target_id: str,
    draft_id: str,
    action: str = Form(...),
    subject: str = Form(""),
    body: str = Form(""),
):
    """Approve, edit or reject a draft — and move the trust dial accordingly.

    The distinction between approve and edit is the whole mechanic. An approval that
    changed nothing is evidence the agent is writing well enough to be trusted further.
    Any edit is evidence it is not, and costs a level immediately.
    """
    if (redirect := _locked(request)) is not None:
        return redirect

    repo, scheduler = get_repo(), get_scheduler()
    draft = repo.get_draft(draft_id)
    target = repo.get_target(target_id)
    if draft is None or target is None:
        return HTMLResponse("<h1>404</h1>", status_code=404)

    if draft.status != DraftStatus.PENDING:
        # Someone already resolved this — a double-click, or a second tab.
        return RedirectResponse(url=f"/target/{target_id}", status_code=303)

    redirect_to = RedirectResponse(url=f"/target/{target_id}", status_code=303)
    mode = TrustMode(target.mode)

    if action == "reject":
        repo.resolve_draft(draft_id, status=DraftStatus.REJECTED)
        repo.record_decision(
            target_id=target_id,
            thread_id=draft.thread_id,
            kind=DecisionKind.REJECTED,
            draft_before=draft.original_body,
            note="rejected on the dashboard",
        )
        repo.set_mode(target_id, demote(mode), reason="draft rejected")
        repo._col("targets").document(target_id).update({"clean_approvals": 0})
        return redirect_to

    changed = (
        subject.strip() != draft.original_subject.strip()
        or body.strip() != draft.original_body.strip()
    )

    # The trust dial measures whether a human CHANGED the agent's output, not which
    # button they happened to press. Pressing "Save edit & send" without touching the
    # text is an approval: the draft went out exactly as written. Scoring the button
    # instead cost a real approval its promotion credit and stored an empty diff as if
    # it were a style signal, which would have quietly poisoned the style memo with
    # no-op examples.
    if changed:
        diff = "\n".join(
            difflib.unified_diff(
                draft.original_body.splitlines(),
                body.splitlines(),
                fromfile="agent",
                tofile="xavier",
                lineterm="",
            )
        )
        updated = repo.resolve_draft(
            draft_id, status=DraftStatus.EDITED, subject=subject, body=body
        )
        repo.record_decision(
            target_id=target_id,
            thread_id=draft.thread_id,
            kind=DecisionKind.EDITED,
            draft_before=draft.original_body,
            draft_after=body,
            diff=diff,
        )
        repo.set_mode(target_id, demote(mode), reason="draft edited")
        repo._col("targets").document(target_id).update({"clean_approvals": 0})
        scheduler.enqueue_send_for_draft(updated)
        return redirect_to

    # Approved with no changes.
    updated = repo.resolve_draft(draft_id, status=DraftStatus.APPROVED)
    repo.record_decision(
        target_id=target_id,
        thread_id=draft.thread_id,
        kind=DecisionKind.APPROVED,
        draft_before=draft.original_body,
        draft_after=draft.original_body,
    )

    clean = int(target.clean_approvals) + 1
    threshold = get_config().policy.trust.promote_after_clean_approvals
    if clean >= threshold:
        repo.set_mode(target_id, promote(mode), reason=f"{clean} clean approvals")
        clean = 0
    repo._col("targets").document(target_id).update({"clean_approvals": clean})

    scheduler.enqueue_send_for_draft(updated)
    return redirect_to


# --------------------------------------------------------------------------- other


@router.get("/quarantine", response_class=HTMLResponse)
async def quarantine(request: Request):
    if (redirect := _locked(request)) is not None:
        return redirect

    from google.cloud import firestore

    repo = get_repo()
    messages = [
        m.to_dict()
        for m in repo._col("messages")
        .where(filter=firestore.FieldFilter("quarantined", "==", True))
        .limit(100)
        .stream()
    ]
    messages.sort(
        key=lambda m: m.get("created_at") or datetime.min.replace(tzinfo=UTC), reverse=True
    )
    return templates.TemplateResponse(
        request, "quarantine.html", {**_chrome(request, "quarantine"), "messages": messages}
    )


@router.get("/settings", response_class=HTMLResponse)
async def settings_view(request: Request):
    if (redirect := _locked(request)) is not None:
        return redirect

    repo, config = get_repo(), get_config()
    window_open, window_reason = _window_state()
    ops = config.policy.operations
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            **_chrome(request, "settings"),
            "targets": sorted(repo.list_targets(), key=lambda t: t.organisation),
            "sends_today": repo.sends_today(tz=ops.send_window.timezone),
            "send_cap": ops.max_sends_per_day,
            "window_open": window_open,
            "window_reason": window_reason,
            "model": GEMINI_MODEL,
        },
    )


@router.post("/settings/pause")
async def set_pause(request: Request, paused: str = Form(...), reason: str = Form("")):
    if (redirect := _locked(request)) is not None:
        return redirect
    get_repo().set_paused(paused == "true", reason=reason or "paused from the dashboard")
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/mode")
async def set_mode(request: Request, target_id: str = Form(...), mode: str = Form(...)):
    if (redirect := _locked(request)) is not None:
        return redirect
    get_repo().set_mode(target_id, TrustMode(mode), reason="set manually on the dashboard")
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/admin/sync-targets")
async def sync_targets(request: Request):
    """Load config/targets.csv into Firestore. Safe to re-run: existing targets keep
    their status and earned trust level."""
    if (redirect := _locked(request)) is not None:
        return redirect

    repo, queue = get_repo(), get_queue()
    from greenroom.state.models import JobType

    created = 0
    for target in get_config().targets.targets:
        existing = repo.get_target(target.key)
        repo.upsert_target(target)
        if existing is None:
            created += 1
            queue.enqueue(
                job_type=JobType.RESEARCH_TARGET,
                idempotency_key=f"research:{target.key}",
                target_id=target.key,
            )
    log.info("targets synced", extra={"targets_created": created})
    return RedirectResponse(url="/", status_code=303)
