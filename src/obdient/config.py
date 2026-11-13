"""Settings: models per role, prices, budgets, paths (SPEC §2).

Model names and budgets live only in this file.
"""

import os
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path

from dotenv import dotenv_values
from pydantic import SecretStr

from obdient.errors import ObdientError
from obdient.schemas.common import Model

OBDEX_COMMIT = "9ba1ffa"
WAL33D_COMMIT = "04c43d7"

REPO_ROOT = Path(__file__).resolve().parents[2]


class ModelRole(StrEnum):
    DIAGNOSTICIAN = "diagnostician"
    DEV = "dev"
    COMPLAINT_RANKER = "complaint_ranker"  # v0.2
    JUDGE = "judge"  # v0.2


class ModelPrice(Model):
    """USD per 1M tokens."""

    input: float
    cached_input: float
    output: float


class Budgets(Model):
    max_turns: int = 12
    max_live_reads: int = 6
    max_questions: int = 3
    max_user_actions: int = 3  # v0.2
    max_revisions: int = 1
    max_output_tokens: int = 6000  # per LLM call; [VERIFY M5]


# [VERIFY M0] Model IDs and prices come from the PRD table, not the official pricing page.
DEFAULT_MODELS: dict[ModelRole, str] = {
    ModelRole.DIAGNOSTICIAN: "gpt-6.1-sol",
    ModelRole.DEV: "gpt-6-luna",
    ModelRole.COMPLAINT_RANKER: "gpt-6-luna",
    ModelRole.JUDGE: "gpt-6.1-sol",
}

# [VERIFY M0] cached_input is not known yet. It is set equal to input, so logged cost can
# only be too high, never too low, until the real discount is filled in.
DEFAULT_PRICES: dict[str, ModelPrice] = {
    "gpt-6.1-sol": ModelPrice(input=2.0, cached_input=2.0, output=10.0),
    "gpt-6.1-terra": ModelPrice(input=1.0, cached_input=1.0, output=5.0),
    "gpt-6-luna": ModelPrice(input=0.10, cached_input=0.10, output=0.50),
}


class Settings(Model):
    home: Path
    db_path: Path
    reports_dir: Path
    runs_dir: Path  # full transcripts
    sessions_dir: Path  # recordings
    data_dir: Path
    obdex_index_path: Path
    openai_api_key: SecretStr | None
    models: dict[ModelRole, str]
    prices: dict[str, ModelPrice]  # keyed by model name
    budgets: Budgets
    nhtsa_delay_s: float = 1.0
    nhtsa_timeout_s: float = 10.0
    nhtsa_products_enabled: bool = False  # [VERIFY M0]


def load_settings(home: Path | None = None, env: Mapping[str, str] | None = None) -> Settings:
    """Build Settings and create the home directory tree.

    `home` beats `OBDIENT_HOME`, which beats `~/.obdient`. When `env` is None the process
    environment is used, with `.env` in the working directory as a fallback for unset names.
    A missing API key is not an error here; it is one only when an LLM call is attempted.
    """
    if env is None:
        file_values = {k: v for k, v in dotenv_values(".env").items() if v is not None}
        env = {**file_values, **os.environ}

    if home is None:
        home = Path(env["OBDIENT_HOME"]) if env.get("OBDIENT_HOME") else Path.home() / ".obdient"
    home = home.expanduser()

    models = dict(DEFAULT_MODELS)
    prices = dict(DEFAULT_PRICES)
    unpriced = sorted({name for name in models.values() if name not in prices})
    if unpriced:
        raise ObdientError(
            f"No price configured for model(s) {', '.join(unpriced)}; add them to config.py."
        )

    key = env.get("OPENAI_API_KEY") or None
    settings = Settings(
        home=home,
        db_path=home / "obdient.db",
        reports_dir=home / "reports",
        runs_dir=home / "runs",
        sessions_dir=home / "sessions",
        data_dir=REPO_ROOT / "data",
        obdex_index_path=home / "index" / f"obdex-{OBDEX_COMMIT}.sqlite",
        openai_api_key=SecretStr(key) if key else None,
        models=models,
        prices=prices,
        budgets=Budgets(),
    )
    for directory in (
        settings.home,
        settings.reports_dir,
        settings.runs_dir,
        settings.sessions_dir,
        settings.obdex_index_path.parent,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    return settings
