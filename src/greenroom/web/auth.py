"""The dashboard demo gate.

**This is not authentication.** It is a single shared secret in a cookie, chosen because
the hackathon brief asks for exactly that and because real auth is not what is being
judged here. It is documented as a demo gate in the README so nobody mistakes it for
security, and it is the reason the dashboard shows no message bodies from outside
Greenroom's own threads even to a logged-in viewer.

What it does buy: the Cloud Run service must be publicly reachable for Pub/Sub push and
for the demo, and this stops a stray crawler finding the approve buttons.
"""

from __future__ import annotations

import hmac

from fastapi import Request
from fastapi.responses import RedirectResponse

from greenroom.settings import get_settings

COOKIE_NAME = "greenroom_gate"


def is_unlocked(request: Request) -> bool:
    secret = get_settings().dashboard_secret
    if not secret:
        # No secret configured: open. Local development would otherwise be unusable,
        # and a deployed service always has one set.
        return True
    presented = request.cookies.get(COOKIE_NAME, "")
    # Constant-time compare: the secret is short and a timing oracle is free to avoid.
    return hmac.compare_digest(presented, secret)


def require_unlocked(request: Request) -> RedirectResponse | None:
    """Returns a redirect if locked, else None."""
    if is_unlocked(request):
        return None
    return RedirectResponse(url=f"/login?next={request.url.path}", status_code=303)


def check_secret(candidate: str) -> bool:
    secret = get_settings().dashboard_secret
    return bool(secret) and hmac.compare_digest(candidate, secret)
