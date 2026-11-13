from pathlib import Path

import pytest

from obdient import config
from obdient.config import Budgets, ModelRole, Settings, load_settings
from obdient.errors import ObdientError


def test_home_argument_wins_and_directories_are_created(tmp_path: Path) -> None:
    home = tmp_path / "explicit"
    s = load_settings(home=home, env={"OBDIENT_HOME": str(tmp_path / "from_env")})
    assert s.home == home
    for directory in (s.home, s.reports_dir, s.runs_dir, s.sessions_dir):
        assert directory.is_dir()
    assert s.obdex_index_path.parent.is_dir()
    assert not (tmp_path / "from_env").exists()


def test_obdient_home_env_overrides_default(tmp_path: Path) -> None:
    s = load_settings(env={"OBDIENT_HOME": str(tmp_path / "from_env")})
    assert s.home == tmp_path / "from_env"
    assert s.db_path == tmp_path / "from_env" / "obdient.db"


def test_index_path_embeds_the_pinned_commit(settings: Settings) -> None:
    assert settings.obdex_index_path.name == "obdex-9ba1ffa.sqlite"
    assert settings.data_dir.name == "data"


def test_missing_api_key_is_not_an_error_at_load(settings: Settings) -> None:
    assert settings.openai_api_key is None


def test_api_key_is_kept_secret(tmp_path: Path) -> None:
    s = load_settings(home=tmp_path, env={"OPENAI_API_KEY": "sk-test-123"})
    assert s.openai_api_key is not None
    assert s.openai_api_key.get_secret_value() == "sk-test-123"
    assert "sk-test-123" not in repr(s)


def test_every_role_has_a_priced_model(settings: Settings) -> None:
    assert set(settings.models) == set(ModelRole)
    for name in settings.models.values():
        assert name in settings.prices


def test_model_without_a_price_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(config.DEFAULT_MODELS, ModelRole.DEV, "gpt-unpriced")
    with pytest.raises(ObdientError, match="gpt-unpriced"):
        load_settings(home=tmp_path, env={})


def test_budget_defaults() -> None:
    b = Budgets()
    assert (b.max_turns, b.max_live_reads, b.max_questions, b.max_revisions) == (12, 6, 3, 1)
    assert b.max_output_tokens == 6000
    assert not any("usd" in name for name in Budgets.model_fields)  # SPEC X10: no dollar limits
