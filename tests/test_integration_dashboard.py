"""The approve / edit / reject flow, and the trust dial it drives.

The distinction between "approved unchanged" and "edited" is the whole autonomy
mechanic, so it gets tested at the level a human actually touches it: an HTTP form post.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from greenroom.state.models import (
    DecisionKind,
    DraftDoc,
    DraftStatus,
    JobStatus,
    JobType,
    TargetStatus,
    TrustMode,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def client(repo, queue, monkeypatch):
    """A dashboard wired to the throwaway namespace rather than the real pipeline."""
    from greenroom.agents.scheduler import Scheduler
    from greenroom.config import get_config
    from greenroom.tools.calendar import CalendarTool
    from greenroom.tools.gmail import GmailTool
    from greenroom.web import deps
    from greenroom.web.main import app

    scheduler = Scheduler(
        repo=repo,
        queue=queue,
        config=get_config(),
        gmail=GmailTool(dry_run=True),
        calendar=CalendarTool(dry_run=True),
        dry_run=True,
    )
    monkeypatch.setattr(deps, "get_repo", lambda: repo)
    monkeypatch.setattr(deps, "get_queue", lambda: queue)
    monkeypatch.setattr(deps, "get_scheduler", lambda: scheduler)
    monkeypatch.setattr("greenroom.web.dashboard.get_repo", lambda: repo)
    monkeypatch.setattr("greenroom.web.dashboard.get_queue", lambda: queue)
    monkeypatch.setattr("greenroom.web.dashboard.get_scheduler", lambda: scheduler)

    with TestClient(app) as c:
        yield c


@pytest.fixture
def target_with_draft(repo, real_config_dir):
    from greenroom.config import load_targets

    target = load_targets(real_config_dir / "targets.csv").targets[0]
    doc = repo.upsert_target(target)
    repo.set_status(doc.target_id, TargetStatus.RESEARCHED)

    draft = repo.create_draft(
        DraftDoc(
            draft_id=uuid.uuid4().hex,
            target_id=doc.target_id,
            kind="pitch",
            subject="live music nights at rise",
            body="Club Sandwich has been a fixture at RISE for years.",
            original_subject="live music nights at rise",
            original_body="Club Sandwich has been a fixture at RISE for years.",
            mode_at_draft=TrustMode.REVIEW,
        )
    )
    return doc, draft


def _post(client, target_id, draft_id, **form):
    return client.post(f"/target/{target_id}/draft/{draft_id}", data=form, follow_redirects=False)


# ------------------------------------------------------------------ rendering


def test_the_board_shows_a_pending_draft(client, target_with_draft):
    target, _ = target_with_draft
    body = client.get("/").text
    assert "Waiting on you" in body
    # Match on a fragment with no apostrophe: Jinja escapes ' as &#39;, which is not
    # what html.escape produces, so a naive round-trip comparison fails.
    assert target.organisation.split("'")[0] in body
    assert f"/target/{target.target_id}" in body


def test_the_target_page_shows_the_draft_for_editing(client, target_with_draft):
    target, draft = target_with_draft
    body = client.get(f"/target/{target.target_id}").text
    assert draft.body in body
    assert "Approve" in body and "Reject" in body


# ------------------------------------------------------------------ approve


def test_approving_unchanged_queues_the_send_and_counts_toward_promotion(
    client, repo, queue, target_with_draft
):
    target, draft = target_with_draft

    response = _post(
        client,
        target.target_id,
        draft.draft_id,
        action="approve",
        subject=draft.subject,
        body=draft.body,
    )
    assert response.status_code == 303

    assert repo.get_draft(draft.draft_id).status == DraftStatus.APPROVED

    jobs = queue.list_by_status(JobStatus.QUEUED)
    assert [j.job_type for j in jobs] == [JobType.SEND_PITCH]
    assert jobs[0].payload["body"] == draft.body

    after = repo.get_target(target.target_id)
    assert after.clean_approvals == 1
    assert after.mode == TrustMode.REVIEW, "one approval is not yet a promotion"


def test_three_clean_approvals_promote_one_level(client, repo, target_with_draft):
    target, _ = target_with_draft

    for _ in range(3):
        draft = repo.create_draft(
            DraftDoc(
                draft_id=uuid.uuid4().hex,
                target_id=target.target_id,
                subject="s",
                body="b",
                original_subject="s",
                original_body="b",
            )
        )
        _post(client, target.target_id, draft.draft_id, action="approve", subject="s", body="b")

    after = repo.get_target(target.target_id)
    assert after.mode == TrustMode.VETO, "three clean approvals should promote review → veto"
    assert after.clean_approvals == 0, "the counter resets after a promotion"


def test_approving_twice_sends_once(client, repo, queue, target_with_draft):
    """A double-clicked Approve button must not produce two emails."""
    target, draft = target_with_draft

    _post(
        client,
        target.target_id,
        draft.draft_id,
        action="approve",
        subject=draft.subject,
        body=draft.body,
    )
    _post(
        client,
        target.target_id,
        draft.draft_id,
        action="approve",
        subject=draft.subject,
        body=draft.body,
    )

    assert len(queue.list_by_status(JobStatus.QUEUED)) == 1


# ------------------------------------------------------------------ edit


def test_editing_demotes_immediately_and_stores_the_diff(client, repo, queue, target_with_draft):
    target, draft = target_with_draft
    repo.set_mode(target.target_id, TrustMode.AUTOPILOT)

    edited = "Club Sandwich has run at RISE for years — we'd love to add a night."
    _post(
        client, target.target_id, draft.draft_id, action="edit", subject=draft.subject, body=edited
    )

    after_draft = repo.get_draft(draft.draft_id)
    assert after_draft.status == DraftStatus.EDITED
    assert after_draft.body == edited
    assert after_draft.original_body == draft.body, "the agent's original must be preserved"

    assert repo.get_target(target.target_id).mode == TrustMode.VETO, "an edit costs one level"

    decisions = repo.recent_decisions(limit=5)
    assert decisions[0].kind == DecisionKind.EDITED
    assert decisions[0].diff, "the diff is the training signal; it must be stored"

    assert queue.list_by_status(JobStatus.QUEUED)[0].payload["body"] == edited


def test_a_silent_edit_is_detected_even_when_approve_was_pressed(client, repo, target_with_draft):
    """If the text changed, it is an edit — whichever button was clicked."""
    target, draft = target_with_draft
    _post(
        client,
        target.target_id,
        draft.draft_id,
        action="approve",
        subject=draft.subject,
        body="totally different",
    )

    assert repo.get_draft(draft.draft_id).status == DraftStatus.EDITED
    assert repo.get_target(target.target_id).clean_approvals == 0


def test_editing_resets_the_promotion_counter(client, repo, target_with_draft):
    target, draft = target_with_draft
    repo._col("targets").document(target.target_id).update({"clean_approvals": 2})

    _post(
        client,
        target.target_id,
        draft.draft_id,
        action="edit",
        subject=draft.subject,
        body="changed",
    )

    assert repo.get_target(target.target_id).clean_approvals == 0


# ------------------------------------------------------------------ reject


def test_rejecting_sends_nothing_and_demotes(client, repo, queue, target_with_draft):
    target, draft = target_with_draft
    repo.set_mode(target.target_id, TrustMode.VETO)

    _post(
        client,
        target.target_id,
        draft.draft_id,
        action="reject",
        subject=draft.subject,
        body=draft.body,
    )

    assert repo.get_draft(draft.draft_id).status == DraftStatus.REJECTED
    assert queue.list_by_status(JobStatus.QUEUED) == [], "a rejected draft must queue no send"
    assert repo.get_target(target.target_id).mode == TrustMode.REVIEW


# ------------------------------------------------------------------ kill switch


def test_the_pause_control_reaches_the_send_gate(client, repo):
    client.post(
        "/settings/pause", data={"paused": "true", "reason": "demo"}, follow_redirects=False
    )
    paused, reason = repo.is_paused()
    assert paused is True and "demo" in reason
    assert "PAUSED" in client.get("/").text


async def test_pressing_save_edit_without_changing_anything_counts_as_approval(
    client, repo, target_with_draft
):
    """Regression: the trust dial scored the button pressed, not whether the text
    changed. Clicking "Save edit & send" on an untouched draft was recorded as an edit,
    which cost a genuine approval its promotion credit and stored an empty diff as if it
    were a style signal."""
    target, draft = target_with_draft

    _post(
        client, target.target_id, draft.draft_id,
        action="edit", subject=draft.subject, body=draft.body,
    )

    assert repo.get_draft(draft.draft_id).status == DraftStatus.APPROVED
    assert repo.get_target(target.target_id).clean_approvals == 1

    latest = repo.recent_decisions(limit=1)[0]
    assert latest.kind == DecisionKind.APPROVED
    assert not latest.diff, "an unchanged draft has no diff to learn from"


async def test_a_real_edit_still_registers_as_an_edit(client, repo, target_with_draft):
    target, draft = target_with_draft
    _post(
        client, target.target_id, draft.draft_id,
        action="approve", subject=draft.subject, body=draft.body + " One more line.",
    )
    assert repo.get_draft(draft.draft_id).status == DraftStatus.EDITED
    assert repo.recent_decisions(limit=1)[0].diff
