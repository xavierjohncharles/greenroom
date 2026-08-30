"""The /inbound endpoint: authentication, envelope decoding, and retry semantics.

The retry semantics matter as much as the auth. Pub/Sub retries any non-200, so the
status code is a control signal: returning 500 for a message that will never succeed
creates an infinite redelivery loop, and returning 200 for a transient failure silently
drops a customer's reply.
"""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from greenroom.web.inbound import PushAuthError, decode_push_body, verify_push_token


def envelope(payload: dict, *, message_id: str = "pubsub-1") -> dict:
    return {
        "message": {
            "messageId": message_id,
            "data": base64.urlsafe_b64encode(json.dumps(payload).encode()).decode(),
        },
        "subscription": "projects/p/subscriptions/greenroom-gmail-push",
    }


# ------------------------------------------------------------------ decoding


def test_decodes_a_gmail_notification():
    pubsub_id, payload = decode_push_body(
        envelope({"emailAddress": "admin@beatidapp.com", "historyId": "9876"})
    )
    assert pubsub_id == "pubsub-1"
    assert payload["historyId"] == "9876"


def test_a_notification_carries_no_message_content():
    """Gmail sends only {emailAddress, historyId} — the fetch is ours to scope, which is
    what lets containment be enforced on the read side."""
    _, payload = decode_push_body(
        envelope({"emailAddress": "admin@beatidapp.com", "historyId": "1"})
    )
    assert set(payload) == {"emailAddress", "historyId"}


def test_an_empty_data_field_is_tolerated():
    pubsub_id, payload = decode_push_body({"message": {"messageId": "x"}})
    assert pubsub_id == "x"
    assert payload == {}


def test_undecodable_payload_raises_rather_than_guessing():
    with pytest.raises(ValueError):
        decode_push_body({"message": {"messageId": "x", "data": "!!!not-base64!!!"}})


# ------------------------------------------------------------------ auth


class _Req:
    def __init__(self, headers: dict[str, str] | None = None):
        self.headers = headers or {}


def test_push_is_refused_when_no_service_account_is_configured(monkeypatch):
    """An unauthenticated endpoint that runs agents is a worse default than a broken
    one, so a missing config refuses rather than waves through."""
    from greenroom.settings import Settings, get_settings

    get_settings.cache_clear()
    monkeypatch.setattr("greenroom.web.inbound.get_settings", lambda: Settings(_env_file=None))
    with pytest.raises(PushAuthError, match="refusing to accept unauthenticated push"):
        verify_push_token(_Req({"authorization": "Bearer whatever"}))


def test_missing_bearer_token_is_refused(monkeypatch):
    from greenroom.settings import Settings

    monkeypatch.setattr(
        "greenroom.web.inbound.get_settings",
        lambda: Settings(_env_file=None, GREENROOM_PUSH_SA_EMAIL="push@x.iam.gserviceaccount.com"),
    )
    with pytest.raises(PushAuthError, match="missing bearer token"):
        verify_push_token(_Req({}))


def test_a_token_from_the_wrong_service_account_is_refused(monkeypatch):
    """The signature can be perfectly valid and still be the wrong caller."""
    from greenroom.settings import Settings

    monkeypatch.setattr(
        "greenroom.web.inbound.get_settings",
        lambda: Settings(
            _env_file=None, GREENROOM_PUSH_SA_EMAIL="expected@x.iam.gserviceaccount.com"
        ),
    )
    monkeypatch.setattr(
        "google.oauth2.id_token.verify_oauth2_token",
        lambda *a, **k: {
            "iss": "https://accounts.google.com",
            "email": "attacker@evil.iam.gserviceaccount.com",
            "email_verified": True,
        },
    )
    with pytest.raises(PushAuthError, match="unexpected service account"):
        verify_push_token(_Req({"authorization": "Bearer t"}))


def test_an_unverified_service_account_email_is_refused(monkeypatch):
    from greenroom.settings import Settings

    monkeypatch.setattr(
        "greenroom.web.inbound.get_settings",
        lambda: Settings(
            _env_file=None, GREENROOM_PUSH_SA_EMAIL="expected@x.iam.gserviceaccount.com"
        ),
    )
    monkeypatch.setattr(
        "google.oauth2.id_token.verify_oauth2_token",
        lambda *a, **k: {
            "iss": "https://accounts.google.com",
            "email": "expected@x.iam.gserviceaccount.com",
            "email_verified": False,
        },
    )
    with pytest.raises(PushAuthError, match="not verified"):
        verify_push_token(_Req({"authorization": "Bearer t"}))


def test_a_valid_push_token_is_accepted(monkeypatch):
    from greenroom.settings import Settings

    monkeypatch.setattr(
        "greenroom.web.inbound.get_settings",
        lambda: Settings(
            _env_file=None, GREENROOM_PUSH_SA_EMAIL="expected@x.iam.gserviceaccount.com"
        ),
    )
    monkeypatch.setattr(
        "google.oauth2.id_token.verify_oauth2_token",
        lambda *a, **k: {
            "iss": "https://accounts.google.com",
            "email": "expected@x.iam.gserviceaccount.com",
            "email_verified": True,
        },
    )
    claims = verify_push_token(_Req({"authorization": "Bearer t"}))
    assert claims["email"] == "expected@x.iam.gserviceaccount.com"


# ------------------------------------------------------------------ endpoint


def test_a_forged_push_gets_403_not_200():
    """403 rather than 200: a forged push should be visibly rejected, and Pub/Sub should
    not be told to retry it into existence."""
    from greenroom.web.main import app

    with TestClient(app) as client:
        r = client.post("/inbound", json=envelope({"historyId": "1"}))
        assert r.status_code == 403


def test_a_malformed_body_gets_200_so_pubsub_stops_retrying(monkeypatch):
    """A malformed message will never become well-formed on retry. Returning non-200
    would put it in a redelivery loop until the retention window expires."""
    from greenroom.web import inbound
    from greenroom.web.main import app

    monkeypatch.setattr(inbound, "verify_push_token", lambda request: {"email": "ok"})
    with TestClient(app) as client:
        r = client.post("/inbound", json={"message": {"messageId": "x", "data": "@@@"}})
        assert r.status_code == 200
        assert r.json()["reason"] == "malformed"


def test_a_notification_for_another_mailbox_is_ignored(monkeypatch):
    from greenroom.web import inbound
    from greenroom.web.main import app

    monkeypatch.setattr(inbound, "verify_push_token", lambda request: {"email": "ok"})
    with TestClient(app) as client:
        r = client.post(
            "/inbound", json=envelope({"emailAddress": "someone@else.com", "historyId": "1"})
        )
        assert r.status_code == 200
        assert r.json()["reason"] == "wrong mailbox"


# ------------------------------------------------------------------ tick auth


def test_the_tick_is_not_open_to_anonymous_callers(monkeypatch):
    """/tick runs agents and can cause mail to be sent. An unauthenticated caller with
    the URL must not be able to make the agent act."""
    from greenroom.settings import Settings, get_settings
    from greenroom.web import auth
    from greenroom.web.main import app

    get_settings.cache_clear()
    monkeypatch.setattr(auth, "is_unlocked", lambda request: False)
    monkeypatch.setattr(
        "greenroom.web.inbound.get_settings",
        lambda: Settings(
            _env_file=None, GREENROOM_SCHEDULER_SA_EMAIL="sched@x.iam.gserviceaccount.com"
        ),
    )
    with TestClient(app) as client:
        assert client.post("/tick").status_code == 403


def test_a_scheduler_token_is_accepted(monkeypatch):
    from greenroom.settings import Settings
    from greenroom.web.inbound import verify_tick_token

    monkeypatch.setattr(
        "greenroom.web.inbound.get_settings",
        lambda: Settings(
            _env_file=None, GREENROOM_SCHEDULER_SA_EMAIL="sched@x.iam.gserviceaccount.com"
        ),
    )
    monkeypatch.setattr(
        "google.oauth2.id_token.verify_oauth2_token",
        lambda *a, **k: {
            "iss": "https://accounts.google.com",
            "email": "sched@x.iam.gserviceaccount.com",
            "email_verified": True,
        },
    )
    assert verify_tick_token(_Req({"authorization": "Bearer t"}))["email"].startswith("sched@")


def test_a_stranger_token_is_refused_on_the_tick(monkeypatch):
    from greenroom.settings import Settings
    from greenroom.web.inbound import PushAuthError, verify_tick_token

    monkeypatch.setattr(
        "greenroom.web.inbound.get_settings",
        lambda: Settings(
            _env_file=None, GREENROOM_SCHEDULER_SA_EMAIL="sched@x.iam.gserviceaccount.com"
        ),
    )
    monkeypatch.setattr(
        "google.oauth2.id_token.verify_oauth2_token",
        lambda *a, **k: {
            "iss": "https://accounts.google.com",
            "email": "someone-else@evil.iam.gserviceaccount.com",
            "email_verified": True,
        },
    )
    with pytest.raises(PushAuthError, match="unexpected service account"):
        verify_tick_token(_Req({"authorization": "Bearer t"}))
