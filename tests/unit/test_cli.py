import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from obdient import __version__
from obdient.agent.safety import RECALLS_NOTE
from obdient.cli import app, mask_vin
from obdient.knowledge.nhtsa import FixtureCache, NhtsaResult
from obdient.schemas.common import Condition
from obdient.schemas.snapshot import Snapshot, WarningLight
from obdient.storage.db import Database
from support import GTI_FIXTURE, SCENARIOS_DIR

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


# ---- nhtsa (SPEC §13.5) ------------------------------------------------------


def nhtsa(tmp_path: Path, *args: str) -> Result:
    return runner.invoke(app, ["--home", str(tmp_path / "home"), "nhtsa", *args])


def seed_cache(tmp_path: Path) -> None:
    """Put the GTI fixture responses into the database cache, as a live run would."""
    fixtures = FixtureCache(GTI_FIXTURE)
    database = Database(tmp_path / "home" / "obdient.db")
    for endpoint in ("recalls", "complaints"):
        for model in ("GOLF GTI", "GTI"):
            key = f"{endpoint}|make=VOLKSWAGEN|model={model}|modelYear=2018"
            database.put(key, fixtures.get(key, None))
    database.put("vpic_decode|vin=WVWZZZAUZJW000000", fixtures.get("vpic_decode|vin=X", None))
    database.close()


GTI_ARGS = ["--make", "VOLKSWAGEN", "--model", "Golf GTI", "--year", "2018", "--offline"]


def test_nhtsa_shows_names_tried_and_where_each_recall_was_found(tmp_path: Path) -> None:
    seed_cache(tmp_path)
    result = nhtsa(tmp_path, *GTI_ARGS)

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert "Names tried: Golf GTI, GTI" in lines
    assert "  Golf GTI: 0 recalls, 33 complaints" in lines  # the name vPIC gives finds none
    assert "  GTI: 3 recalls, 17 complaints" in lines
    assert "Recalls: 3" in lines
    assert "  24V110000  2024-02-14  FUEL SYSTEM, GASOLINE  [found under: GTI]" in lines
    assert RECALLS_NOTE in lines and "https://www.nhtsa.gov/recalls" in lines
    assert "Owner complaints (unverified): 50" in lines
    assert not any(line.startswith("    ") for line in lines)  # no complaint text by default


def test_nhtsa_complaints_flag_prints_the_newest(tmp_path: Path) -> None:
    seed_cache(tmp_path)
    result = nhtsa(tmp_path, *GTI_ARGS, "--complaints", "2")
    lines = result.stdout.splitlines()
    start = lines.index("Owner complaints (unverified): 50")
    assert len(lines[start + 1 :]) == 4  # two complaints, two lines each
    assert "2026-09-15" in lines[start + 1]


def test_nhtsa_offline_uses_cached_answers_older_than_a_day(tmp_path: Path) -> None:
    then = datetime(2026, 11, 1, tzinfo=UTC)
    database = Database(tmp_path / "home" / "obdient.db", clock=lambda: then)
    fixtures = FixtureCache(GTI_FIXTURE)
    for endpoint in ("recalls", "complaints"):
        for model in ("GOLF GTI", "GTI"):
            key = f"{endpoint}|make=VOLKSWAGEN|model={model}|modelYear=2018"
            database.put(key, fixtures.get(key, None))
    database.close()

    result = nhtsa(tmp_path, *GTI_ARGS)  # run "now", well over 24 hours later, offline
    assert result.exit_code == 0, result.output
    assert "Recalls: 3" in result.stdout.splitlines()


def test_nhtsa_vw_is_an_alias_for_the_family_prefix_rule(tmp_path: Path) -> None:
    result = nhtsa(tmp_path, "--make", "VW", "--model", "Golf GTI", "--year", "2018", "--offline")
    assert "Names tried: Golf GTI, GTI" in result.stdout.splitlines()


def test_nhtsa_json(tmp_path: Path) -> None:
    seed_cache(tmp_path)
    result = nhtsa(tmp_path, *GTI_ARGS, "--json")
    parsed = NhtsaResult.model_validate_json(result.stdout)
    assert len(parsed.recalls) == 3 and parsed.candidate_names == ["Golf GTI", "GTI"]


def test_nhtsa_by_vin_decodes_first_and_never_prints_the_vin(tmp_path: Path) -> None:
    seed_cache(tmp_path)
    result = nhtsa(tmp_path, "--vin", "wvwzzzauzjw000000", "--offline")
    assert result.exit_code == 0, result.output
    assert "Vehicle: 2018 VOLKSWAGEN Golf GTI (2.0L 4-cyl Gasoline)" in result.stdout.splitlines()
    assert "Recalls: 3" in result.stdout.splitlines()
    assert "WVWZZZAUZJW000000" not in result.output


def test_nhtsa_zero_results_never_says_no_recalls(tmp_path: Path) -> None:
    database = Database(tmp_path / "home" / "obdient.db")
    for endpoint in ("recalls", "complaints"):
        database.put(f"{endpoint}|make=VOLKSWAGEN|model=GOLF|modelYear=2018", {"results": []})
    database.close()
    result = nhtsa(
        tmp_path, "--make", "VOLKSWAGEN", "--model", "Golf", "--year", "2018", "--offline"
    )
    assert result.exit_code == 0, result.output
    assert (
        "Recalls: none found under names: Golf. This is not proof that none exist."
        in result.stdout.splitlines()
    )
    assert "Owner complaints: none found under names: Golf." in result.stdout.splitlines()
    assert "no recalls" not in result.output.lower()


def test_nhtsa_offline_with_an_empty_cache_exits_1_and_claims_nothing(tmp_path: Path) -> None:
    result = nhtsa(tmp_path, *GTI_ARGS)
    assert result.exit_code == 1
    assert "Recalls: lookup failed; nothing is known either way." in result.stdout
    assert "none found" not in result.output
    assert "Lookup failed: recalls for 'Golf GTI': offline and not cached" in result.stderr

    by_vin = nhtsa(tmp_path, "--vin", "WVWZZZAUZJW000000", "--offline")
    assert by_vin.exit_code == 1 and "offline and not cached" in by_vin.stderr


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ([], "Give --vin, or --make, --model and --year together."),
        (["--make", "VOLKSWAGEN", "--model", "GTI"], "Give --vin, or --make, --model and --year"),
        (
            ["--vin", "WVWZZZAUZJW000000", "--make", "VOLKSWAGEN"],
            "Give either --vin or --make/--model/--year, not both.",
        ),
        ([*GTI_ARGS, "--refresh"], "--refresh and --offline cannot be combined."),
        (["--vin", "SHORT"], "A VIN is 17 characters"),
    ],
)
def test_nhtsa_usage_errors_exit_2(tmp_path: Path, args: list[str], message: str) -> None:
    result = nhtsa(tmp_path, *args)
    assert result.exit_code == 2 and message in result.stderr
