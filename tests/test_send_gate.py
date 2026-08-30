"""The send window — the rule that answers "what stops it emailing at 3am?"

The logic is tested against explicit fixture windows so it does not depend on whatever
policy.yaml happens to say today. A single separate test asserts that the *shipped*
policy is still business-hours-sane, which is what catches a temporary widening left in
by accident.
"""

from __future__ import annotations

from datetime import UTC, datetime

from greenroom.config import load_policy
from greenroom.config.schemas import SendWindow
from greenroom.state.repo import in_send_window

# Mon-Fri, 09:00-17:00 Europe/London.
BUSINESS_HOURS = SendWindow(
    timezone="Europe/London", start_hour=9, end_hour=17, weekdays=[0, 1, 2, 3, 4]
)

MON_10AM = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)  # 10:00 BST
MON_3AM = datetime(2026, 8, 31, 2, 0, tzinfo=UTC)  # 03:00 BST
SAT_11AM = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)  # Saturday
MON_6PM = datetime(2026, 8, 31, 17, 0, tzinfo=UTC)  # 18:00 BST


# ------------------------------------------------------------------ the logic


def test_weekday_business_hours_are_allowed():
    allowed, why = in_send_window(MON_10AM, BUSINESS_HOURS)
    assert allowed, why


def test_the_middle_of_the_night_is_refused():
    allowed, why = in_send_window(MON_3AM, BUSINESS_HOURS)
    assert not allowed
    assert "03:00" in why


def test_weekends_are_refused():
    allowed, why = in_send_window(SAT_11AM, BUSINESS_HOURS)
    assert not allowed
    assert "Saturday" in why


def test_after_hours_is_refused():
    allowed, why = in_send_window(MON_6PM, BUSINESS_HOURS)
    assert not allowed
    assert "18:00" in why


def test_the_reason_reads_like_a_sentence():
    """It is rendered verbatim on the dashboard and in the job's last_error."""
    _, why = in_send_window(SAT_11AM, BUSINESS_HOURS)
    assert "outside the send window" in why


def test_boundaries_are_inclusive_at_the_start_exclusive_at_the_end():
    nine = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)  # 09:00 BST
    five = datetime(2026, 8, 31, 16, 0, tzinfo=UTC)  # 17:00 BST
    assert in_send_window(nine, BUSINESS_HOURS)[0], "09:00 is inside the window"
    assert not in_send_window(five, BUSINESS_HOURS)[0], "17:00 is the end, not part of it"


def test_utc_input_is_converted_to_london_before_judging():
    """In August the UK is BST, one hour ahead. A naive UTC comparison would let the
    agent send an hour early at each end of the day."""
    eight_bst = datetime(2026, 8, 31, 7, 0, tzinfo=UTC)  # 08:00 BST — too early
    assert not in_send_window(eight_bst, BUSINESS_HOURS)[0]


def test_a_widened_window_really_does_allow_more():
    """Sanity check on the fixture itself, so the tests above cannot pass vacuously."""
    always = SendWindow(
        timezone="Europe/London", start_hour=0, end_hour=23, weekdays=[0, 1, 2, 3, 4, 5, 6]
    )
    assert in_send_window(SAT_11AM, always)[0]
    assert in_send_window(MON_3AM, always)[0]


# ------------------------------------------------------------------ the shipped config


def test_shipped_policy_still_sends_only_in_uk_business_hours(real_config_dir):
    """Guards against a temporary widening being left in.

    The send window is widened by hand occasionally to run a live end-to-end test
    outside office hours. This test is the thing that notices if it is never put back —
    which would otherwise only be discovered by a students' union receiving a pitch at
    two in the morning.
    """
    window = load_policy(real_config_dir / "policy.yaml").operations.send_window

    assert window.weekdays == [0, 1, 2, 3, 4], (
        f"send window allows weekends: {window.weekdays}. "
        "If this was a temporary widening for testing, put it back."
    )
    assert window.start_hour >= 8, f"send window opens at {window.start_hour}:00"
    assert window.end_hour <= 18, f"send window closes at {window.end_hour}:00"
    assert window.timezone == "Europe/London"
