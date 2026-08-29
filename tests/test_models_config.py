"""Stage One guard: the mandatory stack constants must not drift.

The hackathon's pass/fail check is that the mandated model and framework are actually
used. These assertions make an accidental edit fail CI rather than fail the entry.
"""

from __future__ import annotations

import importlib.metadata

from greenroom.models import GEMINI_MODEL, POSTER_SIZE_PX


def test_gemini_model_is_the_mandated_one():
    assert GEMINI_MODEL == "gemini-3.5-flash"


def test_adk_is_installed_and_current():
    version = importlib.metadata.version("google-adk")
    assert version.startswith("2."), f"ADK 2.x is required, found {version}"


def test_poster_is_the_size_the_brief_asks_for():
    assert POSTER_SIZE_PX == (1080, 1350)
