from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def real_config_dir() -> Path:
    """The repo's actual config/ directory — these must always be valid."""
    return REPO_ROOT / "config"


@pytest.fixture
def tmp_config(tmp_path: Path, real_config_dir: Path) -> Path:
    """A writable copy of the real config, for mutating into invalid states."""
    import shutil

    dest = tmp_path / "config"
    shutil.copytree(real_config_dir, dest)
    return dest
