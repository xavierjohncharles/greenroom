"""Model IDs. One place, so a Stage One stack check can read a single file.

Verified against the live docs on 2026-08-29:
  https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash
  https://docs.cloud.google.com/vertex-ai/generative-ai/docs/models
  https://docs.cloud.google.com/vertex-ai/generative-ai/docs/image/generate-images
"""

from typing import Final

# --- Text -------------------------------------------------------------------
# Mandated by the hackathon rules. Newer Flash models (3.7, 3.6) are stable as of
# Aug 2026; we are on 3.5 deliberately, not because this constant went stale.
# The ID string is identical on the Gemini API and on Vertex AI.
GEMINI_MODEL: Final[str] = "gemini-3.5-flash"

# --- Images (bonus) ---------------------------------------------------------
# Google's own migration table flags every imagen-* endpoint as deprecated with a
# recommended migration date of 2026-06-30, pointing at the Gemini image family.
# The hackathon bonus names Imagen specifically, so we try Imagen first and fall
# back automatically if the endpoint no longer serves.
# https://docs.cloud.google.com/vertex-ai/generative-ai/docs/image/set-output-resolution
IMAGEN_MODEL: Final[str] = "imagen-4.0-generate-001"
IMAGE_FALLBACK_MODEL: Final[str] = "gemini-3.1-flash-image"

# Imagen supports 1:1, 3:4, 4:3, 16:9, 9:16 — there is no 4:5. The 1080x1350 poster
# is generated at 3:4 and centre-cropped.
POSTER_ASPECT_RATIO: Final[str] = "3:4"
POSTER_RESOLUTION: Final[str] = "2K"
POSTER_SIZE_PX: Final[tuple[int, int]] = (1080, 1350)

# --- Monday stretch goals ---------------------------------------------------
VEO_MODEL: Final[str] = "veo-3.1"
TTS_MODEL: Final[str] = "gemini-2.5-flash-tts"  # stable; 3.1-flash-tts is preview only
