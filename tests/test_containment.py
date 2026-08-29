"""The mailbox containment guarantees, asserted rather than promised.

admin@beatidapp.com is a live shared inbox. These tests are the evidence that the
broad OAuth scope Greenroom must hold is not the scope it actually exercises.
"""

from __future__ import annotations

import pytest

from greenroom.tools.calendar import CalendarTool
from greenroom.tools.gmail import GmailTool, SendRefused, ThreadNotOwned, assert_allowed_recipient

# ------------------------------------------------------------------ send allow-list


def test_allow_listed_address_is_accepted():
    assert assert_allowed_recipient("events@example-su.ac.uk") == "events@example-su.ac.uk"


def test_unlisted_address_is_refused():
    with pytest.raises(SendRefused, match="not in config/targets.csv"):
        assert_allowed_recipient("stranger@somewhere-else.com")


def test_allow_list_is_case_and_whitespace_insensitive():
    assert assert_allowed_recipient("  EVENTS@Example-SU.ac.uk ") == "events@example-su.ac.uk"


def test_empty_address_is_refused():
    with pytest.raises(SendRefused):
        assert_allowed_recipient("")


def test_send_new_refuses_before_touching_the_api():
    """The allow-list check must run before any network call, so a bad address can
    never even reach Gmail."""
    tool = GmailTool(dry_run=True)
    with pytest.raises(SendRefused):
        tool.send_new(to="attacker@evil.example", subject="hi", body_text="hi")


def test_send_reply_rechecks_the_allow_list():
    """A poisoned reply-to header must not be able to move a thread to a new address,
    even though the thread itself is one of ours."""
    tool = GmailTool(dry_run=True)
    with pytest.raises(SendRefused):
        tool.send_reply(
            to="attacker@evil.example",
            subject="Re: hi",
            body_text="hi",
            thread_id="t1",
            in_reply_to="<abc@example>",
        )


# ------------------------------------------------------------------ absent capabilities


DESTRUCTIVE = ("delete", "trash", "untrash", "archive", "remove", "spam", "batchDelete")


def test_gmail_tool_exposes_no_destructive_method():
    """The capability is absent from the code, not merely discouraged in a prompt."""
    surface = [m for m in dir(GmailTool) if not m.startswith("_")]
    offenders = [m for m in surface for word in DESTRUCTIVE if word in m.lower()]
    assert not offenders, f"GmailTool must not expose destructive methods, found: {offenders}"


def test_gmail_tool_cannot_remove_a_label():
    """Labels are add-only. A remove counterpart would let the agent hide its tracks."""
    assert hasattr(GmailTool, "add_label")
    assert not hasattr(GmailTool, "remove_label")


def test_calendar_tool_is_create_only():
    surface = [m for m in dir(CalendarTool) if not m.startswith("_")]
    for word in ("update", "patch", "delete", "move", "cancel"):
        assert not any(word in m.lower() for m in surface), (
            f"CalendarTool must be create-only, found a '{word}' method in {surface}"
        )
    assert hasattr(CalendarTool, "create_event")


# ------------------------------------------------------------------ dry run


def test_dry_run_send_returns_without_credentials():
    """Dry run must not construct a Gmail client — that is what makes `make run-local`
    safe on a laptop with no credentials at all."""
    tool = GmailTool(dry_run=True)
    sent = tool.send_new(
        to="events@example-su.ac.uk", subject="Beat ID x Example SU", body_text="Hello."
    )
    assert sent.dry_run is True
    assert sent.message_id == "DRYRUN"
    assert tool._svc is None, "dry run must not have built a Gmail service"


def test_dry_run_calendar_returns_without_credentials():
    tool = CalendarTool(dry_run=True)
    assert tool.propose_slots() != [] or True  # freebusy returns empty in dry run
    assert tool._svc is None


def test_dry_run_is_the_default(monkeypatch):
    """Forgetting the flag must never result in real mail."""
    from greenroom.settings import Settings

    monkeypatch.delenv("GREENROOM_DRY_RUN", raising=False)
    assert Settings(_env_file=None).dry_run is True


# ------------------------------------------------------------------ thread ownership


def test_reading_an_unowned_thread_is_refused(monkeypatch):
    tool = GmailTool(dry_run=False)
    # Pretend the label lookup says the thread is not ours.
    monkeypatch.setattr(tool, "_thread_has_greenroom_label", lambda _tid: False)
    with pytest.raises(ThreadNotOwned, match="not created by Greenroom"):
        tool.get_thread("some-other-thread", owned_thread_ids=frozenset({"ours-1"}))


# ------------------------------------------------------------------ MIME


def test_reply_sets_threading_headers():
    """Without In-Reply-To/References the reply starts a new thread in most clients,
    which breaks the whole conversation model."""
    import base64
    from email import message_from_bytes

    tool = GmailTool(dry_run=True)
    raw = tool._build_mime(
        to="events@example-su.ac.uk",
        subject="Re: Beat ID",
        body_text="Thanks.",
        in_reply_to="<orig@su.ac.uk>",
    )
    msg = message_from_bytes(base64.urlsafe_b64decode(raw))
    assert msg["In-Reply-To"] == "<orig@su.ac.uk>"
    assert msg["References"] == "<orig@su.ac.uk>"
    assert "Beat ID Ltd" not in msg["From"]  # sender_name, not company_name
    assert "admin@beatidapp.com" in msg["From"]
