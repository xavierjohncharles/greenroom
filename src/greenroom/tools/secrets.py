"""Secret Manager access. The only place credentials enter the process.

https://docs.cloud.google.com/secret-manager/docs/create-secret-quickstart
https://docs.cloud.google.com/secret-manager/docs/access-control

Nothing here writes a secret to disk or logs a secret value. Values are cached in
memory for the life of the container so a hot path does not hit Secret Manager on
every send, and the cache can be cleared if a token is rotated mid-run.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from greenroom.obs import get_logger
from greenroom.settings import get_settings

log = get_logger(__name__)


class SecretNotFound(RuntimeError):
    """Raised when a required secret is absent, with the exact gcloud fix in the message."""


@lru_cache(maxsize=8)
def get_secret(name: str, version: str = "latest") -> str:
    """Fetch a secret's payload as text. Cached per (name, version).

    `name` is the short secret id, not the full resource path.
    """
    from google.api_core import exceptions as gcp_exc
    from google.cloud import secretmanager

    settings = get_settings()
    if not settings.google_cloud_project:
        raise SecretNotFound("GOOGLE_CLOUD_PROJECT is not set — cannot reach Secret Manager")

    client = secretmanager.SecretManagerServiceClient()
    path = f"projects/{settings.google_cloud_project}/secrets/{name}/versions/{version}"
    try:
        response = client.access_secret_version(request={"name": path})
    except gcp_exc.NotFound as exc:
        raise SecretNotFound(
            f"secret {name!r} not found in project {settings.google_cloud_project!r}. "
            f"Create it with:\n"
            f"  gcloud secrets create {name} --replication-policy=automatic\n"
            f"  echo -n '<value>' | gcloud secrets versions add {name} --data-file=-"
        ) from exc
    except gcp_exc.PermissionDenied as exc:
        raise SecretNotFound(
            f"permission denied reading secret {name!r}. The Cloud Run service account "
            f"needs roles/secretmanager.secretAccessor on it."
        ) from exc

    # Never log the payload — only that a fetch happened and how big it was.
    payload = response.payload.data.decode("utf-8")
    log.info("secret fetched", extra={"secret": name, "version": version, "bytes": len(payload)})
    return payload


def get_secret_json(name: str, version: str = "latest") -> dict[str, Any]:
    raw = get_secret(name, version)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SecretNotFound(f"secret {name!r} is not valid JSON: {exc}") from exc


def clear_cache() -> None:
    """Drop cached secrets. Call after rotating a token."""
    get_secret.cache_clear()
