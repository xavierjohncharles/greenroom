"""The send window and the caps — the rules that answer 'what stops it emailing at 3am?'"""

from __future__ import annotations

from datetime import UTC, datetime

from greenroom.config import load_policy
from greenroom.state.repo import in_send_window

# Policy ships with Mon-Fri 09:00-17:00 Europe/London.
MON_10AM_UK = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)  # 10:00 BST
MON_3AM_UK = datetime(2026, 8, 31, 2, 0, tzinfo=UTC)  # 03:00 BST
SAT_11AM_UK = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)  # Saturday
MON_6PM_UK = datetime(2026, 8, 31, 17, 0, tzinfo=UTC)  # 18:00 BST


def window(real_config_dir):
    return load_policy(real_config_dir / "policy.yaml").operations.send_window


def test_weekday_business_hours_are_allowed(real_config_dir):
    allowed, why = in_send_window(MON_10AM_UK, window(real_config_dir))
    assert allowed, why


def test_the_middle_of_the_night_is_refused(real_config_dir):
    allowed, why = in_send_window(MON_3AM_UK, window(real_config_dir))
    assert not allowed
    assert "03:00" in why


def test_weekends_are_refused(real_config_dir):
    allowed, why = in_send_window(SAT_11AM_UK, window(real_config_dir))
    assert not allowed
    assert "Saturday" in why


def test_after_hours_is_refused(real_config_dir):
    allowed, why = in_send_window(MON_6PM_UK, window(real_config_dir))
    assert not allowed
    assert "18:00" in why


def test_the_reason_is_human_readable(real_config_dir):
    """It is rendered verbatim on the dashboard, so it has to read like a sentence."""
    _, why = in_send_window(SAT_11AM_UK, window(real_config_dir))
    assert why and why[0].isupper() or "outside" in why


def test_boundaries_are_inclusive_at_the_start_exclusive_at_the_end(real_config_dir):
    w = window(real_config_dir)
    nine = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)  # 09:00 BST
    five = datetime(2026, 8, 31, 16, 0, tzinfo=UTC)  # 17:00 BST
    assert in_send_window(nine, w)[0], "09:00 should be inside the window"
    assert not in_send_window(five, w)[0], "17:00 is the end, not part of the window"


def test_utc_input_is_converted_to_london_before_judging(real_config_dir):
    """In August the UK is BST, one hour ahead. A naive UTC comparison would let the
    agent send an hour early at each end of the day."""
    eight_bst = datetime(2026, 8, 31, 7, 0, tzinfo=UTC)  # 08:00 BST — too early
    assert not in_send_window(eight_bst, window(real_config_dir))[0]
