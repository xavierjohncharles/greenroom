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

# --- Images -----------------------------------------------------------------
# IMAGEN IS RETIRED. Verified on 2026-08-30 against this project: every imagen-*
# endpoint returns 404 NOT_FOUND in every region tested (global, us-central1,
# europe-west2). Google's deprecation notice gave a migration date of 2026-06-30 and
# the endpoints are now actually switched off, not merely discouraged. The successor
# is the Gemini image family, which is what the deprecation table points at.
#
# The hackathon bonus names Imagen by name; we use Google's current image model
# instead because Imagen no longer exists to be used. Documented in the README so a
# judge is not left wondering.
#
# Availability is also regional in a way worth recording: image models serve from the
# `global` endpoint only. europe-west2, where the rest of Greenroom runs, has none.
IMAGE_MODEL: Final[str] = "gemini-3-pro-image"  # "Nano Banana Pro" — best at text
IMAGE_FALLBACK_MODEL: Final[str] = "gemini-3.1-flash-image"
IMAGE_LOCATION: Final[str] = "global"

# A poster carries a venue name and a date, so text fidelity matters more than latency.
# 1080x1350 is 4:5, which no model offers directly; generated at 3:4 and centre-cropped.
POSTER_ASPECT_RATIO: Final[str] = "3:4"
POSTER_SIZE_PX: Final[tuple[int, int]] = (1080, 1350)

# --- Monday stretch goals ---------------------------------------------------
VEO_MODEL: Final[str] = "veo-3.1"
TTS_MODEL: Final[str] = "gemini-2.5-flash-tts"  # stable; 3.1-flash-tts is preview only
