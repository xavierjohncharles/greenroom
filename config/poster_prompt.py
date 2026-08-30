"""The poster prompt, in one file so it can be tuned without touching any other code.

Edit BASE_PROMPT and re-run; nothing else needs to change. The brand palette and the
copy rules live here too, because a poster prompt is really a design brief and splitting
it across files makes it impossible to see what you are actually asking for.
"""

from __future__ import annotations

# Beat ID brand palette.
MAGENTA = "#D30D4F"
CYAN = "#22D3EE"
CREAM = "#FAF8F2"
INK = "#14121A"

BASE_PROMPT = """\
A bold, modern event poster for a live music quiz night at a UK university students' union.

STRICT COLOUR PALETTE — use only these four:
  deep magenta {magenta}, electric cyan {cyan}, warm cream {cream}, near-black ink {ink}.
Cream is the dominant background. Magenta is the primary accent. Cyan is used sparingly,
for emphasis only. No other colours, no gradients into unrelated hues.

STYLE
Flat vector graphic design, high contrast, generous negative space. Bold geometric
shapes suggesting sound: concentric arcs, waveform bars, a stylised vinyl or speaker
form. Swiss/International poster influence — confident type, strong grid, nothing
cluttered. Print-quality. Absolutely no photographic elements and no human faces.

TEXT — render these exactly, spelled exactly as written, and nothing else:
  "{headline}"
  "{venue_line}"
  "{date_line}"
The headline is the largest element and must be immediately legible. Do not invent
additional words, taglines, prices, times, sponsor logos or social handles. Do not add
lorem ipsum. If you cannot render a word legibly, render it larger rather than
substituting a different word.

COMPOSITION
Vertical portrait poster. Graphic element in the upper two thirds, text block below it.
Leave a generous margin on all four sides, and at least 8% clear space below the last
line of text. The image is centre-cropped from 3:4 to 4:5 after generation, which trims
the top and bottom, so text sitting near an edge will be cut. Keep every word well
inside the frame.
"""


DEFAULT_DATE_LINE = "FRESHERS 2026"
MAX_VENUE_CHARS = 40
MAX_DATE_CHARS = 26


def tidy_date_line(raw: str) -> str:
    """Turn the Researcher's prose into a poster-safe line, or give up cleanly.

    The Researcher returns sentences like "Welcome Week 2026 runs from September 18th
    through late September." Truncating that to fit produced "WELCOME WEEK STARTS ON
    SEPTEMBER" on a real poster — grammatical wreckage printed in 40pt type.

    So: use it only if it already fits, otherwise fall back. A generic line that reads
    correctly beats a specific one that reads as broken, and there is no third option
    that does not involve guessing at someone's calendar.
    """
    cleaned = " ".join((raw or "").split()).rstrip(".")
    if not cleaned:
        return DEFAULT_DATE_LINE
    if len(cleaned) <= MAX_DATE_CHARS:
        return cleaned
    # A leading fragment up to a natural break, if that alone fits and stands alone.
    for sep in (" runs ", " from ", " starts ", ",", " — ", " - "):
        head = cleaned.split(sep)[0].strip()
        if head and len(head) <= MAX_DATE_CHARS and len(head.split()) >= 2:
            return head
    return DEFAULT_DATE_LINE


def build(*, organisation: str, venue: str = "", date_line: str = "") -> str:
    """Compose the prompt for one target.

    `venue` falls back to the organisation name: a poster naming a room nobody
    recognises is worse than one naming the union.
    """
    return BASE_PROMPT.format(
        magenta=MAGENTA,
        cyan=CYAN,
        cream=CREAM,
        ink=INK,
        headline="BEAT ID — GUESS THE SONG",
        venue_line=(venue or organisation).upper()[:MAX_VENUE_CHARS],
        date_line=tidy_date_line(date_line).upper(),
    )
