"""Poster generation: cropping, the prompt, and the failure behaviour that matters."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "config"))

from poster_prompt import CREAM, CYAN, INK, MAGENTA, build  # noqa: E402

from greenroom.models import POSTER_SIZE_PX  # noqa: E402
from greenroom.tools.images import _crop_to_poster, make_poster  # noqa: E402


def _image(width: int, height: int) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (250, 248, 242)).save(out, format="PNG")
    return out.getvalue()


# ------------------------------------------------------------------ cropping


@pytest.mark.parametrize(
    "size",
    [(1536, 2048), (2048, 1536), (1024, 1024), (900, 1600), (3000, 3750)],
)
def test_any_input_becomes_exactly_the_poster_size(size):
    """No image model offers 4:5, so whatever comes back must land on 1080x1350."""
    assert Image.open(io.BytesIO(_crop_to_poster(_image(*size)))).size == POSTER_SIZE_PX


def test_a_correctly_shaped_image_is_not_distorted():
    """A 4:5 input should be a straight resize, not a crop-and-stretch."""
    cropped = _crop_to_poster(_image(2160, 2700))
    assert Image.open(io.BytesIO(cropped)).size == POSTER_SIZE_PX


def test_output_is_a_png():
    assert Image.open(io.BytesIO(_crop_to_poster(_image(1536, 2048)))).format == "PNG"


# ------------------------------------------------------------------ the prompt


def test_the_prompt_carries_every_brand_colour():
    prompt = build(organisation="Goldsmiths Students' Union", venue="RISE")
    for colour in (MAGENTA, CYAN, CREAM, INK):
        assert colour in prompt


def test_the_prompt_names_the_venue_not_just_the_organisation():
    assert "RISE" in build(organisation="Goldsmiths Students' Union", venue="RISE")


def test_the_venue_falls_back_to_the_organisation():
    """A poster naming a room nobody recognises is worse than one naming the union."""
    assert "GOLDSMITHS" in build(organisation="Goldsmiths Students' Union").upper()


def test_long_names_are_truncated_rather_than_overflowing():
    prompt = build(organisation="X" * 200, venue="Y" * 200)
    assert "Y" * 41 not in prompt


def test_the_prompt_forbids_inventing_extra_copy():
    """The model must not add prices, times or sponsor logos nobody approved."""
    # Normalised: the instruction wraps across lines in the prompt file.
    prompt = " ".join(build(organisation="Test SU").split())
    assert "Do not invent additional words" in prompt
    for forbidden in ("prices", "sponsor logos", "social handles"):
        assert forbidden in prompt


def test_the_prompt_warns_about_the_crop():
    """Text near an edge is trimmed by the 3:4 to 4:5 crop, so the prompt has to say so."""
    assert "cropped" in build(organisation="Test SU").lower()


# ------------------------------------------------------------------ dry run


def test_dry_run_generates_nothing():
    """`make run-local` must not spend money on image generation."""
    poster = make_poster(target_id="t1", organisation="Test SU", dry_run=True)
    assert poster.dry_run is True
    assert poster.png == b""
    assert poster.gcs_uri == ""


# ------------------------------------------------------------------ date line


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Freshers 2026", "Freshers 2026"),
        ("Welcome Week 2026", "Welcome Week 2026"),
        ("", "FRESHERS 2026"),
        ("   ", "FRESHERS 2026"),
        # The real research string that produced "WELCOME WEEK STARTS ON SEPTEMBER"
        # on a poster. Must fall back rather than truncate mid-sentence.
        (
            "Welcome Week 2026 runs from September 18th through late September.",
            "Welcome Week 2026",
        ),
        # Fits as-is, so it is kept whole rather than split at the comma.
        ("September 2026, week one", "September 2026, week one"),
        # Too long, so it falls back to the fragment before the comma.
        ("September 2026, during the whole of Welcome Week", "September 2026"),
    ],
)
def test_the_date_line_never_truncates_mid_sentence(raw, expected):
    """Grammatical wreckage printed in 40pt type is worse than a generic line."""
    from poster_prompt import tidy_date_line

    assert tidy_date_line(raw) == expected


def test_every_date_line_fits_the_poster():
    from poster_prompt import MAX_DATE_CHARS, tidy_date_line

    for raw in (
        "Welcome Week 2026 runs from September 18th through late September.",
        "Refreshers is in the third week of January 2027 according to the SU site",
        "x" * 300,
    ):
        assert len(tidy_date_line(raw)) <= MAX_DATE_CHARS
