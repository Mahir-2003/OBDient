"""ELM327Vehicle over TCP against ELM327-emulator (dev dependency, pinned).

Run with: uv run pytest -m emulator

The emulator is only driven through its public Python API; none of its code is copied here.
The full read goes through `collect()`, the same path as `obdient scan`.
"""

import socket
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from obdient.cli import app
from obdient.schemas.common import Condition, SnapshotSource
from obdient.schemas.snapshot import DtcStatus, ReadErrorKind, UserReport, WarningLight
from obdient.storage.db import Database
from obdient.vehicle.collector import collect
from obdient.vehicle.elm327 import ELM327Vehicle
from obdient.vehicle.pids import DEFAULT_LIVE_PIDS
from obdient.vehicle.transports.tcp import TcpTransport

pytestmark = pytest.mark.emulator

STORED, PENDING, PERMANENT = ["0171"], ["0300"], ["0171"]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_for_port(port: int, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


@pytest.fixture
def emulator() -> Iterator[tuple[Any, int]]:
    """A fresh emulator on a free port, 'car' scenario, with one stored code."""
    from elm import Elm, obd_message  # pyright: ignore[reportMissingTypeStubs]

    saved = (obd_message.DTC_STORED[:], obd_message.DTC_PENDING[:], obd_message.DTC_PERMANENT[:])
    obd_message.DTC_STORED[:] = STORED
    obd_message.DTC_PENDING[:] = PENDING
    obd_message.DTC_PERMANENT[:] = PERMANENT
    port = _free_port()
    try:
        with Elm(net_port=port) as emu:
            emu.set_sorted_obd_msg("car")
            emu.delay = 0
            _wait_for_port(port)
            time.sleep(0.2)  # let the emulator drop the probe connection
            yield emu, port
    finally:
        (
            obd_message.DTC_STORED[:],
            obd_message.DTC_PENDING[:],
            obd_message.DTC_PERMANENT[:],
        ) = saved


def _vehicle(port: int, timeout_s: float = 2.0) -> ELM327Vehicle:
    return ELM327Vehicle(
        TcpTransport(port=port), SnapshotSource.EMULATOR, default_timeout_s=timeout_s
    )


def test_full_collect_decodes_everything_and_clears_nothing(emulator: tuple[Any, int]) -> None:
    from elm import obd_message  # pyright: ignore[reportMissingTypeStubs]

    emu, port = emulator
    snapshot = collect(_vehicle(port), UserReport(symptoms_text="test"), sample_interval_s=0)

    assert snapshot.source is SnapshotSource.EMULATOR
    assert snapshot.adapter.id_string == "ELM327 v1.5"
    assert snapshot.adapter.protocol_number == "A6"
    assert snapshot.adapter.voltage is not None and 10.0 < snapshot.adapter.voltage < 16.0

    supported = snapshot.supported_pids
    assert "010C" in supported and supported == sorted(supported)
    assert snapshot.vin is not None and len(snapshot.vin) == 17
    assert snapshot.vin_source == "mode09"

    assert (snapshot.mil_on, snapshot.dtc_count) == (True, 1)
    assert len(snapshot.readiness) == 11

    assert {(d.code, d.status) for d in snapshot.dtcs} == {
        ("P0171", DtcStatus.STORED),
        ("P0300", DtcStatus.PENDING),
        ("P0171", DtcStatus.PERMANENT),
    }
    assert snapshot.freeze_frame is None  # the emulator's car has no freeze frame
    assert snapshot.since_clear.warmups is not None
    assert snapshot.since_clear.distance_km is not None

    live = [pid for pid in DEFAULT_LIVE_PIDS if pid in supported]
    assert len(live) >= 10
    assert {r.pid for r in snapshot.readings} == set(live)
    assert len(snapshot.readings) == 3 * len(live)  # three samples of each
    assert all(isinstance(r.value, float) and r.raw for r in snapshot.readings)
    assert {r.condition for r in snapshot.readings} == {Condition.UNKNOWN}
    assert snapshot.conditions_captured == [Condition.UNKNOWN]
    assert snapshot.ecus == ["7E8"]
    assert snapshot.read_errors == []

    # The proof: nothing we sent cleared codes.
    assert not emu.counters.get("cmd_dtc_cleared")
    assert obd_message.DTC_STORED == STORED


def test_scan_command_reads_the_emulator_and_stores_a_snapshot(
    emulator: tuple[Any, int], tmp_path: Path
) -> None:
    emu, port = emulator
    home = tmp_path / "home"
    args = ["--home", str(home), "scan", "--source", "emulator", "--port", str(port)]

    result = CliRunner().invoke(app, [*args, "--no-input", "--symptoms", "rough idle"])

    assert result.exit_code == 0, result.output
    assert "stored: P0171; pending: P0300; permanent: P0171" in result.stdout
    database = Database(home / "obdient.db")
    try:
        snapshot = database.latest_snapshot()
    finally:
        database.close()
    assert snapshot is not None and snapshot.source is SnapshotSource.EMULATOR
    assert snapshot.user_report.symptoms_text == "rough idle"
    assert snapshot.vin is not None and snapshot.vin not in result.stdout  # masked
    assert not emu.counters.get("cmd_dtc_cleared")


def test_scan_command_runs_intake_after_the_read(emulator: tuple[Any, int], tmp_path: Path) -> None:
    _, port = emulator
    home = tmp_path / "home"
    # Answers: symptoms, started, conditions, flashing, fuel, smoke, brakes, steering, oil,
    # abs/airbag, odometer.
    answers = "whistle\nlast week\nidle\nn\nn\nn\ny\nn\nn\nn\n98000\n"
    result = CliRunner().invoke(
        app, ["--home", str(home), "scan", "--source", "emulator", "--port", str(port)], answers
    )

    assert result.exit_code == 0, result.output
    database = Database(home / "obdient.db")
    try:
        snapshot = database.latest_snapshot()
    finally:
        database.close()
    assert snapshot is not None
    report = snapshot.user_report
    assert (report.symptoms_text, report.started_when, report.odometer_km) == (
        "whistle",
        "last week",
        98000,
    )
    assert report.warning_lights_seen == [WarningLight.STEADY_MIL]  # the car said the MIL is on
    assert report.checklist.brake_problem is True and report.checklist.fuel_smell is False
    assert "Describe what you've noticed (Enter to skip): " in result.stdout
    assert "skip)::" not in result.stdout  # typer's own ": " suffix is turned off


def test_scan_command_keeps_the_scan_when_intake_is_cut_short(
    emulator: tuple[Any, int], tmp_path: Path
) -> None:
    _, port = emulator
    home = tmp_path / "home"
    result = CliRunner().invoke(
        app, ["--home", str(home), "scan", "--source", "emulator", "--port", str(port)], "whi"
    )

    assert result.exit_code == 0, result.output
    assert "Intake questions skipped." in result.stderr
    database = Database(home / "obdient.db")
    try:
        snapshot = database.latest_snapshot()
    finally:
        database.close()
    assert snapshot is not None and snapshot.dtc_count == 1


def test_scan_command_prompts_for_a_condition_and_relabels_a_wrong_one(
    emulator: tuple[Any, int], tmp_path: Path
) -> None:
    """The emulator idles near 900 rpm, so a declared hold_2500 is contradicted."""
    _, port = emulator
    home = tmp_path / "home"
    args = ["--home", str(home), "scan", "--source", "emulator", "--port", str(port)]
    result = CliRunner().invoke(app, [*args, "--condition", "hold_2500", "--json"], "\n" * 12)

    assert result.exit_code == 0, result.output
    database = Database(home / "obdient.db")
    try:
        snapshot = database.latest_snapshot()
    finally:
        database.close()
    assert snapshot is not None
    assert snapshot.conditions_captured == [Condition.UNKNOWN]
    assert [e.kind for e in snapshot.read_errors] == [ReadErrorKind.CONDITION_MISMATCH]


def test_the_cleared_counter_would_catch_a_mode_04(emulator: tuple[Any, int]) -> None:
    """Control for the test above: a raw mode 04, sent behind OBDient's back, is detected."""
    emu, port = emulator
    with socket.create_connection(("127.0.0.1", port), timeout=3) as raw:
        for command in (b"ATE0\r", b"04\r"):
            raw.sendall(command)
            buffer = b""
            while b">" not in buffer:
                buffer += raw.recv(4096)
    assert emu.counters.get("cmd_dtc_cleared")


def test_no_data_on_one_pid_is_recorded_and_the_rest_still_read(
    emulator: tuple[Any, int],
) -> None:
    emu, port = emulator
    vehicle = _vehicle(port)
    try:
        vehicle.connect()
        emu.answer["RPM"] = "NO DATA\r"
        readings = vehicle.read_pids(["010C", "010D"])
        errors = vehicle.drain_errors()
    finally:
        vehicle.close()
    assert [r.pid for r in readings] == ["010D"]
    assert [(e.request, e.kind) for e in errors] == [("010C", ReadErrorKind.NO_DATA)]


def test_searching_prefix_is_ignored(emulator: tuple[Any, int]) -> None:
    emu, port = emulator
    vehicle = _vehicle(port)
    try:
        vehicle.connect()
        emu.answer["SPEED"] = (
            "<string>SEARCHING...</string><writeln />"
            "<header>7E8</header><size>03</size><data>41 0D 3C</data>"
        )
        (reading,) = vehicle.read_pids(["010D"])
        errors = vehicle.drain_errors()
    finally:
        vehicle.close()
    assert reading.value == 60.0
    assert errors == []


def test_slow_replies_within_the_timeout_are_fine(emulator: tuple[Any, int]) -> None:
    emu, port = emulator
    vehicle = _vehicle(port)
    try:
        vehicle.connect()
        emu.delay = 0.3
        (reading,) = vehicle.read_pids(["010C"])
    finally:
        emu.delay = 0
        vehicle.close()
    assert reading.name == "rpm"


def test_replies_slower_than_the_timeout_become_a_timeout_error(
    emulator: tuple[Any, int],
) -> None:
    emu, port = emulator
    vehicle = _vehicle(port)
    try:
        vehicle.connect()
        vehicle.supported_pids()
        vehicle.timeout_s = 0.15  # setup needs the normal timeout: ATZ alone takes 0.5 s
        emu.delay = 0.6
        assert vehicle.read_pids(["010C"]) == []
        errors = vehicle.drain_errors()
        emu.delay = 0
        time.sleep(1.5)  # both late replies arrive; the next send discards them
        (reading,) = vehicle.read_pids(["010C"])
    finally:
        emu.delay = 0
        vehicle.close()
    assert [(e.request, e.kind) for e in errors] == [("010C", ReadErrorKind.TIMEOUT)]
    assert reading.name == "rpm"
