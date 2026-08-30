"""Read the morning brief aloud.

https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/speech/text-to-speech

The API returns raw PCM — `audio/L16`, 24 kHz, mono — with no container. A browser will
not play that, so it is wrapped in a WAV header here. Nothing in the docs is wrong about
this; it is simply not mentioned, and the failure mode is a silent audio element.

Audio is generated once per brief and stored in Cloud Storage next to the posters. It is
regenerated whenever the brief is, which is once a day.
"""

from __future__ import annotations

import io
import re
import wave
from dataclasses import dataclass

from greenroom.models import TTS_LOCATION, TTS_MODEL, TTS_SAMPLE_RATE, TTS_VOICE
from greenroom.obs import get_logger
from greenroom.settings import get_settings

log = get_logger(__name__)

# A brief is a few sentences. Anything longer is a sign something upstream has gone
# wrong, and synthesising it would be slow and expensive for no benefit.
MAX_CHARS = 2000


def to_spoken(text: str) -> str:
    """Make the brief read naturally aloud.

    Written text and spoken text differ in ways a TTS model will not fix for you: it
    reads "GBP 850" as three letters and a number, and it reads a bare "3" mid-sentence
    without the emphasis a person would give it. Small substitutions, but the difference
    between a brief you listen to and one you turn off.
    """
    spoken = " ".join((text or "").split())
    spoken = spoken.replace("£", "").replace("GBP ", "")
    spoken = re.sub(r"\bfee\.floor\b", "the fee floor", spoken)
    spoken = re.sub(r"\bescalate\.max_attendees\b", "the attendee limit", spoken)
    spoken = re.sub(r"\bescalate\.\w+\b", "a policy rule", spoken)
    spoken = re.sub(r"\bSU\b", "S U", spoken)
    return spoken[:MAX_CHARS]


def pcm_to_wav(pcm: bytes, *, sample_rate: int = TTS_SAMPLE_RATE) -> bytes:
    """Wrap raw 16-bit mono PCM in a WAV container."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)  # 16-bit
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


@dataclass(frozen=True)
class Speech:
    wav: bytes
    model: str
    voice: str
    gcs_uri: str = ""
    public_url: str = ""
    dry_run: bool = False


def synthesise(text: str, *, dry_run: bool = False) -> Speech:
    """Turn text into a playable WAV. Raises if no audio came back."""
    if dry_run:
        log.info("dry-run: would synthesise speech", extra={"chars": len(text)})
        return Speech(wav=b"", model=TTS_MODEL, voice=TTS_VOICE, dry_run=True)

    from google import genai
    from google.genai import types

    settings = get_settings()
    client = genai.Client(
        vertexai=True, project=settings.google_cloud_project, location=TTS_LOCATION
    )

    response = client.models.generate_content(
        model=TTS_MODEL,
        contents=to_spoken(text),
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=TTS_VOICE)
                )
            ),
        ),
    )

    pcm = b"".join(
        part.inline_data.data
        for candidate in (response.candidates or [])
        for part in (candidate.content.parts or [])
        if getattr(part, "inline_data", None) and part.inline_data.data
    )
    if not pcm:
        raise RuntimeError(f"{TTS_MODEL} returned no audio")

    wav = pcm_to_wav(pcm)
    log.info("brief synthesised", extra={"model": TTS_MODEL, "bytes": len(wav)})
    return Speech(wav=wav, model=TTS_MODEL, voice=TTS_VOICE)


def store(speech: Speech, *, name: str) -> tuple[str, str]:
    """Put the audio in Cloud Storage. Returns (gs:// uri, https url)."""
    from google.cloud import storage

    settings = get_settings()
    if not settings.poster_bucket or not speech.wav:
        return "", ""

    client = storage.Client(project=settings.google_cloud_project)
    blob = client.bucket(settings.poster_bucket).blob(f"briefs/{name}.wav")
    blob.upload_from_string(speech.wav, content_type="audio/wav")
    return f"gs://{settings.poster_bucket}/{blob.name}", blob.public_url
