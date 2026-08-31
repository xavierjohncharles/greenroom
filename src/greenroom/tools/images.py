"""Poster generation: Gemini image model → centre-crop → Cloud Storage.

https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/image-generation

Two findings from probing this project directly, both recorded in `models.py`:

  * Imagen is retired. Every `imagen-*` endpoint 404s. The successor is the Gemini
    image family, which is what Google's own deprecation table points at.
  * Image models serve from the `global` endpoint only. europe-west2, where the rest
    of Greenroom runs, has none — so this tool uses its own client rather than the
    shared one.

The prompt lives in `config/poster_prompt.py` and nowhere else.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

from greenroom.models import (
    IMAGE_FALLBACK_MODEL,
    IMAGE_LOCATION,
    IMAGE_MODEL,
    POSTER_ASPECT_RATIO,
    POSTER_SIZE_PX,
)
from greenroom.obs import get_logger, tool_span
from greenroom.settings import get_settings

log = get_logger(__name__)


class RateLimited(RuntimeError):
    """The image model is out of quota right now.

    Distinct from a failure on purpose. A 429 means "not now", and treating it as a
    failure burns a retry attempt against a job that was never broken — five quota
    blips and a perfectly good poster job is dead. Same reasoning as a send blocked by
    the send window in the Scheduler.
    """


@dataclass(frozen=True)
class Poster:
    png: bytes
    model: str
    gcs_uri: str = ""
    public_url: str = ""
    dry_run: bool = False

    @property
    def filename(self) -> str:
        return "beatid-poster.png"


def _load_prompt_builder():
    """Import the prompt module by path, so it can live in config/ next to the YAML."""
    import importlib.util

    for candidate in (
        Path.cwd() / "config" / "poster_prompt.py",
        Path(__file__).resolve().parents[3] / "config" / "poster_prompt.py",
    ):
        if candidate.exists():
            spec = importlib.util.spec_from_file_location("poster_prompt", candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module.build
    raise RuntimeError("config/poster_prompt.py not found")


def _generate(prompt: str) -> tuple[bytes, str]:
    """Ask for an image, falling back one model if the flagship is unavailable."""
    from google import genai
    from google.genai import types

    settings = get_settings()
    client = genai.Client(
        vertexai=True, project=settings.google_cloud_project, location=IMAGE_LOCATION
    )

    last_error: Exception | None = None
    rate_limited = False
    for model in (IMAGE_MODEL, IMAGE_FALLBACK_MODEL):
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE"],
                    image_config=types.ImageConfig(aspect_ratio=POSTER_ASPECT_RATIO),
                ),
            )
            for candidate in response.candidates or []:
                for part in candidate.content.parts or []:
                    if getattr(part, "inline_data", None) and part.inline_data.data:
                        log.info("poster generated", extra={"model": model})
                        return part.inline_data.data, model
            last_error = RuntimeError(f"{model} returned no image part")
        except Exception as exc:
            text = str(exc)
            if "429" in text or "RESOURCE_EXHAUSTED" in text or "quota" in text.lower():
                # Try the next model — quotas are per-model, so the flagship being busy
                # does not mean the fallback is.
                rate_limited = True
                log.info("image model out of quota", extra={"model": model})
            else:
                log.warning("image model failed", extra={"model": model, "error": text[:200]})
            last_error = exc

    if rate_limited:
        raise RateLimited(f"every image model is rate limited: {last_error}")
    raise RuntimeError(f"no image model produced a poster: {last_error}")


def _crop_to_poster(data: bytes) -> bytes:
    """Centre-crop to exactly 1080x1350.

    No model offers 4:5 directly, so the image is generated at 3:4 and trimmed. Cropping
    from the centre rather than resizing keeps the type crisp — which is why the prompt
    asks for a clean margin on all four sides.
    """
    from PIL import Image

    target_w, target_h = POSTER_SIZE_PX
    image = Image.open(io.BytesIO(data)).convert("RGB")

    target_ratio = target_w / target_h
    width, height = image.size
    if width / height > target_ratio:
        new_width = int(height * target_ratio)
        left = (width - new_width) // 2
        image = image.crop((left, 0, left + new_width, height))
    else:
        new_height = int(width / target_ratio)
        top = (height - new_height) // 2
        image = image.crop((0, top, width, top + new_height))

    image = image.resize((target_w, target_h), Image.LANCZOS)
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _upload(png: bytes, *, target_id: str) -> tuple[str, str]:
    """Store the poster in Cloud Storage. Returns (gs:// uri, https url)."""
    from google.cloud import storage

    settings = get_settings()
    bucket_name = settings.poster_bucket
    if not bucket_name:
        return "", ""

    client = storage.Client(project=settings.google_cloud_project)
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(f"posters/{target_id}.png")
    blob.upload_from_string(png, content_type="image/png")
    log.info("poster stored", extra={"target_id": target_id, "bytes": len(png)})
    return f"gs://{bucket_name}/{blob.name}", blob.public_url


def make_poster(
    *,
    target_id: str,
    organisation: str,
    venue: str = "",
    date_line: str = "",
    dry_run: bool = False,
) -> Poster:
    """Generate, crop and store a poster for one target."""
    prompt = _load_prompt_builder()(
        organisation=organisation, venue=venue, date_line=date_line
    )

    if dry_run:
        log.info("dry-run: would generate poster", extra={"target_id": target_id})
        return Poster(png=b"", model=IMAGE_MODEL, dry_run=True)

    with tool_span("image.generate", target_id=target_id, organisation=organisation) as span:
        raw, model = _generate(prompt)
        png = _crop_to_poster(raw)
        gcs_uri, public_url = _upload(png, target_id=target_id)
        span.summarise(model=model, bytes=len(png), uri=gcs_uri)
    return Poster(png=png, model=model, gcs_uri=gcs_uri, public_url=public_url)
