from typer.testing import CliRunner

from obdient import __version__
from obdient.cli import app

runner = CliRunner()


def test_version_prints_and_exits_zero() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"obdient {__version__}"


def test_no_arguments_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "Read-only OBD-II diagnostic agent" in result.output


def test_unknown_option_is_a_usage_error() -> None:
    result = runner.invoke(app, ["--nope"])
    assert result.exit_code == 2
