"""Policy evaluation: is this counter-offer inside the envelope, and if not, which line?

This is the component that makes "negotiates within guardrails" a real claim rather than
a prompt instruction. The Negotiator does not decide whether a deal is acceptable — it
extracts terms, and *this* decides, deterministically, against config/policy.yaml.

Two consequences worth stating:

  * A language model cannot be talked into accepting a bad deal, because it is not the
    thing doing the accepting. The worst a hostile email can achieve is mis-extraction,
    and mis-extraction that invents a *better* offer still gets checked against the same
    rules.
  * Every breach carries the rule id and the actual configured value, so an escalation
    on the dashboard cites `fee.floor: 850` rather than "this seems low to me".

`unmatched_requests` is the catch-all: anything the extractor could not map onto a known
term escalates. Silence is not permission.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from greenroom.config.schemas import Policy


@dataclass(frozen=True)
class Breach:
    """One rule the counter-offer falls outside of."""

    rule_id: str
    explanation: str
    policy_value: str

    def cite(self) -> str:
        return f"{self.rule_id} = {self.policy_value}"


@dataclass
class ProposedTerms:
    """What the other side is asking for, as extracted from their reply.

    Every field is optional: an email that only asks a question proposes no terms, and
    that is not the same as proposing zero.
    """

    fee: float | None = None
    event_date: date | None = None
    attendees: int | None = None

    wants_free: bool = False
    wants_exclusivity: bool = False
    wants_multi_date: bool = False
    mentions_contract_or_legal: bool = False
    mentions_media_or_recording: bool = False

    # Anything the extractor saw but could not map onto a field above.
    unmatched_asks: list[str] = field(default_factory=list)


@dataclass
class PolicyVerdict:
    inside: bool
    breaches: list[Breach]

    @property
    def summary(self) -> str:
        if self.inside:
            return "inside policy"
        return "; ".join(b.explanation for b in self.breaches)

    @property
    def cited_rules(self) -> str:
        return ", ".join(b.cite() for b in self.breaches)


def evaluate(terms: ProposedTerms, policy: Policy, *, today: date | None = None) -> PolicyVerdict:
    """Check proposed terms against the deal envelope. Pure and deterministic."""
    with _span(terms) as span:
        verdict = _evaluate(terms, policy, today=today or date.today())
        span.summarise(
            inside_policy=verdict.inside,
            breaches=verdict.cited_rules or "none",
            reason=verdict.summary,
        )
        return verdict


def _span(terms: ProposedTerms):
    """The policy decision is the single most important step to be able to audit, so it
    gets its own span even though no model is involved in it."""
    from greenroom.obs import span

    return span(
        "policy.evaluate",
        kind="decision",
        fee=terms.fee,
        attendees=terms.attendees,
        event_date=str(terms.event_date) if terms.event_date else None,
        wants_free=terms.wants_free,
        wants_exclusivity=terms.wants_exclusivity,
    )


def _evaluate(terms: ProposedTerms, policy: Policy, *, today: date) -> PolicyVerdict:
    breaches: list[Breach] = []
    esc = policy.escalate

    # --- always-escalate flags ------------------------------------------
    if terms.wants_free and esc.free_event:
        breaches.append(
            Breach(
                "escalate.free_event",
                "they are asking us to play for free or for exposure",
                "true",
            )
        )
    if terms.wants_exclusivity and esc.exclusivity:
        breaches.append(Breach("escalate.exclusivity", "they are asking for exclusivity", "true"))
    if terms.wants_multi_date and esc.multi_date_commitment:
        breaches.append(
            Breach(
                "escalate.multi_date_commitment",
                "they want a commitment to more than one date at once",
                "true",
            )
        )
    if terms.mentions_contract_or_legal and esc.contract_or_legal:
        breaches.append(
            Breach(
                "escalate.contract_or_legal",
                "the reply raises contracts, terms, insurance or legal points",
                "true",
            )
        )
    if terms.mentions_media_or_recording and esc.media_or_recording:
        breaches.append(
            Breach(
                "escalate.media_or_recording",
                "they want filming, streaming or recording rights",
                "true",
            )
        )

    # --- money -----------------------------------------------------------
    if terms.fee is not None and terms.fee < policy.fee.floor:
        breaches.append(
            Breach(
                "fee.floor",
                f"they offered {policy.fee.currency} {terms.fee:.0f}, "
                f"below the floor of {policy.fee.currency} {policy.fee.floor:.0f}",
                f"{policy.fee.floor:.0f}",
            )
        )

    # --- capacity --------------------------------------------------------
    if terms.attendees is not None and terms.attendees > esc.max_attendees:
        breaches.append(
            Breach(
                "escalate.max_attendees",
                f"the event is for {terms.attendees} people, above the {esc.max_attendees} limit",
                str(esc.max_attendees),
            )
        )

    # --- dates -----------------------------------------------------------
    if terms.event_date is not None:
        avail = policy.availability
        window = next((w for w in avail.windows if w.contains(terms.event_date)), None)
        if window is None:
            windows = ", ".join(f"{w.start}..{w.end}" for w in avail.windows)
            breaches.append(
                Breach(
                    "availability.windows",
                    f"{terms.event_date} is outside every available window",
                    windows,
                )
            )
        if terms.event_date.weekday() not in avail.allowed_weekdays:
            names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            allowed = ", ".join(names[d] for d in avail.allowed_weekdays)
            breaches.append(
                Breach(
                    "availability.allowed_weekdays",
                    f"{terms.event_date} is a {names[terms.event_date.weekday()]}; "
                    f"we only play {allowed}",
                    allowed,
                )
            )
        lead = (terms.event_date - today).days
        if lead < avail.min_lead_time_days:
            breaches.append(
                Breach(
                    "availability.min_lead_time_days",
                    f"{terms.event_date} is {lead} days away; we need "
                    f"{avail.min_lead_time_days} days' notice",
                    str(avail.min_lead_time_days),
                )
            )

    # --- catch-all -------------------------------------------------------
    # Deliberately last, so a breach with a precise rule id is reported ahead of the
    # vague one when both apply.
    if terms.unmatched_asks and esc.unmatched_requests:
        breaches.append(
            Breach(
                "escalate.unmatched_requests",
                "they asked for something the policy does not cover: "
                + "; ".join(terms.unmatched_asks[:3]),
                "true",
            )
        )

    return PolicyVerdict(inside=not breaches, breaches=breaches)


def describe_envelope(policy: Policy) -> str:
    """A plain-language summary of what the agent may agree to.

    Given to the Negotiator so its *draft* reflects the same envelope this module
    enforces — but the draft is still checked by `evaluate`, never trusted.
    """
    fee, avail, meet = policy.fee, policy.availability, policy.meetings
    windows = "; ".join(f"{w.label or w.id} ({w.start} to {w.end})" for w in avail.windows)
    names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    nights = ", ".join(names[d] for d in avail.allowed_weekdays)

    return f"""\
FEE
  Standard: {fee.currency} {fee.standard:.0f}. You may negotiate down to, but never
  below, {fee.currency} {fee.floor:.0f}. Anything below that is not yours to accept.
  Deposit {fee.deposit_pct}%, payment terms {fee.payment_terms_days} days.

DATES
  Available windows: {windows}
  Nights we play: {nights}
  Minimum notice: {avail.min_lead_time_days} days.

INCLUDED IN THE FEE
{chr(10).join("  - " + item for item in policy.includes)}

NOT INCLUDED
{chr(10).join("  - " + item for item in policy.excludes)}

CALLS
  {meet.duration_minutes} minutes, {meet.timezone}, between {meet.earliest_hour:02d}:00
  and {meet.latest_hour:02d}:00, offering {meet.slots_to_offer} options.

ALWAYS NEEDS A HUMAN (never agree to these yourself)
  - Playing for free or "for exposure"
  - Any exclusivity or non-compete
  - Events above {policy.escalate.max_attendees} capacity
  - Committing to more than one date at once
  - Anything touching contracts, terms, insurance or legal
  - Filming, streaming or recording rights
  - Anything at all that is not covered above
"""
