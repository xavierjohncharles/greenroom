"""Pydantic schemas for config/brand.yaml, config/policy.yaml and config/targets.csv.

These are strict on purpose. A typo in policy.yaml is the difference between the
agent escalating a bad deal and silently accepting it, so config failures must be
loud and must happen at startup rather than at 2am inside a negotiation.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

# Monday=0 .. Sunday=6, matching datetime.weekday().
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class StrictModel(BaseModel):
    """Reject unknown keys everywhere. A misspelled config key is a silent bug."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# --------------------------------------------------------------------------- brand


class BrandLinks(StrictModel):
    website: str = ""
    past_events: str = ""
    mixes_or_video: str = ""
    press_or_reviews: str = ""


class CopyRules(StrictModel):
    max_words: int = Field(default=200, ge=50, le=600)
    banned_phrases: list[str] = Field(default_factory=list)
    require_specific_hook: bool = True


class Brand(StrictModel):
    company_name: str = Field(min_length=1)
    sender_name: str = Field(min_length=1)
    sender_email: EmailStr
    sender_role: str = ""
    pitch: str = Field(min_length=40)
    proof_points: list[str] = Field(min_length=1)
    links: BrandLinks = Field(default_factory=BrandLinks)
    tone_notes: str = ""
    copy_rules: CopyRules = Field(default_factory=CopyRules)


# --------------------------------------------------------------------------- policy


class DateWindow(StrictModel):
    id: str = Field(min_length=1)
    label: str = ""
    start: date
    end: date

    @model_validator(mode="after")
    def _ordered(self) -> DateWindow:
        if self.end < self.start:
            raise ValueError(f"window {self.id!r}: end {self.end} is before start {self.start}")
        return self

    def contains(self, when: date) -> bool:
        return self.start <= when <= self.end


class Availability(StrictModel):
    windows: list[DateWindow] = Field(min_length=1)
    allowed_weekdays: list[int] = Field(min_length=1)
    min_lead_time_days: int = Field(default=21, ge=0)

    @field_validator("allowed_weekdays")
    @classmethod
    def _weekday_range(cls, v: list[int]) -> list[int]:
        bad = [d for d in v if not 0 <= d <= 6]
        if bad:
            raise ValueError(f"allowed_weekdays must be 0-6 (Mon-Sun), got {bad}")
        return sorted(set(v))


class Fee(StrictModel):
    currency: str = Field(default="GBP", min_length=3, max_length=3)
    standard: float = Field(gt=0)
    floor: float = Field(gt=0)
    deposit_pct: int = Field(default=50, ge=0, le=100)
    payment_terms_days: int = Field(default=14, ge=0)

    @model_validator(mode="after")
    def _floor_below_standard(self) -> Fee:
        if self.floor > self.standard:
            raise ValueError(
                f"fee.floor ({self.floor}) is above fee.standard ({self.standard}); "
                "the agent would have no room to negotiate and would escalate everything"
            )
        return self


class Meetings(StrictModel):
    duration_minutes: int = Field(default=30, gt=0, le=240)
    timezone: str = "Europe/London"
    earliest_hour: int = Field(default=10, ge=0, le=23)
    latest_hour: int = Field(default=17, ge=0, le=23)
    allowed_weekdays: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    slots_to_offer: int = Field(default=3, ge=1, le=10)
    min_notice_hours: int = Field(default=24, ge=0)

    @model_validator(mode="after")
    def _hours_ordered(self) -> Meetings:
        if self.latest_hour <= self.earliest_hour:
            raise ValueError(
                f"meetings.latest_hour ({self.latest_hour}) must be after "
                f"earliest_hour ({self.earliest_hour})"
            )
        return self


class EscalationRules(StrictModel):
    free_event: bool = True
    exclusivity: bool = True
    max_attendees: int = Field(default=600, gt=0)
    multi_date_commitment: bool = True
    contract_or_legal: bool = True
    media_or_recording: bool = True
    unmatched_requests: bool = True


class SendWindow(StrictModel):
    timezone: str = "Europe/London"
    start_hour: int = Field(default=9, ge=0, le=23)
    end_hour: int = Field(default=17, ge=0, le=23)
    weekdays: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])

    @model_validator(mode="after")
    def _hours_ordered(self) -> SendWindow:
        if self.end_hour <= self.start_hour:
            raise ValueError(
                f"send_window.end_hour ({self.end_hour}) must be after "
                f"start_hour ({self.start_hour})"
            )
        return self


class Operations(StrictModel):
    max_sends_per_day: int = Field(default=25, gt=0)
    send_window: SendWindow = Field(default_factory=SendWindow)
    follow_up_days: list[int] = Field(default_factory=lambda: [3, 7])
    close_after_days: int = Field(default=14, gt=0)
    daily_spend_cap_usd: float = Field(default=20.0, gt=0)
    veto_window_minutes: int = Field(default=30, gt=0)

    @model_validator(mode="after")
    def _follow_ups_before_close(self) -> Operations:
        if self.follow_up_days and max(self.follow_up_days) >= self.close_after_days:
            raise ValueError(
                f"operations.close_after_days ({self.close_after_days}) must be after the "
                f"last follow-up ({max(self.follow_up_days)}), or that follow-up never fires"
            )
        return self


class TrustConfig(StrictModel):
    default_mode: str = "review"
    promote_after_clean_approvals: int = Field(default=3, gt=0)
    demote_on_edit: bool = True
    escalations_always_review: bool = True

    @field_validator("default_mode")
    @classmethod
    def _known_mode(cls, v: str) -> str:
        if v not in {"review", "veto", "autopilot"}:
            raise ValueError(f"trust.default_mode must be review|veto|autopilot, got {v!r}")
        return v


class Policy(StrictModel):
    version: int = 1
    availability: Availability
    fee: Fee
    includes: list[str] = Field(default_factory=list)
    excludes: list[str] = Field(default_factory=list)
    meetings: Meetings = Field(default_factory=Meetings)
    escalate: EscalationRules = Field(default_factory=EscalationRules)
    operations: Operations = Field(default_factory=Operations)
    trust: TrustConfig = Field(default_factory=TrustConfig)


# --------------------------------------------------------------------------- targets


class Target(StrictModel):
    organisation: str = Field(min_length=1)
    contact_name: str = ""
    email: EmailStr
    venue_notes: str = ""
    tier: int = Field(default=3, ge=1, le=3)
    context: str = ""

    @property
    def key(self) -> str:
        """Stable Firestore document id, derived from the email."""
        return self.email.strip().lower().replace("@", "_at_").replace(".", "_")


class TargetList(StrictModel):
    targets: list[Target]

    @model_validator(mode="after")
    def _unique_emails(self) -> TargetList:
        seen: dict[str, int] = {}
        for i, t in enumerate(self.targets, start=2):  # +2: header row is line 1
            addr = t.email.lower()
            if addr in seen:
                raise ValueError(
                    f"duplicate email {addr!r} on lines {seen[addr]} and {i} of targets.csv"
                )
            seen[addr] = i
        return self

    @property
    def allowed_addresses(self) -> frozenset[str]:
        """The send allow-list. Nothing outside this set can ever be emailed."""
        return frozenset(t.email.lower() for t in self.targets)

    def by_email(self, email: str) -> Target | None:
        addr = email.strip().lower()
        return next((t for t in self.targets if t.email.lower() == addr), None)
