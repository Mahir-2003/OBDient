"""SPEC §4.5: every decoder row against a hand-computed SAE J1979 example.

The OBDex cross-check named in the SPEC waits for data/obdex (M4).
"""

import pytest

from obdient.errors import ParseError
from obdient.schemas.common import CANONICAL_UNITS
from obdient.vehicle.parsers import decode_pid
from obdient.vehicle.pids import DEFAULT_LIVE_PIDS, NAME_TO_PID, PID_TABLE, SINCE_CLEAR_PIDS

# pid, name, data bytes, expected value, unit. Arithmetic is in the comments.
ROWS: list[tuple[str, str, str, float, str]] = [
    ("0104", "load_pct", "80", 50.2, "%"),  # 128*100/255 = 50.196
    ("0105", "ect_c", "7B", 83, "°C"),  # 123 - 40
    ("0106", "stft_b1", "88", 6.2, "%"),  # (136-128)*100/128 = 6.25 -> 6.2 (round half even)
    ("0107", "ltft_b1", "99", 19.5, "%"),  # (153-128)*100/128 = 19.53
    ("0108", "stft_b2", "70", -12.5, "%"),  # (112-128)*100/128
    ("0109", "ltft_b2", "00", -100.0, "%"),  # (0-128)*100/128
    ("010A", "fuel_pressure_kpa", "64", 300, "kPa"),  # 3*100
    ("010B", "map_kpa", "65", 101, "kPa"),
    ("010C", "rpm", "0C30", 780, "rpm"),  # (12*256+48)/4 = 3120/4
    ("010D", "speed_kmh", "3C", 60, "km/h"),
    ("010E", "timing_deg", "8A", 5.0, "°"),  # 138/2 - 64
    ("010F", "iat_c", "41", 25, "°C"),  # 65 - 40
    ("0110", "maf_gs", "00F0", 2.4, "g/s"),  # 240/100
    ("0111", "throttle_pct", "26", 14.9, "%"),  # 38*100/255 = 14.90
    ("0114", "o2_b1s1_v", "5AFF", 0.45, "V"),  # 90/200; byte B ignored
    ("0115", "o2_b1s2_v", "8C80", 0.7, "V"),  # 140/200
    ("011F", "runtime_s", "0258", 600, "s"),  # 2*256+88
    ("0121", "distance_mil_on_km", "0064", 100, "km"),
    ("012F", "fuel_level_pct", "FF", 100.0, "%"),
    ("0130", "warmups_since_clear", "2D", 45, "count"),
    ("0131", "distance_since_clear_km", "0334", 820, "km"),  # 3*256+52
    ("0133", "baro_kpa", "65", 101, "kPa"),
    ("0134", "lambda_b1s1", "80000000", 1.0, "λ"),  # 32768*2/65536
    ("0142", "module_v", "3714", 14.1, "V"),  # 14100/1000
    ("0143", "abs_load_pct", "0040", 25.1, "%"),  # 64*100/255 = 25.098
    ("0144", "lambda_cmd", "7AE1", 0.96, "λ"),  # 31457*2/65536 = 0.95999
    ("0146", "ambient_c", "3C", 20, "°C"),
    ("014D", "minutes_mil_on", "003C", 60, "min"),
    ("014E", "minutes_since_clear", "0100", 256, "min"),
    ("015C", "oil_temp_c", "82", 90, "°C"),  # 130 - 40
]


@pytest.mark.parametrize("pid,name,data,expected,unit", ROWS)
def test_decoder_row(pid: str, name: str, data: str, expected: float, unit: str) -> None:
    spec = PID_TABLE[pid]
    raw = bytes.fromhex(data)
    assert spec.name == name
    assert spec.length == len(raw)
    assert spec.unit == unit
    assert NAME_TO_PID[name] == pid
    assert decode_pid(pid, raw) == (pytest.approx(expected), unit)


def test_every_decoder_has_a_row_here() -> None:
    decoded = {pid for pid, spec in PID_TABLE.items() if spec.decode is not None}
    assert decoded == {row[0] for row in ROWS}
    assert len(decoded) == 30


def test_values_are_rounded_to_the_pid_decimals() -> None:
    assert decode_pid("0104", b"\x80")[0] == 50.2  # not 50.19607...
    assert decode_pid("0144", bytes.fromhex("7AE1"))[0] == 0.96


def test_units_are_canonical_and_names_unique() -> None:
    specs = [spec for spec in PID_TABLE.values() if spec.decode is not None]
    assert all(spec.unit in CANONICAL_UNITS for spec in specs)
    assert len({spec.name for spec in specs}) == len(specs)


def test_length_only_pids_carry_raw_values() -> None:
    assert PID_TABLE["0101"].length == 4
    assert PID_TABLE["0103"].length == 2
    assert PID_TABLE["0124"].length == 4
    assert decode_pid("0103", b"\x02\x00") == (None, None)


def test_unknown_pid_has_no_value() -> None:
    assert decode_pid("01A6", b"\x00\x00\x00\x00") == (None, None)


def test_short_data_is_a_parse_error() -> None:
    with pytest.raises(ParseError):
        decode_pid("010C", b"\x0c")


def test_default_and_since_clear_pids_all_have_decoders() -> None:
    for pid in (*DEFAULT_LIVE_PIDS, *SINCE_CLEAR_PIDS):
        assert PID_TABLE[pid].decode is not None
    assert len(DEFAULT_LIVE_PIDS) == 20
