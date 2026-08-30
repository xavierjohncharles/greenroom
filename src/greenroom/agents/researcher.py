"""The Researcher: grounds a target row in real, checkable facts.

Tools: Google Search grounding and URL context. **No send tool, no calendar tool, no
Firestore write tool.** The read side of Greenroom is physically incapable of sending
mail — not discouraged from it, incapable — because the send tool is never handed to it.

https://adk.dev/tools-custom/
"""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.adk.tools import google_search, url_context

from greenroom.agents.runtime import run_agent
from greenroom.agents.schemas import ResearchDoc
from greenroom.models import GEMINI_MODEL
from greenroom.obs import get_logger
from greenroom.state.models import TargetDoc

log = get_logger(__name__)

INSTRUCTION = """\
You research UK students' unions and small music venues for a live events brand that
wants to pitch them a club night.

Your entire job is to find ONE specific, checkable fact that proves an email was written
by a person who looked them up — not a mail merge. That fact goes in `best_hook`.

A good hook is concrete, verifiable AND CURRENT — something that is true of them
this year, and that gives a reason to talk now:
  - "your Thursday night 'Rewind' has run in the Great Hall since 2019"
  - "your SU took the venue back in-house from Sodexo last year"
  - "you have just announced Welcome Week runs 18-25 September"

A bad hook is anything that would be true of any students' union:
  - "you have a vibrant student community"
  - "you host a range of events"
  - "you are a leading university"

RECENCY IS A HARD REQUIREMENT. Prefer facts from the last 12 months. Anything older
than about three years is history, not a reason to book a night — a famous gig in 2009
tells the reader you searched their Wikipedia page, which is the opposite of the effect
you want. If the only specific thing you can find is a decade old, treat that as NO
HOOK: set `best_hook` to "" and `confidence` to "low".

Prefer, in this order: their current or upcoming programme; a recent change to the
venue or the team; this year's Freshers/Refreshers dates; a recent event they ran.

Rules:
  - Search before you answer. Do not rely on what you already know.
  - If you cannot find a specific, CURRENT fact, say so: set `best_hook` to "" and
    `confidence` to "low". An honest empty hook is far more useful than a stale or
    invented one, because a human will write that email instead.
  - Never invent a name, a capacity, a date or a booking. If it is not in a source, it
    does not go in the output.
  - Put the URL you got the hook from in `hook_source`.
"""

researcher_agent = LlmAgent(
    name="researcher",
    model=GEMINI_MODEL,
    description="Researches a target organisation and returns structured, sourced facts.",
    instruction=INSTRUCTION,
    tools=[google_search, url_context],
    output_schema=ResearchDoc,
)


def build_prompt(target: TargetDoc) -> str:
    lines = [f"Organisation: {target.organisation}"]
    if target.contact_name:
        lines.append(f"Named contact: {target.contact_name}")
    if target.venue_notes:
        lines.append(f"What we already know about the venue: {target.venue_notes}")
    if target.context:
        lines.append(f"Existing context: {target.context}")
    lines.append(f"Their contact address is {target.email} (useful for finding their website).")
    return "\n".join(lines)


async def research(target: TargetDoc) -> ResearchDoc:
    raw = await run_agent(researcher_agent, build_prompt(target))
    doc = ResearchDoc.model_validate_json(raw)
    log.info(
        "research complete",
        extra={
            "target_id": target.target_id,
            "confidence": doc.confidence,
            "has_hook": bool(doc.best_hook),
        },
    )
    return doc
