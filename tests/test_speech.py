"""Brief audio: the WAV wrapping, and making written text read aloud properly."""

from __future__ import annotations

import io
import wave

import pytest

from greenroom.models import TTS_SAMPLE_RATE
from greenroom.tools.speech import MAX_CHARS, pcm_to_wav, synthesise, to_spoken

# ------------------------------------------------------------------ wav wrapping


def test_raw_pcm_becomes_a_playable_wav():
    """The API returns audio/L16 with no container. A browser will not play that, and
    the failure mode is a silent audio element rather than an error."""
    wav = pcm_to_wav(b"\x00\x01" * 12000)
    assert wav[:4] == b"RIFF"
    assert wav[8:12] == b"WAVE"


def test_the_wav_header_describes_the_audio_correctly():
    with wave.open(io.BytesIO(pcm_to_wav(b"\x00\x01" * 24000)), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2  # 16-bit
        assert w.getframerate() == TTS_SAMPLE_RATE


def test_empty_pcm_still_produces_a_valid_container():
    """Better a zero-length WAV than a corrupt file the browser chokes on."""
    assert pcm_to_wav(b"")[:4] == b"RIFF"


# ------------------------------------------------------------------ spoken text


@pytest.mark.parametrize(
    "written,must_contain,must_not_contain",
    [
        ("They offered GBP 500", "500", "GBP"),
        ("below fee.floor today", "the fee floor", "fee.floor"),
        ("breaches escalate.max_attendees", "the attendee limit", "escalate.max"),
        ("breaches escalate.exclusivity", "a policy rule", "escalate.exclusivity"),
        ("The SU replied", "S U", "SU replied"),
        ("They offered £850", "850", "£"),
    ],
)
def test_written_shorthand_is_turned_into_speech(written, must_contain, must_not_contain):
    """A TTS model reads 'GBP' as three letters and 'fee.floor' as punctuation. Small
    substitutions, but the difference between a brief you listen to and one you turn off."""
    spoken = to_spoken(written)
    assert must_contain in spoken
    assert must_not_contain not in spoken


def test_whitespace_is_collapsed():
    assert to_spoken("one\n\n  two\t three") == "one two three"


def test_absurdly_long_input_is_truncated():
    """A brief is a few sentences. Anything longer means something upstream broke."""
    assert len(to_spoken("word " * 5000)) <= MAX_CHARS


def test_empty_input_is_survivable():
    assert to_spoken("") == ""
    assert to_spoken(None) == ""


# ------------------------------------------------------------------ dry run


def test_dry_run_synthesises_nothing():
    speech = synthesise("Good morning.", dry_run=True)
    assert speech.dry_run is True
    assert speech.wav == b""
