"""The Writer: turns research into a pitch email in Xavier's voice.

**Tools: none.** The Writer reads brand config, policy, the research doc and past human
decisions, and returns text. It cannot search, cannot fetch a URL, and cannot send.
That is deliberate beyond tidiness: the Writer is the component most exposed to
attacker-influenced content once a thread is live (research text, quoted replies), and
a component with no tools has nothing worth hijacking.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent

from greenroom.agents.runtime import run_agent
from greenroom.agents.schemas import PitchDraft, ResearchDoc
from greenroom.config import AppConfig
from greenroom.models import GEMINI_MODEL
from greenroom.obs import get_logger
from greenroom.state.models import DecisionDoc, DecisionKind, TargetDoc

log = get_logger(__name__)

INSTRUCTION = """\
You write short cold outreach emails for a live events brand pitching UK students' unions.

You are writing AS the sender, in their voice. You are not a marketing department.

Non-negotiables:
  - Open with the specific hook from the research, IN YOUR OWN WORDS and briefly. A
    clause or a short sentence, not the research note pasted in. You are showing you
    know the place, not proving you did homework.
        Research note: "your regular student mashup club night 'Club Sandwich' hosted
        by DJ Shinzee remains a legendary campus institution at RISE, famously offering
        entry and three drinks for under a tenner"
        Bad  (verbatim): "I saw that your regular student mashup club night 'Club
        Sandwich' hosted by DJ Shinzee remains a legendary campus institution at RISE,
        famously offering entry and three drinks for under a tenner."
        Good (in your voice): "Club Sandwich has been a fixture at RISE for years —"
  - If the research has no hook, open with something honest and plain instead. Never
    invent one, and never fake familiarity you do not have.
  - Use only the proof points given to you. Do not invent numbers, venues or credentials.
  - Ask for one thing: a short call. Do not ask for a decision.
  - Keep sentences short. If a sentence runs past about 25 words, split it.
  - Obey the word limit and the banned phrases absolutely.
  - British English. Plain text. No markdown, no bullet lists, no emoji.
  - Do not write a signature block or sign-off name — that is added afterwards.
  - Do not mention a fee. The first email opens a conversation, it does not quote.

If past human edits are shown to you, they are the strongest signal you have about how
this person actually writes. Match that voice over any general instinct about what a
good cold email looks like.
"""

writer_agent = LlmAgent(
    name="writer",
    model=GEMINI_MODEL,
    description="Writes a personalised pitch email from research. Has no tools by design.",
    instruction=INSTRUCTION,
    output_schema=PitchDraft,
)


def _format_decisions(decisions: list[DecisionDoc], *, limit: int = 5) -> str:
    """Show the Writer how its drafts were changed, not just that they were.

    Only edits are included. An approval says "this was fine" and carries no signal
    about what to change; an edit is a worked example of the gap between what the agent
    wrote and what the human actually wanted.
    """
    edits = [d for d in decisions if DecisionKind(d.kind) == DecisionKind.EDITED][:limit]
    if not edits:
        return "(no past edits yet — this is an early draft, write it straight)"

    blocks = []
    for i, decision in enumerate(edits, start=1):
        blocks.append(
            f"--- Edit {i} ---\n"
            f"You wrote:\n{decision.draft_before.strip()}\n\n"
            f"They changed it to:\n{decision.draft_after.strip()}"
        )
    return "\n\n".join(blocks)


def build_prompt(
    *,
    target: TargetDoc,
    research: ResearchDoc | None,
    config: AppConfig,
    decisions: list[DecisionDoc] | None = None,
    style_memo: str = "",
) -> str:
    brand = config.brand
    rules = brand.copy_rules

    hook = (research.best_hook if research else "") or ""
    research_block = "(no research available)"
    if research:
        research_block = "\n".join(
            filter(
                None,
                [
                    f"Venue: {research.venue_name} {research.venue_capacity}".strip(),
                    f"Who runs the venue: {research.venue_ownership}"
                    if research.venue_ownership
                    else "",
                    f"Their existing programme: {research.events_programme}"
                    if research.events_programme
                    else "",
                    f"Freshers/Refreshers timing: {research.freshers_timing}"
                    if research.freshers_timing
                    else "",
                    f"Notes: {research.notes}" if research.notes else "",
                    f"Research confidence: {research.confidence}",
                ],
            )
        )

    greeting_note = (
        f"Address them as {target.contact_name}."
        if target.contact_name
        else "You do not have a named contact. Do not guess a name; open without one."
    )

    return f"""\
WHO YOU ARE
{brand.company_name} — {brand.sender_name}, {brand.sender_role}.

WHAT WE DO
{brand.pitch}

PROOF POINTS (use only these, do not invent more)
{chr(10).join("- " + p for p in brand.proof_points)}

TONE
{brand.tone_notes}

WHO YOU ARE WRITING TO
{target.organisation}
{greeting_note}
{f"What we already know: {target.venue_notes}" if target.venue_notes else ""}
{f"Existing context: {target.context}" if target.context else ""}

RESEARCH
{research_block}

THE HOOK TO OPEN WITH
{hook if hook else "(none found — open honestly and plainly, do not fake familiarity)"}

HOW THIS PERSON WRITES (learned from their edits to your past drafts)
{style_memo or "(no style memo yet)"}

{_format_decisions(decisions or [])}

HARD LIMITS
- Maximum {rules.max_words} words in the body.
- Never use these phrases: {", ".join(rules.banned_phrases)}
- Links you may include: {brand.links.website}

Write the email.
"""


class DraftRejected(ValueError):
    """The draft broke a hard copy rule and must not be shown as if it were fine."""


def validate_draft(draft: PitchDraft, config: AppConfig) -> list[str]:
    """Check a draft against the copy rules. Returns a list of problems, empty if clean.

    Checked in code rather than trusted to the prompt: "never use this phrase" is a
    rule a language model will obey most of the time, and most of the time is not good
    enough for something with the founder's name on it.
    """
    rules = config.brand.copy_rules
    problems: list[str] = []

    words = len(draft.body.split())
    if words > rules.max_words:
        problems.append(f"body is {words} words, limit is {rules.max_words}")

    lowered = draft.body.lower()
    for phrase in rules.banned_phrases:
        if phrase.lower() in lowered:
            problems.append(f"contains banned phrase: {phrase!r}")

    if not draft.subject.strip():
        problems.append("subject is empty")
    if not draft.body.strip():
        problems.append("body is empty")

    # A pitch that quotes a fee has jumped a step the policy does not allow yet.
    if any(token in lowered for token in ("£", "gbp", "our fee", "the fee is")):
        problems.append("first-contact email must not quote a fee")

    return problems


async def write_pitch(
    *,
    target: TargetDoc,
    research: ResearchDoc | None,
    config: AppConfig,
    decisions: list[DecisionDoc] | None = None,
    style_memo: str = "",
) -> tuple[PitchDraft, list[str]]:
    """Draft a pitch. Returns the draft and any copy-rule problems found."""
    prompt = build_prompt(
        target=target, research=research, config=config, decisions=decisions, style_memo=style_memo
    )
    raw = await run_agent(writer_agent, prompt)
    draft = PitchDraft.model_validate_json(raw)
    problems = validate_draft(draft, config)

    log.info(
        "pitch drafted",
        extra={
            "target_id": target.target_id,
            "words": len(draft.body.split()),
            "problems": len(problems),
        },
    )
    return draft, problems
