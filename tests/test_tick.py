"""Tick behaviour: veto expiry, brief scheduling, style memo thresholds."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from greenroom.state.models import (
    DecisionDoc,
    DecisionKind,
    DraftDoc,
    DraftStatus,
    TrustMode,
)

pytestmark = pytest.mark.integration


class StubScheduler:
    def __init__(self):
        self.queued: list[str] = []

    def enqueue_send_for_draft(self, draft):
        self.queued.append(draft.draft_id)
        return "job-" + draft.draft_id


def _draft(target_id="t1", *, auto_send_at=None, is_escalation=False):
    return DraftDoc(
        draft_id=uuid.uuid4().hex,
        target_id=target_id,
        kind="reply",
        subject="Re: hi",
        body="body",
        original_subject="Re: hi",
        original_body="body",
        mode_at_draft=TrustMode.VETO,
        auto_send_at=auto_send_at,
        is_escalation=is_escalation,
    )


# ------------------------------------------------------------------ veto expiry


async def test_an_elapsed_veto_window_sends_itself(repo):
    """Veto mode in one behaviour: the human had their window and did not object."""
    from greenroom.jobs.tick import expire_veto_windows

    draft = repo.create_draft(_draft(auto_send_at=datetime.now(UTC) - timedelta(minutes=1)))
    scheduler = StubScheduler()

    result = await expire_veto_windows(repo, scheduler)

    assert result["released"] == 1
    assert scheduler.queued == [draft.draft_id]
    assert repo.get_draft(draft.draft_id).status == DraftStatus.APPROVED


async def test_a_live_veto_window_is_left_alone(repo):
    from greenroom.jobs.tick import expire_veto_windows

    repo.create_draft(_draft(auto_send_at=datetime.now(UTC) + timedelta(minutes=20)))
    scheduler = StubScheduler()

    assert (await expire_veto_windows(repo, scheduler))["released"] == 0
    assert scheduler.queued == []


async def test_an_escalation_never_auto_sends(repo):
    """Even with a window set, an escalation waits for a human. Earned autonomy is
    permission to skip review, never permission to decide outside the envelope."""
    from greenroom.jobs.tick import expire_veto_windows

    repo.create_draft(
        _draft(auto_send_at=datetime.now(UTC) - timedelta(hours=1), is_escalation=True)
    )
    scheduler = StubScheduler()

    assert (await expire_veto_windows(repo, scheduler))["released"] == 0
    assert scheduler.queued == []


async def test_a_review_draft_has_no_window_and_never_fires(repo):
    from greenroom.jobs.tick import expire_veto_windows

    repo.create_draft(_draft(auto_send_at=None))
    assert (await expire_veto_windows(repo, StubScheduler()))["released"] == 0


async def test_auto_send_is_recorded_as_a_decision(repo):
    """The trust dial needs to know a draft went out untouched, even though no human
    pressed anything."""
    from greenroom.jobs.tick import expire_veto_windows

    repo.create_draft(_draft(auto_send_at=datetime.now(UTC) - timedelta(minutes=5)))
    await expire_veto_windows(repo, StubScheduler())

    latest = repo.recent_decisions(limit=1)[0]
    assert latest.kind == DecisionKind.AUTO_SENT
    assert "veto window" in latest.note


# ------------------------------------------------------------------ morning brief


def test_the_brief_is_not_written_before_its_hour(repo):
    from greenroom.jobs.tick import should_write_brief

    six_am_uk = datetime(2026, 9, 1, 5, 0, tzinfo=UTC)  # 06:00 BST
    assert should_write_brief(repo, now=six_am_uk) is False


def test_the_brief_is_written_once_per_day(repo):
    """An hourly tick must not produce a brief every hour."""
    from greenroom.jobs.tick import BRIEF_DOC, should_write_brief

    nine_am_uk = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
    assert should_write_brief(repo, now=nine_am_uk) is True

    repo._col("control").document(BRIEF_DOC).set({"for_date": "2026-09-01", "summary": "x"})
    assert should_write_brief(repo, now=nine_am_uk) is False

    next_day = datetime(2026, 9, 2, 8, 0, tzinfo=UTC)
    assert should_write_brief(repo, now=next_day) is True


def test_a_missed_eight_am_still_produces_a_brief_later(repo):
    """A tick that fails at 08:00 must not skip the day entirely."""
    from greenroom.jobs.tick import should_write_brief

    eleven_am_uk = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    assert should_write_brief(repo, now=eleven_am_uk) is True


def test_brief_facts_are_counted_not_narrated(repo, real_config_dir):
    """The numbers in the brief come from Firestore reads, not from a model. '3 threads
    need you' has to be true."""
    from greenroom.config import load_targets
    from greenroom.jobs.tick import gather_brief_facts

    target = load_targets(real_config_dir / "targets.csv").targets[0]
    doc = repo.upsert_target(target)
    repo.create_draft(_draft(doc.target_id, is_escalation=True))
    repo.create_draft(_draft(doc.target_id))

    facts = gather_brief_facts(repo)
    assert facts["total_targets"] == 1
    assert len(facts["escalations"]) == 1
    assert len(facts["awaiting_approval"]) == 1
    assert facts["escalations"][0]["organisation"] == target.organisation


# ------------------------------------------------------------------ style memo


async def test_no_style_memo_from_too_few_edits(repo):
    """One edit says more about that email than about how someone writes."""
    from greenroom.agents.style import regenerate

    repo._col("decisions").document("d1").set(
        DecisionDoc(
            decision_id="d1",
            target_id="t1",
            kind=DecisionKind.EDITED,
            draft_before="a",
            draft_after="b",
        ).model_dump()
    )
    assert await regenerate(repo) is None


async def test_approvals_alone_produce_no_memo(repo):
    """An approval says 'this was fine' and carries no signal about what to change."""
    from greenroom.agents.style import regenerate

    for i in range(5):
        repo._col("decisions").document(f"a{i}").set(
            DecisionDoc(
                decision_id=f"a{i}",
                target_id="t1",
                kind=DecisionKind.APPROVED,
                draft_before="same",
                draft_after="same",
            ).model_dump()
        )
    assert await regenerate(repo) is None


async def test_no_op_edits_are_not_treated_as_signal(repo):
    """A stored edit whose before and after are identical teaches nothing and would
    dilute the real examples."""
    from greenroom.agents.style import regenerate

    for i in range(4):
        repo._col("decisions").document(f"n{i}").set(
            DecisionDoc(
                decision_id=f"n{i}",
                target_id="t1",
                kind=DecisionKind.EDITED,
                draft_before="identical text",
                draft_after="identical text",
            ).model_dump()
        )
    assert await regenerate(repo) is None
