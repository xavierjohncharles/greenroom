"""OAuth credentials for the agent mailbox, built from a refresh token in Secret Manager.

https://developers.google.com/identity/protocols/oauth2/web-server#offline
https://developers.google.com/workspace/gmail/api/auth/scopes
https://developers.google.com/workspace/calendar/api/auth

Why a user OAuth client and not a service account with domain-wide delegation:
DWD would grant Greenroom access to *every* mailbox in the Workspace domain and is
configured at the org level, which is a far larger blast radius than this needs. A
single user-consented refresh token for admin@beatidapp.com can only ever reach that
one mailbox. Smaller grant, and revocable by Xavier alone without an admin console.

The refresh token is never written to disk and never appears in a log line.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from greenroom.obs import get_logger
from greenroom.settings import get_settings
from greenroom.tools.secrets import get_secret_json

if TYPE_CHECKING:
    from google.oauth2.credentials import Credentials

log = get_logger(__name__)

TOKEN_URI = "https://oauth2.googleapis.com/token"

# --- Scopes -----------------------------------------------------------------
# The narrowest set that satisfies the mailbox rules. Documented in the README.
#
# gmail.modify is unavoidable: applying a label to a thread requires it. The
# `gmail.labels` scope only manages label *definitions* and cannot attach one, so
# `readonly` + `labels` does not work. gmail.modify cannot delete mail.
# Containment is enforced structurally in tools/gmail.py, not by the scope.
SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
)


@lru_cache(maxsize=1)
def get_credentials() -> Credentials:
    """Build refreshable OAuth credentials for the agent mailbox.

    Expects two secrets:
      * the OAuth client (the JSON downloaded from the Cloud console), and
      * a JSON blob {"refresh_token": "..."} produced by scripts/bootstrap_oauth.py.
    """
    from google.oauth2.credentials import Credentials

    settings = get_settings()
    client_blob = get_secret_json(settings.oauth_client_secret)
    token_blob = get_secret_json(settings.oauth_token_secret)

    # The console downloads the client wrapped in an "installed" or "web" key.
    client = client_blob.get("installed") or client_blob.get("web") or client_blob
    client_id = client.get("client_id")
    client_secret = client.get("client_secret")
    if not client_id or not client_secret:
        raise RuntimeError(
            f"secret {settings.oauth_client_secret!r} does not look like an OAuth client "
            "JSON (no client_id/client_secret)"
        )

    refresh_token = token_blob.get("refresh_token")
    if not refresh_token:
        raise RuntimeError(
            f"secret {settings.oauth_token_secret!r} has no 'refresh_token'. "
            "Run: uv run python scripts/bootstrap_oauth.py"
        )

    creds = Credentials(
        token=None,  # forces a refresh on first use
        refresh_token=refresh_token,
        token_uri=TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=list(SCOPES),
    )
    log.info("oauth credentials constructed", extra={"scopes": len(SCOPES)})
    return creds


@lru_cache(maxsize=2)
def _service(api: str, version: str):
    """Build a Google API client. Cached — discovery is slow and the client is thread-safe."""
    from googleapiclient.discovery import build

    return build(api, version, credentials=get_credentials(), cache_discovery=False)


def gmail_service():
    return _service("gmail", "v1")


def calendar_service():
    return _service("calendar", "v3")


def clear_cache() -> None:
    get_credentials.cache_clear()
    _service.cache_clear()
