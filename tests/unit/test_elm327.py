"""ELM327Vehicle against a scripted transport: setup sequence and request planning (SPEC §4.4)."""

from datetime import UTC, datetime

import pytest

from obdient.errors import (
    TransportError,
    TransportTimeout,
    UnsupportedProtocolError,
    VehicleConnectionError,
)
from obdient.schemas.common import Condition, SnapshotSource
from obdient.schemas.snapshot import DtcStatus, ReadErrorKind
from obdient.vehicle.allowlist import AllowedCommand
from obdient.vehicle.elm327 import ELM327Vehicle

NOW = datetime(2026, 11, 12, 18, 30, tzinfo=UTC)
Reply = str | Exception

SETUP: dict[str, list[Reply]] = {
    "ATZ": ["\r\rELM327 v1.5\r\r"],
    "ATE0": ["ATE0\rOK\r\r"],
    "ATL0": ["OK\r\r"],
    "ATS0": ["OK\r\r"],
    "ATH1": ["OK\r\r"],
    "ATCAF1": ["OK\r\r"],
    "ATAT1": ["OK\r\r"],
    "ATSP0": ["OK\r\r"],
    "ATI": ["ELM327 v1.5\r\r"],
    "AT@1": ["OBDII to RS232 Interpreter\r\r"],
    "STI": ["?\r\r"],
    "STDI": ["?\r\r"],
    "ATRV": ["14.1V\r\r"],
    # Supported: 01 03 04 05 06 07 0B 0C 0D 0E 0F 10 11 13 15 1C 1F 20
    "0100": ["SEARCHING...\r7E8064100BE3FA813\r\r"],
    "ATDPN": ["A6\r\r"],
    # Supported: 21 2F 30 31; no further range
    "0120": ["7E806412080038000\r\r"],
}
CONNECT_ORDER = [
    "ATZ", "ATE0", "ATL0", "ATS0", "ATH1", "ATCAF1", "ATAT1", "ATSP0",
    "ATI", "AT@1", "STI", "STDI", "ATRV", "0100", "ATDPN",
]  # fmt: skip


class FakeTransport:
    """Replies from a script: each command has a list, consumed in order; the last one repeats."""

    def __init__(self, script: dict[str, list[Reply]], open_error: Exception | None = None):
        self.script = {command: list(replies) for command, replies in script.items()}
        self.sent: list[str] = []
        self.opened = False
        self.closed = False
        self._open_error = open_error

    def open(self) -> None:
        if self._open_error:
            raise self._open_error
        self.opened = True

    def close(self) -> None:
        self.closed = True

    def send(self, command: AllowedCommand, timeout_s: float) -> bytes:
        assert isinstance(command, AllowedCommand)
        self.sent.append(command.text)
        replies = self.script.get(command.text, ["NO DATA\r\r"])
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply.encode()


def connected(extra: dict[str, list[Reply]] | None = None) -> tuple[ELM327Vehicle, FakeTransport]:
    transport = FakeTransport({**SETUP, **(extra or {})})
    vehicle = ELM327Vehicle(transport, SnapshotSource.EMULATOR, clock=lambda: NOW)
    vehicle.connect()
    transport.sent.clear()
    return vehicle, transport


# ---- connect ----------------------------------------------------------------


def test_connect_sends_the_setup_sequence_in_order() -> None:
    transport = FakeTransport(SETUP)
    vehicle = ELM327Vehicle(transport, SnapshotSource.EMULATOR)
    info = vehicle.connect()
    assert transport.opened
    assert transport.sent == CONNECT_ORDER
    assert info.id_string == "ELM327 v1.5"
    assert info.device_description == "OBDII to RS232 Interpreter"
    assert (info.stn_id, info.stn_device) == (None, None)  # "?" on a plain ELM327
    assert info.protocol_number == "A6"
    assert info.voltage == 14.1
    assert vehicle.source is SnapshotSource.EMULATOR


def test_stn_identification_and_unparsable_voltage() -> None:
    script = {**SETUP, "STI": ["STN2255 v5.6.19\r\r"], "STDI": ["OBDLink CX r2.1\r\r"]}
    info = ELM327Vehicle(
        FakeTransport({**script, "ATRV": ["LOW\r\r"]}), SnapshotSource.BLE
    ).connect()
    assert (info.stn_id, info.stn_device) == ("STN2255 v5.6.19", "OBDLink CX r2.1")
    assert info.voltage is None


@pytest.mark.parametrize(
    "override,message",
    [
        ({"ATZ": ["\r\rHELLO\r\r"]}, "Adapter did not identify itself after ATZ: HELLO"),
        ({"ATH1": ["?\r\r"]}, "Setup command ATH1 failed: ?"),
        ({"0100": ["SEARCHING...\rUNABLE TO CONNECT\r\r"]},
         "Vehicle not responding (UNABLE TO CONNECT); ignition may be off."),
        ({"0100": ["NO DATA\r\r"]}, "Vehicle not responding (NO DATA); ignition may be off."),
        ({"0100": [TransportTimeout("t")]},
         "Vehicle not responding (TIMEOUT); ignition may be off."),
        ({"ATI": [TransportTimeout("no prompt")]}, "Adapter not responding to ATI: no prompt"),
    ],
)  # fmt: skip
def test_connect_failures(override: dict[str, list[Reply]], message: str) -> None:
    vehicle = ELM327Vehicle(FakeTransport({**SETUP, **override}), SnapshotSource.EMULATOR)
    with pytest.raises(VehicleConnectionError) as info:
        vehicle.connect()
    assert str(info.value) == message


def test_connect_wraps_a_transport_that_cannot_open() -> None:
    transport = FakeTransport(SETUP, open_error=TransportError("Cannot connect to 127.0.0.1:1"))
    with pytest.raises(VehicleConnectionError, match="Cannot connect"):
        ELM327Vehicle(transport, SnapshotSource.EMULATOR).connect()


@pytest.mark.parametrize("protocol,shown", [("A3", "3"), ("7", "7"), ("A9", "9")])
def test_non_can_11_bit_protocols_are_refused(protocol: str, shown: str) -> None:
    vehicle = ELM327Vehicle(
        FakeTransport({**SETUP, "ATDPN": [f"{protocol}\r\r"]}), SnapshotSource.EMULATOR
    )
    with pytest.raises(UnsupportedProtocolError) as info:
        vehicle.connect()
    assert str(info.value) == f"Protocol {shown} is not supported in v0.1 (CAN 11-bit only)."


def test_protocol_8_is_accepted() -> None:
    ELM327Vehicle(FakeTransport({**SETUP, "ATDPN": ["8\r\r"]}), SnapshotSource.EMULATOR).connect()


def test_close_closes_the_transport() -> None:
    vehicle, transport = connected()
    vehicle.close()
    assert transport.closed


# ---- supported PIDs ---------------------------------------------------------


def test_supported_pids_follow_the_range_chain_and_are_cached() -> None:
    vehicle, transport = connected()
    pids = vehicle.supported_pids()
    assert pids == sorted(pids)
    assert pids[:3] == ["0101", "0103", "0104"]
    assert pids[-5:] == ["0120", "0121", "012F", "0130", "0131"]
    assert transport.sent == ["0120"]  # 0100 was answered during connect; 0140 is not advertised
    vehicle.supported_pids()
    assert transport.sent == ["0120"]


# ---- read_pids --------------------------------------------------------------


def test_read_pids_groups_known_lengths_into_one_request() -> None:
    vehicle, transport = connected({"010C0D05": ["7E81008410C0C300D00\r7E8210582AAAAAAAAAA\r\r"]})
    readings = vehicle.read_pids(["010C", "010D", "0105"], Condition.IDLE)
    assert transport.sent == ["0120", "010C0D05"]
    assert [(r.pid, r.name, r.value, r.unit, r.raw) for r in readings] == [
        ("010C", "rpm", 780.0, "rpm", "0C30"),
        ("010D", "speed_kmh", 0.0, "km/h", "00"),
        ("0105", "ect_c", 90.0, "°C", "82"),
    ]
    assert all(r.ecu == "7E8" and r.condition is Condition.IDLE and r.ts == NOW for r in readings)
    assert vehicle.drain_errors() == []


def test_more_than_six_pids_are_split_into_chunks() -> None:
    vehicle, transport = connected()
    vehicle.read_pids(["0104", "0105", "0106", "0107", "010B", "010C", "010D", "010E"])
    assert transport.sent[1:3] == ["010405060 70B0C".replace(" ", ""), "010D0E"]


def test_unsupported_pids_are_recorded_and_never_sent() -> None:
    vehicle, transport = connected({"010C": ["7E804410C0C30\r\r"]})
    readings = vehicle.read_pids(["0142", "010C"])
    assert [r.pid for r in readings] == ["010C"]
    assert "0142" not in transport.sent
    (error,) = vehicle.drain_errors()
    assert (error.request, error.kind) == ("0142", ReadErrorKind.UNSUPPORTED)
    assert vehicle.drain_errors() == []  # draining clears


def test_failed_group_falls_back_to_one_request_per_pid() -> None:
    vehicle, transport = connected(
        {"010C0D": ["NO DATA\r\r"], "010C": ["7E804410C0C30\r\r"], "010D": ["NO DATA\r\r"]}
    )
    readings = vehicle.read_pids(["010C", "010D"])
    assert transport.sent == ["0120", "010C0D", "010C", "010D"]
    assert [r.pid for r in readings] == ["010C"]
    (error,) = vehicle.drain_errors()  # one error per failed PID, none for the group
    assert (error.request, error.kind) == ("010D", ReadErrorKind.NO_DATA)


def test_pid_missing_from_a_group_reply_is_requested_alone() -> None:
    vehicle, transport = connected({"010C0D": ["7E804410C0C30\r\r"], "010D": ["7E803410D3C\r\r"]})
    readings = vehicle.read_pids(["010C", "010D"])
    assert transport.sent == ["0120", "010C0D", "010D"]
    assert [(r.pid, r.value) for r in readings] == [("010C", 780.0), ("010D", 60.0)]


def test_pid_without_a_decoder_is_carried_raw() -> None:
    vehicle, _ = connected({"0103": ["7E80441030200\r\r"]})
    (reading,) = vehicle.read_pids(["0103"])
    assert (reading.name, reading.value, reading.unit, reading.raw) == ("0103", None, None, "0200")


def test_timeout_is_retried_once() -> None:
    vehicle, transport = connected({"010C": [TransportTimeout("t"), "7E804410C0C30\r\r"]})
    assert [r.value for r in vehicle.read_pids(["010C"])] == [780.0]
    assert transport.sent.count("010C") == 2
    assert vehicle.drain_errors() == []


def test_two_timeouts_become_a_read_error() -> None:
    vehicle, transport = connected({"010C": [TransportTimeout("t")]})
    assert vehicle.read_pids(["010C"]) == []
    assert transport.sent.count("010C") == 2
    (error,) = vehicle.drain_errors()
    assert (error.request, error.kind, error.ts) == ("010C", ReadErrorKind.TIMEOUT, NOW)


def test_engine_ecu_wins_for_live_values() -> None:
    vehicle, _ = connected({"010C": ["7E904410C1F40\r7E804410C0C30\r\r"]})
    (reading,) = vehicle.read_pids(["010C"])
    assert (reading.ecu, reading.value) == ("7E8", 780.0)
    assert vehicle.ecus == {"7E8", "7E9"}


def test_garbage_and_negative_replies_are_recorded() -> None:
    vehicle, _ = connected({"010C": ["7E804410C0C3\r\r"], "010D": ["7E8037F0112\r\r"]})
    assert vehicle.read_pids(["010C", "010D"]) == []
    by_request = {e.request: e for e in vehicle.drain_errors()}
    assert by_request["010C"].kind is ReadErrorKind.PARSE_ERROR
    assert by_request["010D"].kind is ReadErrorKind.NO_DATA
    assert by_request["010D"].detail == "negative response 0x12"


# ---- status, codes, freeze frame, VIN, since-clear --------------------------


def test_status() -> None:
    vehicle, _ = connected({"0101": ["7E80641018107A121\r\r"]})
    status = vehicle.status()
    assert status is not None
    assert (status.mil_on, status.dtc_count, len(status.monitors)) == (True, 1, 11)
    assert vehicle.drain_errors() == []


def test_status_failure_returns_none_and_records() -> None:
    vehicle, _ = connected({"0101": ["CAN ERROR\r\r"]})
    assert vehicle.status() is None
    assert [e.kind for e in vehicle.drain_errors()] == [ReadErrorKind.CAN_ERROR]


def test_diesel_status_records_unsupported_monitors() -> None:
    vehicle, _ = connected({"0101": ["7E8064101000FFFFF\r\r"]})
    assert vehicle.status() is not None
    (error,) = vehicle.drain_errors()
    assert (error.kind, error.detail) == (
        ReadErrorKind.UNSUPPORTED,
        "compression-ignition monitors",
    )


def test_read_dtcs_keeps_every_ecu_and_treats_no_data_as_none() -> None:
    vehicle, transport = connected(
        {
            "03": ["7E804430101710000\r7E9044301C1000000\r\r"],
            "07": ["7E80447010300\r\r"],
            "0A": ["NO DATA\r\r"],
        }
    )
    dtcs = vehicle.read_dtcs()
    assert transport.sent == ["03", "07", "0A"]
    assert [(d.code, d.status, d.ecu) for d in dtcs] == [
        ("P0171", DtcStatus.STORED, "7E8"),
        ("U0100", DtcStatus.STORED, "7E9"),
        ("P0300", DtcStatus.PENDING, "7E8"),
    ]
    assert vehicle.drain_errors() == []


def test_read_dtcs_records_real_errors() -> None:
    vehicle, _ = connected({"03": ["BUFFER FULL\r\r"]})
    assert vehicle.read_dtcs() == []
    assert [e.request for e in vehicle.drain_errors()] == ["03"]


@pytest.mark.parametrize("reply", ["7E8054202000000\r\r", "NO DATA\r\r"])
def test_no_freeze_frame(reply: str) -> None:
    vehicle, transport = connected({"020200": [reply]})
    assert vehicle.freeze_frame() is None
    assert transport.sent == ["020200"]
    assert vehicle.drain_errors() == []


def test_freeze_frame_reads_each_supported_decodable_pid() -> None:
    vehicle, transport = connected(
        {
            "020200": ["7E8054202000171\r\r"],
            "020000": ["7E807420000 7E180000\r\r".replace(" ", "")],  # PIDs 02 03 04 05 06 07 0C 0D
            "020400": ["7E8044204007F\r\r"],
            "020500": ["7E80442050080\r\r"],
            "020600": ["7E80442060088\r\r"],
            "020700": ["7E80442070099\r\r"],
            "020C00": ["7E805420C000C30\r\r"],
            "020D00": ["NO DATA\r\r"],
        }
    )
    frame = vehicle.freeze_frame()
    assert frame is not None
    assert (frame.trigger_code, frame.ecu) == ("P0171", "7E8")
    assert transport.sent == [
        "020200", "020000", "020400", "020500", "020600", "020700", "020C00", "020D00",
    ]  # fmt: skip
    assert {r.name: r.value for r in frame.readings} == {
        "load_pct": 49.8,
        "ect_c": 88.0,
        "stft_b1": 6.2,
        "ltft_b1": 19.5,
        "rpm": 780.0,
    }
    assert all(r.condition is Condition.UNKNOWN for r in frame.readings)
    assert vehicle.drain_errors() == []


def test_vin() -> None:
    vehicle, _ = connected(
        {"0902": ["7E81014490201575657\r7E8215A5A5A41555A4A\r7E82257303030303030\r\r"]}
    )
    assert vehicle.vin() == "WVWZZZAUZJW000000"


def test_vin_failures_are_recorded() -> None:
    vehicle, _ = connected({"0902": ["7E8037F0912\r\r"]})
    assert vehicle.vin() is None
    bad, _ = connected({"0902": ["7E806490201414243\r\r"]})
    assert bad.vin() is None
    assert [e.kind for e in vehicle.drain_errors()] == [ReadErrorKind.NO_DATA]
    assert [e.kind for e in bad.drain_errors()] == [ReadErrorKind.PARSE_ERROR]


def test_since_clear_reads_only_supported_counters() -> None:
    # Payload, 9 bytes: 41 | 21 0000 | 30 2D | 31 0334
    vehicle, transport = connected(
        {"01213031": ["7E81009412100003 02D\r7E821310334\r\r".replace(" ", "")]}
    )
    since = vehicle.since_clear()
    assert transport.sent == ["0120", "01213031"]  # 014D and 014E are not supported: not asked
    assert since.warmups == 45
    assert since.distance_km == 820.0
    assert since.distance_mil_on_km == 0.0
    assert since.minutes_mil_on is None
    assert since.minutes_since_clear is None
    assert vehicle.drain_errors() == []


# ---- review fixes -----------------------------------------------------------


@pytest.mark.parametrize("reply", ["?\rOK\r\r", "ATH1\rOK\r\r", "OK OK\r\r"])
def test_setup_reply_must_be_exactly_ok(reply: str) -> None:
    vehicle = ELM327Vehicle(FakeTransport({**SETUP, "ATH1": [reply]}), SnapshotSource.EMULATOR)
    with pytest.raises(VehicleConnectionError, match="Setup command ATH1 failed"):
        vehicle.connect()


def test_the_protocol_search_is_not_retried() -> None:
    transport = FakeTransport({**SETUP, "0100": [TransportTimeout("t")]})
    with pytest.raises(VehicleConnectionError):
        ELM327Vehicle(transport, SnapshotSource.EMULATOR).connect()
    assert transport.sent.count("0100") == 1


def test_negative_response_to_a_code_request_is_recorded_not_read_as_none() -> None:
    vehicle, _ = connected({"03": ["7E8037F0311\r\r"], "07": ["NO DATA\r\r"]})
    assert vehicle.read_dtcs() == []
    (error,) = vehicle.drain_errors()
    assert (error.request, error.kind, error.detail) == (
        "03",
        ReadErrorKind.NO_DATA,
        "negative response 0x11",
    )


def test_response_pending_does_not_hide_the_real_answer() -> None:
    vehicle, _ = connected({"03": ["7E8037F0378\r7E806430101710000\r\r"]})
    assert [d.code for d in vehicle.read_dtcs()] == ["P0171"]
    assert vehicle.drain_errors() == []


@pytest.mark.parametrize("reply", ["7E8024130\r\r", "7E8037F0112\r\r", "7E804413\r\r"])
def test_since_clear_never_raises(reply: str) -> None:
    script: dict[str, list[Reply]] = {"01213031": ["NO DATA\r\r"], "0130": [reply]}
    vehicle, _ = connected(script)
    since = vehicle.since_clear()
    assert since.warmups is None
    assert "0130" in {e.request for e in vehicle.drain_errors()}
