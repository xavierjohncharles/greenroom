"""Gmail watch → Pub/Sub → push → here. The only way mail enters Greenroom.

https://developers.google.com/workspace/gmail/api/guides/push
https://docs.cloud.google.com/pubsub/docs/authenticate-push-subscriptions

Three things this endpoint has to get right, and each has bitten somebody:

  * **Authenticate the push.** The service must be publicly reachable (Pub/Sub push and
    the demo both need it), so the OIDC JWT Pub/Sub signs is verified here rather than
    relying on Cloud Run's IAM check. Issuer, audience and the service-account email are
    all checked.
  * **Tolerate at-least-once delivery.** Pub/Sub redelivers. A redelivered notification
    must not produce a second reply, so every message is deduplicated on the Gmail
    message id before any agent runs.
  * **Never fetch outside our own threads.** The notification carries only
    `{emailAddress, historyId}` — no content — so the fetch is ours to scope. History is
    queried with the greenroom label, and every message is checked against the threads
    Greenroom itself created before it is read.

Always returns 200 for anything we have decided not to process. A non-200 tells Pub/Sub
to retry, and retrying a message we have deliberately ignored is an infinite loop.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from greenroom.obs import get_logger, set_log_context
from greenroom.settings import get_settings

log = get_logger(__name__)

router = APIRouter()

PUBSUB_ISSUERS = ("https://accounts.google.com", "accounts.google.com")


class PushAuthError(RuntimeError):
    """The push request did not carry a valid Pub/Sub OIDC token."""


def verify_push_token(request: Request) -> dict[str, Any]:
    """Verify the OIDC JWT Pub/Sub signs onto the push request.

    If no push service account is configured we refuse rather than wave it through:
    an unauthenticated endpoint that runs agents is a worse default than a broken one.
    """
    settings = get_settings()
    expected_sa = settings.push_sa_email
    if not expected_sa:
        raise PushAuthError(
            "GREENROOM_PUSH_SA_EMAIL is not set; refusing to accept unauthenticated push"
        )

    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise PushAuthError("missing bearer token")
    token = header.split(" ", 1)[1]

    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    try:
        claims = id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            audience=settings.push_audience or None,
        )
    except Exception as exc:
        raise PushAuthError(f"token verification failed: {exc}") from exc

    if claims.get("iss") not in PUBSUB_ISSUERS:
        raise PushAuthError(f"unexpected issuer {claims.get('iss')!r}")
    if claims.get("email") != expected_sa:
        raise PushAuthError(f"unexpected service account {claims.get('email')!r}")
    if not claims.get("email_verified", False):
        raise PushAuthError("service account email is not verified")

    return claims


def decode_push_body(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Pull the Pub/Sub message id and the Gmail notification out of a push envelope."""
    message = body.get("message") or {}
    pubsub_message_id = str(message.get("messageId") or message.get("message_id") or "")

    raw = message.get("data")
    if not raw:
        return pubsub_message_id, {}
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw).decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError(f"push payload is not base64url JSON: {exc}") from exc
    return pubsub_message_id, payload


@router.post("/inbound")
async def inbound(request: Request) -> JSONResponse:
    settings = get_settings()

    try:
        verify_push_token(request)
    except PushAuthError as exc:
        # 403, not 200: a forged or misconfigured push should be visibly rejected and
        # is not something Pub/Sub should retry into existence.
        log.warning("push rejected", extra={"reason": str(exc)})
        return JSONResponse({"status": "forbidden", "reason": str(exc)}, status_code=403)

    try:
        envelope = await request.json()
        pubsub_id, notification = decode_push_body(envelope)
    except (ValueError, TypeError) as exc:
        log.warning("malformed push body", extra={"error": str(exc)})
        # 200: a malformed message will never become well-formed on retry.
        return JSONResponse({"status": "ignored", "reason": "malformed"}, status_code=200)

    history_id = str(notification.get("historyId", ""))
    address = str(notification.get("emailAddress", ""))
    set_log_context(job_id=pubsub_id or None)

    if address and address.lower() != settings.agent_mailbox.lower():
        log.warning("notification for an unexpected mailbox", extra={"address": address})
        return JSONResponse({"status": "ignored", "reason": "wrong mailbox"}, status_code=200)

    log.info(
        "inbound notification",
        extra={"pubsub_id": pubsub_id, "history_id": history_id, "address": address},
    )

    from greenroom.jobs.inbound import process_history

    try:
        result = await process_history(history_id=history_id, pubsub_message_id=pubsub_id)
    except Exception as exc:
        # 500 so Pub/Sub retries: a transient Gmail or Firestore failure should not
        # silently lose an inbound reply.
        log.error("inbound processing failed", extra={"error": str(exc)})
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)

    return JSONResponse({"status": "ok", **result}, status_code=200)
