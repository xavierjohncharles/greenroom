"""The pipeline state machine.

    queued → researched → pitched → replied → negotiating → booked
                             │         │           │
                             │         └───────────┼──→ escalated ──→ (resolved)
                             │                     └──→ declined
                             └──→ closed_no_reply

Transitions are declared once, here, and every write goes through `assert_transition`.
The value of that is not tidiness: it is that an agent which has been talked into
something strange by a hostile email cannot move a target somewhere the pipeline does
not allow. "Escalated" is reachable from anywhere live, because escalation must never
be blocked by bookkeeping.
"""

from __future__ import annotations

from greenroom.state.models import TERMINAL_STATUSES, TRUST_LADDER, TargetStatus, TrustMode

# What may follow what. Escalation is added to every live status below.
_TRANSITIONS: dict[TargetStatus, set[TargetStatus]] = {
    TargetStatus.QUEUED: {TargetStatus.RESEARCHED, TargetStatus.DECLINED},
    TargetStatus.RESEARCHED: {TargetStatus.PITCHED, TargetStatus.DECLINED},
    TargetStatus.PITCHED: {
        TargetStatus.REPLIED,
        TargetStatus.CLOSED_NO_REPLY,
        TargetStatus.DECLINED,
    },
    TargetStatus.REPLIED: {
        TargetStatus.NEGOTIATING,
        TargetStatus.BOOKED,
        TargetStatus.DECLINED,
        TargetStatus.CLOSED_NO_REPLY,
    },
    TargetStatus.NEGOTIATING: {
        TargetStatus.BOOKED,
        TargetStatus.DECLINED,
        TargetStatus.CLOSED_NO_REPLY,
    },
    # A human resolving an escalation can send it anywhere sensible, including back
    # into negotiation.
    TargetStatus.ESCALATED: {
        TargetStatus.NEGOTIATING,
        TargetStatus.BOOKED,
        TargetStatus.DECLINED,
        TargetStatus.CLOSED_NO_REPLY,
        TargetStatus.REPLIED,
    },
    TargetStatus.BOOKED: set(),
    TargetStatus.DECLINED: set(),
    TargetStatus.CLOSED_NO_REPLY: set(),
}

# Escalation is always available from a live status. If the agent is unsure, the
# answer is always "ask Xavier", and no state rule may stand in the way of that.
for _status, _allowed in _TRANSITIONS.items():
    if _status not in TERMINAL_STATUSES and _status is not TargetStatus.ESCALATED:
        _allowed.add(TargetStatus.ESCALATED)


class InvalidTransition(ValueError):
    """Raised when a status change is not permitted by the state machine."""


def allowed_next(status: TargetStatus) -> frozenset[TargetStatus]:
    return frozenset(_TRANSITIONS[TargetStatus(status)])


def can_transition(current: TargetStatus, nxt: TargetStatus) -> bool:
    return TargetStatus(nxt) in _TRANSITIONS[TargetStatus(current)]


def assert_transition(current: TargetStatus, nxt: TargetStatus) -> TargetStatus:
    """Return `nxt` if the move is legal, else raise with the legal options listed."""
    current, nxt = TargetStatus(current), TargetStatus(nxt)
    if current == nxt:
        # Re-asserting the current status is a harmless no-op: a retried job that
        # already applied its transition must not blow up on the second run.
        return nxt
    if not can_transition(current, nxt):
        legal = ", ".join(sorted(s.value for s in allowed_next(current))) or "(terminal)"
        raise InvalidTransition(f"cannot move {current.value} → {nxt.value}; legal moves: {legal}")
    return nxt


def is_terminal(status: TargetStatus) -> bool:
    return TargetStatus(status) in TERMINAL_STATUSES


# --------------------------------------------------------------------------- trust


def promote(mode: TrustMode) -> TrustMode:
    """One rung up the ladder. Autopilot is the top."""
    idx = TRUST_LADDER.index(TrustMode(mode))
    return TRUST_LADDER[min(idx + 1, len(TRUST_LADDER) - 1)]


def demote(mode: TrustMode) -> TrustMode:
    """One rung down. Review is the floor.

    Demotion is deliberately as coarse as promotion is slow: three clean approvals to
    climb a rung, one edit to fall one. Trust should be cheap to lose.
    """
    idx = TRUST_LADDER.index(TrustMode(mode))
    return TRUST_LADDER[max(idx - 1, 0)]


def mermaid_diagram() -> str:
    """The state diagram, generated from the transition table itself.

    Generated rather than hand-drawn so the README can never drift from the code.
    """
    lines = ["stateDiagram-v2", "    [*] --> queued"]
    for status in TargetStatus:
        for nxt in sorted(allowed_next(status)):
            lines.append(f"    {status.value} --> {nxt.value}")
    for status in sorted(TERMINAL_STATUSES):
        lines.append(f"    {status.value} --> [*]")
    return "\n".join(lines)
