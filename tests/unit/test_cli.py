import socket
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from obdient import __version__
from obdient.cli import app, mask_vin
from obdient.schemas.common import Condition
from obdient.schemas.snapshot import Snapshot, WarningLight
from obdient.storage.db import Database
from support import SCENARIOS_DIR

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


# ---- scan (SPEC §13.1) -------------------------------------------------------


def scan(tmp_path: Path, *args: str, stdin: str | None = None) -> Result:
    home = ["--home", str(tmp_path / "home")]
    return runner.invoke(app, [*home, "scan", "--scenarios-dir", str(SCENARIOS_DIR), *args], stdin)


def stored(tmp_path: Path) -> Snapshot | None:
    database = Database(tmp_path / "home" / "obdient.db")
    try:
        return database.latest_snapshot()
    finally:
        database.close()


def test_scan_scenario_prints_a_summary_and_stores_the_snapshot(tmp_path: Path) -> None:
    result = scan(tmp_path, "--scenario", "s01")

    assert result.exit_code == 0, result.output
    snapshot = stored(tmp_path)
    assert snapshot is not None
    lines = result.stdout.splitlines()
    assert lines[0] == f"Snapshot {snapshot.snapshot_id}"
    assert "  Source:      simulator" in lines
    assert "  VIN:         WVW*******TURE" in lines  # masked: the VIN itself is not printed
    assert "WVWZZZ_FIXTURE" not in result.stdout
    assert "  MIL:         on" in lines
    assert "  Codes:       stored: P0171; pending: none; permanent: P0171" in lines
    assert "  Readiness:   7 of 7 supported monitors complete" in lines
    assert "  Conditions:  hold_2500, idle" in lines
    assert "  Read errors: 0" in lines

    assert snapshot.conditions_captured == [Condition.HOLD_2500, Condition.IDLE]
    assert snapshot.user_report.symptoms_text == "Slight whistle from the engine at idle"
    assert snapshot.user_report.warning_lights_seen == [WarningLight.STEADY_MIL]


def test_scan_json_prints_the_stored_snapshot(tmp_path: Path) -> None:
    result = scan(tmp_path, "--scenario", "s08_recently_cleared", "--json")
    assert result.exit_code == 0, result.output
    printed = Snapshot.model_validate_json(result.stdout)
    assert printed == stored(tmp_path)
    assert printed.dtcs == [] and printed.mil_on is False


def test_scan_summary_lists_incomplete_monitors(tmp_path: Path) -> None:
    result = scan(tmp_path, "--scenario", "s08")
    assert (
        "  Readiness:   4 of 7 supported monitors complete "
        "(incomplete: catalyst, evap, o2_sensor)" in result.stdout.splitlines()
    )
    assert "  MIL:         off" in result.stdout.splitlines()


def test_scan_scenario_asks_nothing_and_symptoms_override(tmp_path: Path) -> None:
    result = scan(tmp_path, "--scenario", "s01", "--symptoms", "Hissing noise", stdin="")
    assert result.exit_code == 0, result.output
    snapshot = stored(tmp_path)
    assert snapshot is not None and snapshot.user_report.symptoms_text == "Hissing noise"


def test_scan_overlong_symptoms_are_cut_so_the_snapshot_can_be_read_back(tmp_path: Path) -> None:
    result = scan(tmp_path, "--scenario", "s01", "--symptoms", "x" * 2500)
    assert result.exit_code == 0, result.output
    snapshot = stored(tmp_path)  # raised ValidationError before the fix
    assert snapshot is not None and len(snapshot.user_report.symptoms_text) == 2000


def test_scan_condition_and_vin_flags(tmp_path: Path) -> None:
    result = scan(
        tmp_path, "--scenario", "s01", "--condition", "hold_2500", "--vin", "wvwzzzauzjw123456"
    )
    assert result.exit_code == 0, result.output
    snapshot = stored(tmp_path)
    assert snapshot is not None
    assert snapshot.conditions_captured == [Condition.HOLD_2500]
    assert (snapshot.vin, snapshot.vin_source) == ("WVWZZZAUZJW123456", "user")
    assert "  VIN:         WVW**********3456" in result.stdout.splitlines()


def test_scan_twice_stores_two_snapshots(tmp_path: Path) -> None:
    first = scan(tmp_path, "--scenario", "s01").stdout.splitlines()[0]
    second = scan(tmp_path, "--scenario", "s01").stdout.splitlines()[0]
    assert first != second


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ([], "Give --source, or --scenario to use the simulator."),
        (["--source", "simulator"], "--source simulator requires --scenario."),
        (
            ["--source", "emulator", "--scenario", "s01"],
            "--scenario requires --source simulator, not emulator.",
        ),
        (
            ["--scenario", "s01", "--condition", "cold_start", "--condition", "warm"],
            "--condition cold_start and --condition warm cannot be combined.",
        ),
        (["--scenario", "s01", "--vin", "TOOSHORT"], "A VIN is 17 characters"),
    ],
)
def test_scan_usage_errors_exit_2(tmp_path: Path, args: list[str], message: str) -> None:
    result = scan(tmp_path, *args)
    assert result.exit_code == 2
    assert message in result.stderr
    assert stored(tmp_path) is None


@pytest.mark.parametrize(
    "args", [["--source", "ble"], ["--scenario", "s01", "--condition", "cruise"], ["--replay", "x"]]
)
def test_scan_unknown_choices_and_flags_exit_2(tmp_path: Path, args: list[str]) -> None:
    assert scan(tmp_path, *args).exit_code == 2


def test_scan_unknown_scenario_exits_1(tmp_path: Path) -> None:
    result = scan(tmp_path, "--scenario", "s77")
    assert result.exit_code == 1
    assert "No scenario matches 's77'" in result.stderr


def test_scan_undefined_condition_for_the_scenario_exits_1(tmp_path: Path) -> None:
    result = scan(tmp_path, "--scenario", "s03", "--condition", "hold_2500")
    assert result.exit_code == 1
    assert "does not define condition 'hold_2500'" in result.stderr


def test_scan_connection_failure_exits_3(tmp_path: Path) -> None:
    with socket.socket() as probe:  # a port nothing listens on
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    result = scan(tmp_path, "--source", "emulator", "--port", str(port), "--no-input")
    assert result.exit_code == 3
    assert f"Cannot connect to 127.0.0.1:{port}" in result.stderr
    assert stored(tmp_path) is None


def test_mask_vin() -> None:
    assert mask_vin("WVWZZZAUZJW123456") == "WVW**********3456"
    assert mask_vin(None) == "not read"
    assert mask_vin("SHORT") == "*****"
