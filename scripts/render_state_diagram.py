"""Regenerate the state diagram in README.md from the transition table itself.

Run via `make diagram`. The diagram is generated rather than hand-drawn so it can never
drift from the code it documents — a stale architecture diagram is worse than none.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from greenroom.state.machine import mermaid_diagram  # noqa: E402

START = "<!-- STATE-DIAGRAM:START -->"
END = "<!-- STATE-DIAGRAM:END -->"


def main() -> int:
    readme = Path(__file__).resolve().parents[1] / "README.md"
    text = readme.read_text(encoding="utf-8")
    block = f"{START}\n\n```mermaid\n{mermaid_diagram()}\n```\n\n{END}"

    if START not in text:
        print(f"marker {START} not found in README.md", file=sys.stderr)
        return 1

    updated = re.sub(re.escape(START) + r".*?" + re.escape(END), block, text, flags=re.DOTALL)
    readme.write_text(updated, encoding="utf-8")
    print("README state diagram regenerated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
