"""The style memo: what the agent has learned about how Xavier actually writes.

Regenerated from the diffs between what the Writer produced and what the human sent.
Approvals carry no signal — they only say "this was fine" — so the memo is built from
edits alone, which are worked examples of the gap between the two.

Why a memo rather than just feeding raw diffs to the Writer: diffs accumulate, and a
prompt that grows without bound eventually costs more than it teaches and buries the
recent signal under the old. The memo is a fixed-size summary that gets *rewritten*
each time, so twenty edits and two hundred edits produce a prompt of the same length.
It also degrades gracefully — a bad memo is one bad paragraph, not a corrupted history.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent

from greenroom.agents.runtime import run_agent
from greenroom.models import GEMINI_MODEL
from greenroom.obs import get_logger
from greenroom.state.models import DecisionDoc, DecisionKind, utcnow

log = get_logger(__name__)

STYLE_MEMO_DOC = "style_memo"

# Below this, the memo would be generalising from noise. One person's single edit says
# more about that email than about how they write.
MIN_EDITS = 2

INSTRUCTION = """\
You study how one specific person edits cold outreach emails, and write a short brief
for the writer who drafts them.

You are shown pairs: what the agent wrote, and what the person changed it to. Work out
what they consistently prefer — length, structure, formality, how they open, how they
ask for a call, what they cut, what they add.

Write at most 8 short bullet points, in the imperative, addressed to the writer:
  - "Open with the hook in one clause, not a full sentence."
  - "Cut any sentence explaining why you are emailing."

Rules:
  - Only claim a pattern you can see in at least two edits, unless a single edit is
    unmistakable (a phrase deleted every time it appears).
  - Be specific. "Be more concise" is useless; "cut the closing paragraph to one
    sentence" is actionable.
  - If the edits show no consistent pattern, say exactly that in one line rather than
    inventing a preference. A confident wrong memo is worse than an empty one, because
    the writer will follow it.
  - Do not restate the brand or the product. Only how this person writes.
"""

style_agent = LlmAgent(
    name="style_memo",
    model=GEMINI_MODEL,
    description="Summarises how the human edits drafts, into a short brief for the Writer.",
    instruction=INSTRUCTION,
)


def _format_edits(decisions: list[DecisionDoc], *, limit: int = 12) -> str:
    edits = [
        d
        for d in decisions
        if DecisionKind(d.kind) == DecisionKind.EDITED
        and d.draft_before.strip()
        and d.draft_after.strip()
        and d.draft_before.strip() != d.draft_after.strip()
    ][:limit]

    blocks = []
    for i, d in enumerate(edits, start=1):
        blocks.append(
            f"--- Edit {i} ---\nAGENT WROTE:\n{d.draft_before.strip()}\n\n"
            f"THEY SENT:\n{d.draft_after.strip()}"
        )
    return "\n\n".join(blocks), len(edits)


async def regenerate(repo) -> str | None:
    """Rebuild the style memo from recent edits. Returns the memo, or None if too few.

    Deliberately returns None rather than an empty memo below the threshold: an absent
    memo makes the Writer fall back to the brand tone notes, which is the right default
    before anything has been learned.
    """
    decisions = repo.recent_decisions(limit=40)
    formatted, count = _format_edits(decisions)

    if count < MIN_EDITS:
        log.info("not enough edits for a style memo", extra={"edits": count, "needed": MIN_EDITS})
        return None

    memo = (
        await run_agent(
            style_agent,
            f"Here are {count} edits this person made to drafted emails.\n\n{formatted}\n\n"
            "Write the brief.",
        )
    ).strip()

    repo._col("control").document(STYLE_MEMO_DOC).set(
        {
            "memo": memo,
            "edits_used": count,
            "updated_at": utcnow(),
        }
    )
    log.info("style memo regenerated", extra={"edits": count, "chars": len(memo)})
    return memo


def load(repo) -> str:
    snapshot = repo._col("control").document(STYLE_MEMO_DOC).get()
    return str((snapshot.to_dict() or {}).get("memo", "")) if snapshot.exists else ""
