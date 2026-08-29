"""The pipeline state machine.

These matter beyond tidiness: an agent talked into something odd by a hostile email
must not be able to move a target somewhere the pipeline does not allow.
"""

from __future__ import annotations

import pytest

from greenroom.state.machine import (
    InvalidTransition,
    allowed_next,
    assert_transition,
    can_transition,
    demote,
    is_terminal,
    mermaid_diagram,
    promote,
)
from greenroom.state.models import TERMINAL_STATUSES
from greenroom.state.models import TargetStatus as S
from greenroom.state.models import TrustMode as M


def test_happy_path_is_walkable():
    path = [S.QUEUED, S.RESEARCHED, S.PITCHED, S.REPLIED, S.NEGOTIATING, S.BOOKED]
    for current, nxt in zip(path, path[1:], strict=False):
        assert can_transition(current, nxt), f"{current} → {nxt} should be legal"


def test_cannot_skip_the_pipeline():
    with pytest.raises(InvalidTransition):
        assert_transition(S.QUEUED, S.BOOKED)
    with pytest.raises(InvalidTransition):
        assert_transition(S.QUEUED, S.PITCHED)


def test_terminal_states_are_dead_ends():
    for status in TERMINAL_STATUSES:
        assert allowed_next(status) == frozenset(), f"{status} should be terminal"
        assert is_terminal(status)


def test_escalation_is_reachable_from_every_live_status():
    """If the agent is unsure the answer is always 'ask a human', and no bookkeeping
    rule may stand in the way of that."""
    for status in S:
        if status in TERMINAL_STATUSES or status is S.ESCALATED:
            continue
        assert S.ESCALATED in allowed_next(status), f"cannot escalate from {status}"


def test_escalation_can_be_resolved_back_into_the_pipeline():
    for nxt in (S.NEGOTIATING, S.BOOKED, S.DECLINED, S.CLOSED_NO_REPLY):
        assert can_transition(S.ESCALATED, nxt)


def test_reasserting_the_same_status_is_a_no_op():
    """A retried job that already applied its transition must not blow up on rerun."""
    assert assert_transition(S.PITCHED, S.PITCHED) == S.PITCHED
    assert assert_transition(S.BOOKED, S.BOOKED) == S.BOOKED


def test_invalid_transition_names_the_legal_moves():
    with pytest.raises(InvalidTransition, match="legal moves"):
        assert_transition(S.QUEUED, S.NEGOTIATING)


def test_error_message_is_actionable():
    try:
        assert_transition(S.QUEUED, S.BOOKED)
    except InvalidTransition as exc:
        assert "queued" in str(exc) and "booked" in str(exc)
        assert "researched" in str(exc), "should list what IS allowed"


# ------------------------------------------------------------------ trust dial


def test_promotion_climbs_one_rung_and_stops_at_autopilot():
    assert promote(M.REVIEW) == M.VETO
    assert promote(M.VETO) == M.AUTOPILOT
    assert promote(M.AUTOPILOT) == M.AUTOPILOT


def test_demotion_falls_one_rung_and_stops_at_review():
    assert demote(M.AUTOPILOT) == M.VETO
    assert demote(M.VETO) == M.REVIEW
    assert demote(M.REVIEW) == M.REVIEW


def test_trust_is_slow_to_gain_and_fast_to_lose():
    """Three clean approvals to climb a rung, one edit to fall one."""
    mode = M.REVIEW
    for _ in range(2):  # two promotions = six clean approvals
        mode = promote(mode)
    assert mode == M.AUTOPILOT
    assert demote(mode) == M.VETO, "a single edit undoes three approvals"


# ------------------------------------------------------------------ diagram


def test_diagram_is_generated_from_the_transition_table():
    """The README diagram cannot drift from the code because it is derived from it."""
    diagram = mermaid_diagram()
    assert diagram.startswith("stateDiagram-v2")
    for status in S:
        assert status.value in diagram
    assert "queued --> researched" in diagram
    assert "queued --> booked" not in diagram
