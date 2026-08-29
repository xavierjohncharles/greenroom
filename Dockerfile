# Greenroom container for Cloud Run.
# https://docs.cloud.google.com/run/docs/container-contract
#   - must listen on $PORT (default 8080) on 0.0.0.0
#   - no local Docker needed to build this: `gcloud run deploy --source .` uses Cloud Build
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

# Dependencies first so a code-only change reuses the cached layer.
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

# Config is read at runtime and is not a secret.
COPY config/ ./config/

# Run as non-root.
RUN useradd --create-home --uid 1000 greenroom && chown -R greenroom:greenroom /app
USER greenroom

EXPOSE 8080

# Single worker: Firestore transactions and the job queue provide concurrency safety,
# and Cloud Run scales by adding instances rather than in-process workers.
CMD exec uvicorn greenroom.web.main:app --host 0.0.0.0 --port ${PORT} --workers 1
