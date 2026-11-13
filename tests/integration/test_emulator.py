"""ELM327Vehicle over TCP against ELM327-emulator (dev dependency, pinned).

Run with: uv run pytest -m emulator

The emulator is only driven through its public Python API; none of its code is copied here.
M3 replaces the hand-rolled read sequence in `full_read` with `collect()`.
"""

import socket
import time
from collections.abc import Iterator
from typing import Any

import pytest

from obdient.schemas.common import Condition, SnapshotSource
from obdient.schemas.snapshot import DtcStatus, ReadErrorKind
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


def test_full_read_decodes_everything_and_clears_nothing(emulator: tuple[Any, int]) -> None:
    from elm import obd_message  # pyright: ignore[reportMissingTypeStubs]

    emu, port = emulator
    vehicle = _vehicle(port)
    try:
        info = vehicle.connect()
        supported = vehicle.supported_pids()
        vin = vehicle.vin()
        status = vehicle.status()
        dtcs = vehicle.read_dtcs()
        freeze = vehicle.freeze_frame()
        since = vehicle.since_clear()
        live = [pid for pid in DEFAULT_LIVE_PIDS if pid in supported]
        readings = vehicle.read_pids(live, Condition.IDLE)
        errors = vehicle.drain_errors()
    finally:
        vehicle.close()

    assert info.id_string == "ELM327 v1.5"
    assert info.protocol_number == "A6"
    assert info.voltage is not None and 10.0 < info.voltage < 16.0

    assert "010C" in supported and supported == sorted(supported)
    assert vin is not None and len(vin) == 17

    assert status is not None
    assert (status.mil_on, status.dtc_count) == (True, 1)
    assert len(status.monitors) == 11

    assert {(d.code, d.status) for d in dtcs} == {
        ("P0171", DtcStatus.STORED),
        ("P0300", DtcStatus.PENDING),
        ("P0171", DtcStatus.PERMANENT),
    }
    assert all(d.ecu == "7E8" for d in dtcs)
    assert freeze is None  # the emulator's car has no freeze frame

    assert since.warmups is not None and since.distance_km is not None

    assert len(live) >= 10
    assert {r.pid for r in readings} == set(live)
    assert all(isinstance(r.value, float) and r.raw and r.ecu == "7E8" for r in readings)
    assert errors == []

    # The proof: nothing we sent cleared codes.
    assert not emu.counters.get("cmd_dtc_cleared")
    assert obd_message.DTC_STORED == STORED


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
