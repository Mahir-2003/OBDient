"""Reply cleaning, frame parsing, ISO-TP reassembly and the per-mode decoders (SPEC §4.4, §4.5)."""

import json
from pathlib import Path
from typing import Any

import pytest

from obdient.errors import ParseError
from obdient.schemas.snapshot import DtcStatus, MonitorName, ReadErrorKind
from obdient.vehicle import parsers
from obdient.vehicle.elm327 import CanFrame, clean_lines, parse_reply, reassemble

FIXTURES: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures/elm_responses/handwritten.json").read_text()
)
VIN = "WVWZZZAUZJW000000"
VIN_PAYLOAD = bytes.fromhex("490201") + VIN.encode()


def payloads(name: str) -> dict[str, bytes]:
    case = FIXTURES[name]
    reply = parse_reply(case["rx"].encode(), headers=case["headers"])
    assert reply.error is None, reply
    return reassemble(reply.frames)


# ---- clean_lines ------------------------------------------------------------


@pytest.mark.parametrize("ending", ["\r", "\n", "\r\n", "\n\r", "\r\r", "\r\n\r\n"])
def test_every_line_ending(ending: str) -> None:
    raw = f"7E8 04 41 0C 0C 30{ending}7E9 04 41 0C 0C 34{ending}".encode()
    assert clean_lines(raw) == ["7E8 04 41 0C 0C 30", "7E9 04 41 0C 0C 34"]


def test_nulls_blank_lines_and_searching_are_dropped() -> None:
    raw = b"\x00SEARCHING...\r\r  7E8 04 41 0C 0C 30 \r\x00\r"
    assert clean_lines(raw) == ["7E8 04 41 0C 0C 30"]


# ---- parse_reply ------------------------------------------------------------


@pytest.mark.parametrize("name", ["rpm_single_frame", "rpm_spaces_off", "rpm_headers_off"])
def test_single_frame_in_every_format(name: str) -> None:
    result = payloads(name)
    assert list(result.values()) == [bytes.fromhex("410C0C30")]
    assert list(result) == ([""] if name == "rpm_headers_off" else ["7E8"])


def test_searching_line_is_ignored() -> None:
    assert payloads("rpm_searching") == {"7E8": bytes.fromhex("4100BE3FA813")}


def test_multi_ecu_reply_keeps_every_ecu() -> None:
    assert payloads("rpm_two_ecus") == {
        "7E9": bytes.fromhex("410C0C30"),
        "7E8": bytes.fromhex("410C0C34"),
    }


@pytest.mark.parametrize(
    "line,kind",
    [
        ("NO DATA", ReadErrorKind.NO_DATA),
        ("UNABLE TO CONNECT", ReadErrorKind.UNABLE_TO_CONNECT),
        ("CAN ERROR", ReadErrorKind.CAN_ERROR),
        ("BUFFER FULL", ReadErrorKind.BUFFER_FULL),
        ("?", ReadErrorKind.UNKNOWN_COMMAND),
        ("STOPPED", ReadErrorKind.STOPPED),
        ("BUS INIT: ...ERROR", ReadErrorKind.ADAPTER_ERROR),
        ("BUS BUSY", ReadErrorKind.ADAPTER_ERROR),
        ("BUS ERROR", ReadErrorKind.ADAPTER_ERROR),
        ("FB ERROR", ReadErrorKind.ADAPTER_ERROR),
        ("DATA ERROR", ReadErrorKind.ADAPTER_ERROR),
        ("<DATA ERROR", ReadErrorKind.ADAPTER_ERROR),
        ("<RX ERROR", ReadErrorKind.ADAPTER_ERROR),
        ("ERR94", ReadErrorKind.ADAPTER_ERROR),
        ("LV RESET", ReadErrorKind.ADAPTER_ERROR),
        ("ACT ALERT", ReadErrorKind.ADAPTER_ERROR),
    ],
)
def test_error_tokens(line: str, kind: ReadErrorKind) -> None:
    reply = parse_reply(f"SEARCHING...\r{line}\r\r".encode(), headers=True)
    assert reply.error is kind
    assert reply.frames == []
    assert reply.detail == (line if kind is ReadErrorKind.ADAPTER_ERROR else None)


@pytest.mark.parametrize("raw", ["7E8 04 41 0C 0C 3\r", "HELLO\r", "7E8\r", "41 0C 0C 30\r"])
def test_garbage_with_headers_on_is_a_parse_error_for_the_whole_reply(raw: str) -> None:
    reply = parse_reply(f"7E8 04 41 0C 0C 30\r{raw}".encode(), headers=True)
    assert reply.error is ReadErrorKind.PARSE_ERROR
    assert reply.frames == []
    assert reply.detail is not None and raw.strip() in reply.detail


# ---- reassemble -------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["vin_multi_frame", "vin_headers_off", "vin_headers_off_spaces_off"]
)
def test_multi_frame_vin(name: str) -> None:
    (payload,) = payloads(name).values()
    assert payload == VIN_PAYLOAD
    assert parsers.decode_vin(payload) == VIN


def test_frames_of_two_ecus_may_interleave() -> None:
    assert payloads("vin_two_ecus_interleaved") == {
        "7E8": VIN_PAYLOAD,
        "7E9": bytes.fromhex("7F0911"),
    }


@pytest.mark.parametrize("name", ["vin_out_of_order", "vin_missing_frame"])
def test_out_of_order_or_missing_frames_raise(name: str) -> None:
    case = FIXTURES[name]
    reply = parse_reply(case["rx"].encode(), headers=True)
    with pytest.raises(ParseError):
        reassemble(reply.frames)


def test_padding_beyond_the_declared_length_is_dropped() -> None:
    assert payloads("multi_pid") == {"7E8": bytes.fromhex("410C0C300D000582")}
    padded = [CanFrame("7E8", bytes.fromhex("03410D00AAAAAAAA"))]
    assert reassemble(padded) == {"7E8": bytes.fromhex("410D00")}


def test_sequence_numbers_wrap_from_f_to_0() -> None:
    body = bytes(range(6 + 7 * 16))  # first frame + 16 consecutive frames
    frames = [CanFrame("7E8", bytes([0x10, len(body)]) + body[:6])]
    for n in range(16):
        chunk = body[6 + 7 * n : 13 + 7 * n]
        frames.append(CanFrame("7E8", bytes([0x20 | ((n + 1) & 0x0F)]) + chunk))
    assert frames[-1].data[0] == 0x20  # the 16th consecutive frame carries sequence 0
    assert reassemble(frames) == {"7E8": body}


@pytest.mark.parametrize(
    "data",
    ["", "0841", "00", "21AABB", "30000000", "10"],
    ids=["empty", "length>data", "zero-length", "orphan-consecutive", "flow-control", "short-ff"],
)
def test_malformed_frames_raise(data: str) -> None:
    with pytest.raises(ParseError):
        reassemble([CanFrame("7E8", bytes.fromhex(data))])


# ---- mode 01 ----------------------------------------------------------------


def test_split_multi_pid_reply() -> None:
    payload = payloads("multi_pid")["7E8"]
    assert parsers.split_mode01(payload[1:], ["010C", "010D", "0105"]) == {
        "010C": b"\x0c\x30",
        "010D": b"\x00",
        "0105": b"\x82",
    }


def test_split_accepts_any_order_and_missing_pids() -> None:
    assert parsers.split_mode01(bytes.fromhex("05820C0C30"), ["010C", "010D", "0105"]) == {
        "0105": b"\x82",
        "010C": b"\x0c\x30",
    }


def test_split_single_pid_without_a_known_length() -> None:
    assert parsers.split_mode01(bytes.fromhex("A6000186A0"), ["01A6"]) == {
        "01A6": bytes.fromhex("000186A0")
    }


@pytest.mark.parametrize(
    "payload,requested",
    [
        ("0D00", ["010C", "0105"]),  # a PID nobody asked for
        ("A600010C0C30", ["01A6", "010C"]),  # unknown length inside a multi-PID reply
        ("0C0C", ["010C", "0105"]),  # cut short
    ],
)
def test_split_failures(payload: str, requested: list[str]) -> None:
    with pytest.raises(ParseError):
        parsers.split_mode01(bytes.fromhex(payload), requested)


def test_supported_bitmap() -> None:
    # BE 3F A8 13: bit 7 of the first byte is PID 01, bit 0 of the fourth is PID 20.
    assert parsers.decode_supported(0x00, bytes.fromhex("BE3FA813")) == [
        "0101", "0103", "0104", "0105", "0106", "0107",
        "010B", "010C", "010D", "010E", "010F", "0110",
        "0111", "0113", "0115",
        "011C", "011F", "0120",
    ]  # fmt: skip
    assert parsers.decode_supported(0x20, bytes.fromhex("80038000")) == [
        "0121", "012F", "0130", "0131",
    ]  # fmt: skip
    assert parsers.decode_supported(0x40, bytes.fromhex("00000001")) == ["0160"]
    with pytest.raises(ParseError):
        parsers.decode_supported(0x00, b"\xbe\x3f")


def test_status_spark_ignition() -> None:
    # A=81: MIL on, 1 code. B=07: three continuous monitors supported, all complete.
    # C=A1: catalyst, O2 sensor, EGR supported. D=21: catalyst and O2 sensor incomplete.
    status = parsers.decode_status(bytes.fromhex("8107A121"))
    assert status.mil_on is True
    assert status.dtc_count == 1
    assert [m.name for m in status.monitors] == list(MonitorName)
    by_name = {m.name: (m.supported, m.complete) for m in status.monitors}
    assert by_name[MonitorName.MISFIRE] == (True, True)
    assert by_name[MonitorName.CATALYST] == (True, False)
    assert by_name[MonitorName.O2_SENSOR] == (True, False)
    assert by_name[MonitorName.EGR] == (True, True)
    assert by_name[MonitorName.EVAP] == (False, None)
    assert by_name[MonitorName.HEATED_CATALYST] == (False, None)


def test_status_continuous_incomplete_bits_and_mil_off() -> None:
    status = parsers.decode_status(bytes.fromhex("00570000"))  # B=57: fuel system incomplete
    assert (status.mil_on, status.dtc_count) == (False, 0)
    by_name = {m.name: (m.supported, m.complete) for m in status.monitors}
    assert by_name[MonitorName.MISFIRE] == (True, False)
    assert by_name[MonitorName.FUEL_SYSTEM] == (True, True)
    assert by_name[MonitorName.COMPONENTS] == (True, False)


def test_status_compression_ignition_marks_the_other_eight_unsupported() -> None:
    data = bytes.fromhex("000FFFFF")  # B bit 3 set
    assert parsers.is_compression_ignition(data)
    status = parsers.decode_status(data)
    assert [m.supported for m in status.monitors] == [True] * 3 + [False] * 8
    assert not parsers.is_compression_ignition(bytes.fromhex("0007FFFF"))


# ---- modes 03 / 07 / 0A -----------------------------------------------------


@pytest.mark.parametrize(
    "a,b,code",
    [
        (0x01, 0x71, "P0171"),
        (0x03, 0x02, "P0302"),
        (0x11, 0x9A, "P119A"),
        (0x3F, 0xFF, "P3FFF"),
        (0x40, 0x35, "C0035"),
        (0x91, 0x23, "B1123"),
        (0xC1, 0x00, "U0100"),
    ],
)
def test_dtc_pair(a: int, b: int, code: str) -> None:
    assert parsers.decode_dtc_pair(a, b) == code


def test_zero_pair_is_padding() -> None:
    assert parsers.decode_dtc_pair(0, 0) is None


def test_dtcs_single_and_multi_frame() -> None:
    (one,) = parsers.decode_dtcs(payloads("dtcs_one")["7E8"], DtcStatus.STORED, "7E8")
    assert (one.code, one.status, one.ecu, one.raw) == ("P0171", DtcStatus.STORED, "7E8", "0171")
    four = parsers.decode_dtcs(payloads("dtcs_four_multi_frame")["7E8"], DtcStatus.STORED, "7E8")
    assert [d.code for d in four] == ["P0171", "P0300", "P0302", "U0100"]


def test_no_dtcs_and_zero_padding() -> None:
    assert parsers.decode_dtcs(payloads("dtcs_none")["7E8"], DtcStatus.STORED, "7E8") == []
    padded = bytes.fromhex("4701030000000000")
    assert [d.code for d in parsers.decode_dtcs(padded, DtcStatus.PENDING, "7E9")] == ["P0300"]


def test_dtcs_without_a_count_byte() -> None:
    # ELM327-emulator 4.0.0 framing: <SID> <pairs...>, and <SID> 00 for none.
    codes = parsers.decode_dtcs(bytes.fromhex("4301710300"), DtcStatus.STORED, "7E8")
    assert [d.code for d in codes] == ["P0171", "P0300"]
    assert parsers.decode_dtcs(bytes.fromhex("4700"), DtcStatus.PENDING, "7E8") == []
    assert parsers.decode_dtcs(bytes.fromhex("4A"), DtcStatus.PERMANENT, "7E8") == []


def test_dtc_reply_for_the_wrong_mode_raises() -> None:
    with pytest.raises(ParseError):
        parsers.decode_dtcs(bytes.fromhex("43010171"), DtcStatus.PENDING, "7E8")


# ---- modes 09 / 02 ----------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        bytes.fromhex("490201") + b"WVWZZZAUZJW00000",  # 16 characters
        bytes.fromhex("490201") + b"WVWZZZAUZJW00\x00\x00\x00\x00",  # not printable
        bytes.fromhex("490401") + VIN.encode(),  # not PID 02
        bytes.fromhex("7F0912"),
    ],
)
def test_not_a_vin(payload: bytes) -> None:
    assert parsers.decode_vin(payload) is None


def test_freeze_value() -> None:
    assert parsers.decode_freeze_value(bytes.fromhex("420C000C30")) == ("010C", b"\x0c\x30")
    assert parsers.decode_freeze_value(bytes.fromhex("4202000171")) == ("0102", b"\x01\x71")
    with pytest.raises(ParseError):
        parsers.decode_freeze_value(bytes.fromhex("410C0C30"))
