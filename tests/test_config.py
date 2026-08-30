"""The shipped config must always be valid, and invalid config must fail loudly.

A silently-accepted bad policy.yaml is the worst failure mode in this system: the
agent would keep negotiating, just with the wrong envelope.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from greenroom.config import ConfigError, load_brand, load_policy, load_targets
from greenroom.settings import get_settings

# ------------------------------------------------------------------ shipped config


def test_shipped_brand_is_valid(real_config_dir: Path):
    brand = load_brand(real_config_dir / "brand.yaml")
    assert brand.company_name
    # Not pinned to a literal address: the agent mailbox must be a Google account and
    # moved once beatidapp.com turned out to be on Zoho. What matters is that the
    # configured sender is the mailbox Greenroom actually sends from.
    assert brand.sender_email == get_settings().agent_mailbox
    assert brand.proof_points, "the Writer needs at least one checkable claim"


def test_shipped_policy_is_valid(real_config_dir: Path):
    policy = load_policy(real_config_dir / "policy.yaml")
    assert policy.fee.floor <= policy.fee.standard
    assert policy.availability.windows
    assert policy.trust.default_mode == "review", "targets must start under review"


def test_shipped_targets_are_valid(real_config_dir: Path):
    targets = load_targets(real_config_dir / "targets.csv")
    assert targets.targets
    assert len(targets.allowed_addresses) == len(targets.targets)


def test_allow_list_is_lowercased(real_config_dir: Path):
    targets = load_targets(real_config_dir / "targets.csv")
    assert all(a == a.lower() for a in targets.allowed_addresses)


def test_by_email_is_case_insensitive(real_config_dir: Path):
    targets = load_targets(real_config_dir / "targets.csv")
    first = targets.targets[0]
    assert targets.by_email(first.email.upper()) is not None
    assert targets.by_email("  " + first.email + "  ") is not None
    assert targets.by_email("nobody@nowhere.example") is None


# ------------------------------------------------------------------ policy guards


def test_fee_floor_above_standard_is_rejected(tmp_config: Path):
    path = tmp_config / "policy.yaml"
    path.write_text(path.read_text().replace("floor: 850", "floor: 5000"))
    with pytest.raises(ConfigError, match="floor"):
        load_policy(path)


def test_end_before_start_window_is_rejected(tmp_config: Path):
    path = tmp_config / "policy.yaml"
    path.write_text(path.read_text().replace('end: "2026-10-11"', 'end: "2026-01-01"'))
    with pytest.raises(ConfigError):
        load_policy(path)


def test_close_before_last_follow_up_is_rejected(tmp_config: Path):
    """A follow-up scheduled after the thread closes would never fire."""
    path = tmp_config / "policy.yaml"
    path.write_text(path.read_text().replace("close_after_days: 14", "close_after_days: 5"))
    with pytest.raises(ConfigError):
        load_policy(path)


def test_unknown_policy_key_is_rejected(tmp_config: Path):
    """extra='forbid' — a misspelled key must not be silently ignored."""
    path = tmp_config / "policy.yaml"
    path.write_text(path.read_text() + "\nfee_floor_typo: 100\n")
    with pytest.raises(ConfigError):
        load_policy(path)


def test_bad_weekday_is_rejected(tmp_config: Path):
    path = tmp_config / "policy.yaml"
    path.write_text(
        path.read_text().replace("allowed_weekdays: [2, 3, 4, 5]", "allowed_weekdays: [2, 9]")
    )
    with pytest.raises(ConfigError):
        load_policy(path)


def test_meeting_hours_must_be_ordered(tmp_config: Path):
    path = tmp_config / "policy.yaml"
    path.write_text(path.read_text().replace("latest_hour: 17", "latest_hour: 9"))
    with pytest.raises(ConfigError):
        load_policy(path)


# ------------------------------------------------------------------ targets guards


def test_duplicate_email_is_rejected(tmp_path: Path):
    path = tmp_path / "targets.csv"
    path.write_text(
        "organisation,contact_name,email,venue_notes,tier,context\n"
        "A SU,,dup@example.ac.uk,,1,\n"
        "B SU,,DUP@example.ac.uk,,2,\n"
    )
    with pytest.raises(ConfigError, match="duplicate"):
        load_targets(path)


def test_malformed_email_is_rejected(tmp_path: Path):
    path = tmp_path / "targets.csv"
    path.write_text(
        "organisation,contact_name,email,venue_notes,tier,context\nA SU,,not-an-email,,1,\n"
    )
    with pytest.raises(ConfigError):
        load_targets(path)


def test_missing_column_is_rejected(tmp_path: Path):
    path = tmp_path / "targets.csv"
    path.write_text("organisation,contact_name\nA SU,Sam\n")
    with pytest.raises(ConfigError, match="email"):
        load_targets(path)


def test_blank_rows_are_skipped(tmp_path: Path):
    """Spreadsheets leave trailing empty lines; those must not become targets."""
    path = tmp_path / "targets.csv"
    path.write_text(
        "organisation,contact_name,email,venue_notes,tier,context\n"
        "A SU,,a@example.ac.uk,,1,\n"
        ",,,,,\n"
        ",,,,,\n"
    )
    assert len(load_targets(path).targets) == 1


def test_missing_tier_defaults_to_three(tmp_path: Path):
    path = tmp_path / "targets.csv"
    path.write_text(
        "organisation,contact_name,email,venue_notes,tier,context\nA SU,,a@example.ac.uk,,,\n"
    )
    assert load_targets(path).targets[0].tier == 3


def test_empty_targets_file_is_rejected(tmp_path: Path):
    path = tmp_path / "targets.csv"
    path.write_text("organisation,contact_name,email,venue_notes,tier,context\n")
    with pytest.raises(ConfigError, match="no usable rows"):
        load_targets(path)


def test_missing_file_is_reported_clearly(tmp_path: Path):
    with pytest.raises(ConfigError, match="missing config file"):
        load_policy(tmp_path / "nope.yaml")


# ------------------------------------------------------------------ config discovery


def test_config_is_found_when_package_is_installed_elsewhere(tmp_config: Path, monkeypatch):
    """Regression: the first Cloud Run deploy failed because config_dir() resolved
    relative to the installed package (site-packages) rather than the working
    directory. Discovery must work from a container layout, not just a checkout."""
    from greenroom.config.loader import config_dir

    monkeypatch.delenv("GREENROOM_CONFIG_DIR", raising=False)
    monkeypatch.chdir(tmp_config.parent)
    assert config_dir() == tmp_config


def test_missing_config_dir_names_every_path_it_searched(tmp_path, monkeypatch):
    from greenroom.config.loader import ConfigError, config_dir

    monkeypatch.setenv("GREENROOM_CONFIG_DIR", str(tmp_path / "nowhere"))
    with pytest.raises(ConfigError, match="Searched"):
        config_dir()
