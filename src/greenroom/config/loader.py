"""Load and validate config/*.yaml and config/targets.csv.

Loaded once at startup and cached. Validation failures raise `ConfigError` with the
offending file named, because a config problem discovered at send time is a config
problem discovered too late.
"""

from __future__ import annotations

import csv
import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import ValidationError

from greenroom.config.schemas import Brand, Policy, Target, TargetList


class ConfigError(RuntimeError):
    """Raised when a config file is missing or fails validation."""


def config_dir() -> Path:
    """Where config/ lives. Overridable for tests via GREENROOM_CONFIG_DIR."""
    override = os.environ.get("GREENROOM_CONFIG_DIR")
    if override:
        return Path(override)
    # src/greenroom/config/loader.py -> repo root
    return Path(__file__).resolve().parents[3] / "config"


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"missing config file: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must contain a YAML mapping, got {type(data).__name__}")
    return data


def load_brand(path: Path | None = None) -> Brand:
    path = path or config_dir() / "brand.yaml"
    try:
        return Brand.model_validate(_read_yaml(path))
    except ValidationError as exc:
        raise ConfigError(f"brand.yaml failed validation:\n{exc}") from exc


def load_policy(path: Path | None = None) -> Policy:
    path = path or config_dir() / "policy.yaml"
    try:
        return Policy.model_validate(_read_yaml(path))
    except ValidationError as exc:
        raise ConfigError(f"policy.yaml failed validation:\n{exc}") from exc


def load_targets(path: Path | None = None) -> TargetList:
    path = path or config_dir() / "targets.csv"
    if not path.exists():
        raise ConfigError(f"missing config file: {path}")

    rows: list[Target] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        required = {"organisation", "email"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ConfigError(
                f"targets.csv is missing required column(s): {', '.join(sorted(missing))}"
            )
        for line_no, raw in enumerate(reader, start=2):
            # Blank trailing lines are common when the CSV is edited in a spreadsheet.
            if not any((v or "").strip() for v in raw.values()):
                continue
            cleaned = {k: (v or "").strip() for k, v in raw.items() if k}
            if not cleaned.get("tier"):
                cleaned["tier"] = "3"
            try:
                rows.append(Target.model_validate(cleaned))
            except ValidationError as exc:
                raise ConfigError(f"targets.csv line {line_no} failed validation:\n{exc}") from exc

    if not rows:
        raise ConfigError("targets.csv contains no usable rows")

    try:
        return TargetList(targets=rows)
    except ValidationError as exc:
        raise ConfigError(f"targets.csv failed validation:\n{exc}") from exc


class AppConfig:
    """Everything the agents are allowed to read from disk, in one object."""

    def __init__(self, brand: Brand, policy: Policy, targets: TargetList) -> None:
        self.brand = brand
        self.policy = policy
        self.targets = targets

    @property
    def allowed_addresses(self) -> frozenset[str]:
        return self.targets.allowed_addresses


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """Process-wide singleton. Call `get_config.cache_clear()` in tests."""
    return AppConfig(load_brand(), load_policy(), load_targets())
