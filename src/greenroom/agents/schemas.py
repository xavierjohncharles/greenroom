"""Structured outputs for the reasoning agents.

Every agent returns a typed object rather than prose. That is not decoration: it is
what lets the Gatekeeper hand the Negotiator a classified summary instead of raw
attacker-controlled text, and what lets the dashboard render a draft without parsing
free-form output.

Verified on ADK 2.8.0: `output_schema` works *together* with `tools`, so the Researcher
can ground with Google Search and still return typed fields in one agent. This was not
possible in ADK 1.x and is why the Researcher is one agent rather than two.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ResearchDoc(BaseModel):
    """What the Researcher learns about a target. Stored on the target document."""

    organisation: str = Field(description="Official name of the students' union or venue")
    venue_name: str = Field(default="", description="The room or venue they run events in")
    venue_capacity: str = Field(default="", description="Capacity if stated, else empty")
    venue_ownership: str = Field(
        default="", description="Who runs the venue: the union, the university, or a third party"
    )
    events_programme: str = Field(
        default="", description="What they already put on: club nights, live music, regular events"
    )
    relevant_people: list[str] = Field(
        default_factory=list,
        description="Names and roles of people who book events, if publicly listed",
    )
    freshers_timing: str = Field(
        default="", description="When their Freshers or Refreshers period falls, if findable"
    )
    best_hook: str = Field(
        description=(
            "One specific, checkable fact about THIS organisation that proves the email "
            "is not a mail-merge. A booking, a venue detail, a recent event, a history."
        )
    )
    hook_source: str = Field(default="", description="URL the hook came from")
    confidence: str = Field(
        default="medium", description="high, medium or low — how well-sourced this research is"
    )
    notes: str = Field(default="", description="Anything else useful to a person writing a pitch")


class PitchDraft(BaseModel):
    """A drafted email, ready for a human to approve, edit or reject."""

    subject: str = Field(description="Email subject line. Specific, lower-case-ish, no clickbait.")
    body: str = Field(
        description="Plain text email body. No markdown, no HTML, no signature block."
    )
    hook_used: str = Field(
        default="", description="The specific fact from research this email leans on"
    )
    reasoning: str = Field(
        default="", description="One sentence: why this angle for this target. Shown in the trace."
    )
