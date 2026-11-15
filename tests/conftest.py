from collections.abc import Iterator
from pathlib import Path

import pytest

from obdient.config import OBDEX_COMMIT, Settings, load_settings
from obdient.knowledge.obdex import ObdexIndex, build_index, index_is_current
from support import OBDEX_DIR


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings rooted in a temp directory, with no API key and no .env lookup."""
    return load_settings(home=tmp_path / "home", env={})


@pytest.fixture(scope="session")
def obdex_index_path(request: pytest.FixtureRequest) -> Path:
    """The real OBDex index, built once (about 10 s) and kept in pytest's cache directory."""
    path = request.config.cache.mkdir("obdex") / f"obdex-{OBDEX_COMMIT}.sqlite"
    if not index_is_current(path):  # also rebuilds after a change to the tier list
        build_index(OBDEX_DIR, path)
    return path


@pytest.fixture(scope="session")
def obdex(obdex_index_path: Path) -> Iterator[ObdexIndex]:
    index = ObdexIndex(obdex_index_path)
    yield index
    index.close()
