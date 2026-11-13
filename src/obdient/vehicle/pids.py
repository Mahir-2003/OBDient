"""Mode 01 PID table: names, lengths, formulas, units (SPEC §4.5).

Formulas follow SAE J1979. `A` is the first data byte, `B` the second. Identifiers are
mode + PID as four uppercase hex characters ("010C").
"""

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class PidSpec:
    pid: str  # "010C"
    name: str  # friendly name, unique; the pid itself for length-only entries
    length: int  # data bytes
    unit: str | None
    decimals: int  # display / evidence rounding
    decode: Callable[[bytes], float | str] | None  # None: length known, value carried raw


def _word(d: bytes) -> int:
    return 256 * d[0] + d[1]


def _percent(d: bytes) -> float:
    return d[0] * 100 / 255


def _minus_40(d: bytes) -> float:
    return d[0] - 40


def _trim(d: bytes) -> float:
    return (d[0] - 128) * 100 / 128


def _byte(d: bytes) -> float:
    return d[0]


def _o2_volts(d: bytes) -> float:
    return d[0] / 200  # byte B is the sensor's short-term trim; not used


def _lambda(d: bytes) -> float:
    return _word(d) * 2 / 65536


_DECODED: tuple[PidSpec, ...] = (
    PidSpec("0104", "load_pct", 1, "%", 1, _percent),
    PidSpec("0105", "ect_c", 1, "°C", 0, _minus_40),
    PidSpec("0106", "stft_b1", 1, "%", 1, _trim),
    PidSpec("0107", "ltft_b1", 1, "%", 1, _trim),
    PidSpec("0108", "stft_b2", 1, "%", 1, _trim),
    PidSpec("0109", "ltft_b2", 1, "%", 1, _trim),
    PidSpec("010A", "fuel_pressure_kpa", 1, "kPa", 0, lambda d: 3 * d[0]),
    PidSpec("010B", "map_kpa", 1, "kPa", 0, _byte),
    PidSpec("010C", "rpm", 2, "rpm", 0, lambda d: _word(d) / 4),
    PidSpec("010D", "speed_kmh", 1, "km/h", 0, _byte),
    PidSpec("010E", "timing_deg", 1, "°", 1, lambda d: d[0] / 2 - 64),
    PidSpec("010F", "iat_c", 1, "°C", 0, _minus_40),
    PidSpec("0110", "maf_gs", 2, "g/s", 2, lambda d: _word(d) / 100),
    PidSpec("0111", "throttle_pct", 1, "%", 1, _percent),
    PidSpec("0114", "o2_b1s1_v", 2, "V", 3, _o2_volts),
    PidSpec("0115", "o2_b1s2_v", 2, "V", 3, _o2_volts),
    PidSpec("011F", "runtime_s", 2, "s", 0, _word),
    PidSpec("0121", "distance_mil_on_km", 2, "km", 0, _word),
    PidSpec("012F", "fuel_level_pct", 1, "%", 1, _percent),
    PidSpec("0130", "warmups_since_clear", 1, "count", 0, _byte),
    PidSpec("0131", "distance_since_clear_km", 2, "km", 0, _word),
    PidSpec("0133", "baro_kpa", 1, "kPa", 0, _byte),
    PidSpec("0134", "lambda_b1s1", 4, "λ", 3, _lambda),
    PidSpec("0142", "module_v", 2, "V", 2, lambda d: _word(d) / 1000),
    PidSpec("0143", "abs_load_pct", 2, "%", 1, lambda d: _word(d) * 100 / 255),
    PidSpec("0144", "lambda_cmd", 2, "λ", 3, _lambda),
    PidSpec("0146", "ambient_c", 1, "°C", 0, _minus_40),
    PidSpec("014D", "minutes_mil_on", 2, "min", 0, _word),
    PidSpec("014E", "minutes_since_clear", 2, "min", 0, _word),
    PidSpec("015C", "oil_temp_c", 1, "°C", 0, _minus_40),
)

# PIDs with no decoder whose data length is needed to split multi-PID replies.
# [VERIFY M2 against J1979] Lengths are from the published J1979 PID table as commonly
# reproduced; they have not been checked against the standard document itself.
_LENGTH_ONLY: dict[str, int] = {
    "0101": 4,  # monitor status (decoded by parsers.decode_status)
    "0103": 2,  # fuel system status
    "0112": 1,  # commanded secondary air status
    "0113": 1,  # O2 sensors present (2 banks)
    **{f"01{p:02X}": 2 for p in range(0x16, 0x1C)},  # O2 sensors 3-8: voltage, trim
    "011C": 1,  # OBD standard this vehicle conforms to
    "011D": 1,  # O2 sensors present (4 banks)
    "011E": 1,  # auxiliary input status
    "0120": 4,  # supported PIDs 21-40
    "0122": 2,  # fuel rail pressure, relative to manifold vacuum
    "0123": 2,  # fuel rail gauge pressure
    **{f"01{p:02X}": 4 for p in range(0x24, 0x2C)},  # wide-range O2 sensors: lambda, voltage
    "012C": 1,  # commanded EGR
    "012D": 1,  # EGR error
    "012E": 1,  # commanded evaporative purge
    "0132": 2,  # evap system vapor pressure
    **{f"01{p:02X}": 2 for p in range(0x3C, 0x40)},  # catalyst temperatures
    "0140": 4,  # supported PIDs 41-60
    "0141": 4,  # monitor status this drive cycle
    "0145": 1,  # relative throttle position
    **{f"01{p:02X}": 1 for p in range(0x47, 0x4D)},  # throttle / pedal positions, commanded
    "014F": 4,  # maximum values
    "0150": 4,  # maximum MAF
    "0151": 1,  # fuel type
    "0152": 1,  # ethanol fuel %
    "0153": 2,  # absolute evap vapor pressure
    "0154": 2,  # evap vapor pressure
    **{f"01{p:02X}": 2 for p in range(0x55, 0x59)},  # secondary O2 trims
    "0159": 2,  # fuel rail absolute pressure
    "015A": 1,  # relative accelerator pedal position
    "015B": 1,  # hybrid battery remaining life
    "015D": 2,  # fuel injection timing
    "015E": 2,  # engine fuel rate
    "015F": 1,  # emission requirements
}

PID_TABLE: dict[str, PidSpec] = {
    **{pid: PidSpec(pid, pid, length, None, 0, None) for pid, length in _LENGTH_ONLY.items()},
    **{spec.pid: spec for spec in _DECODED},
}

NAME_TO_PID: dict[str, str] = {spec.name: spec.pid for spec in _DECODED}

# What the collector reads by default, filtered to the PIDs the vehicle supports.
DEFAULT_LIVE_PIDS: tuple[str, ...] = (
    "0104",
    "0105",
    "0106",
    "0107",
    "0108",
    "0109",
    "010B",
    "010C",
    "010D",
    "010E",
    "010F",
    "0110",
    "0111",
    "0114",
    "0115",
    "011F",
    "0133",
    "0134",
    "0142",
    "0144",
)

SINCE_CLEAR_PIDS: tuple[str, ...] = ("0121", "0130", "0131", "014D", "014E")
