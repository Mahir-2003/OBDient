from pathlib import Path

import pytest

from obdient.config import Settings, load_settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings rooted in a temp directory, with no API key and no .env lookup."""
    return load_settings(home=tmp_path / "home", env={})
