"""Per-mode decoders (SPEC §4.5).

Every function here takes bytes that `elm327.reassemble` already turned into one complete
ISO-TP payload, and returns typed values. Nothing here talks to a transport.
"""

from collections.abc import Sequence

from obdient.errors import ParseError
from obdient.schemas.snapshot import (
    CONTINUOUS_MONITORS,
    Dtc,
    DtcStatus,
    Monitor,
    MonitorName,
    StatusSinceClear,
)
from obdient.vehicle.pids import PID_TABLE

_DTC_LETTERS = "PCBU"
_DTC_SIDS: dict[DtcStatus, int] = {
    DtcStatus.STORED: 0x43,
    DtcStatus.PENDING: 0x47,
    DtcStatus.PERMANENT: 0x4A,
}
# PID 0101 bytes C (supported) and D (incomplete), bit 0 first.
_NON_CONTINUOUS: tuple[MonitorName, ...] = (
    MonitorName.CATALYST,
    MonitorName.HEATED_CATALYST,
    MonitorName.EVAP,
    MonitorName.SECONDARY_AIR,
    MonitorName.AC_REFRIGERANT,
    MonitorName.O2_SENSOR,
    MonitorName.O2_HEATER,
    MonitorName.EGR,
)


def _hex(data: bytes) -> str:
    return data.hex().upper()


def decode_pid(pid: str, data: bytes) -> tuple[float | str | None, str | None]:
    """Decode one PID's data bytes to (value, unit). (None, None) when there is no decoder."""
    spec = PID_TABLE.get(pid)
    if spec is None or spec.decode is None:
        return None, None
    if len(data) < spec.length:
        raise ParseError(f"PID {pid} needs {spec.length} data byte(s), got {len(data)}", _hex(data))
    value = spec.decode(data[: spec.length])
    if isinstance(value, float | int):
        value = round(float(value), spec.decimals)
    return value, spec.unit


def split_mode01(payload: bytes, requested: Sequence[str]) -> dict[str, bytes]:
    """Split a mode 01 reply (the bytes after 0x41) into {pid: data bytes}.

    An ECU may answer the requested PIDs in any order and leave some out. A PID that was not
    requested, or whose length is unknown, makes the rest of the reply unreadable: ParseError.
    """
    wanted = set(requested)
    out: dict[str, bytes] = {}
    i = 0
    while i < len(payload):
        pid = f"01{payload[i]:02X}"
        if pid not in wanted:
            raise ParseError(f"Reply contains PID {pid}, which was not requested", _hex(payload))
        if len(wanted) == 1:
            out[pid] = payload[i + 1 :]  # a lone PID owns the rest, even with no known length
            break
        spec = PID_TABLE.get(pid)
        if spec is None:
            raise ParseError(f"Cannot split reply: length of PID {pid} is unknown", _hex(payload))
        data = payload[i + 1 : i + 1 + spec.length]
        if len(data) < spec.length:
            raise ParseError(f"Reply is cut short inside PID {pid}", _hex(payload))
        out[pid] = data
        i += 1 + spec.length
    return out


def decode_supported(base_pid: int, data: bytes) -> list[str]:
    """Decode a 4-byte support bitmap: bit 7 of byte 0 is PID base+1, bit 0 of byte 3 is base+32."""
    if len(data) < 4:
        raise ParseError(f"Support bitmap needs 4 bytes, got {len(data)}", _hex(data))
    bits = int.from_bytes(data[:4], "big")
    return [f"01{base_pid + 1 + i:02X}" for i in range(32) if bits & (1 << (31 - i))]


def is_compression_ignition(data: bytes) -> bool:
    """PID 0101, byte B bit 3: the non-continuous monitors use the diesel layout."""
    return len(data) >= 2 and bool(data[1] & 0x08)


def decode_status(data: bytes) -> StatusSinceClear:
    """Decode PID 0101: MIL, DTC count and readiness. Always returns all 11 monitors."""
    if len(data) < 4:
        raise ParseError(f"PID 0101 needs 4 data bytes, got {len(data)}", _hex(data))
    a, b, c, d = data[:4]
    monitors: list[Monitor] = []
    for bit, name in enumerate(CONTINUOUS_MONITORS):
        supported = bool(b & (1 << bit))
        incomplete = bool(b & (1 << (bit + 4)))
        monitors.append(
            Monitor(
                name=name, supported=supported, complete=(not incomplete) if supported else None
            )
        )
    diesel = is_compression_ignition(data)
    for bit, name in enumerate(_NON_CONTINUOUS):
        supported = bool(c & (1 << bit)) and not diesel
        incomplete = bool(d & (1 << bit))
        monitors.append(
            Monitor(
                name=name, supported=supported, complete=(not incomplete) if supported else None
            )
        )
    return StatusSinceClear(mil_on=bool(a & 0x80), dtc_count=a & 0x7F, monitors=monitors)


def decode_dtc_pair(a: int, b: int) -> str | None:
    """Two bytes -> "P0171". None for 0x0000 (padding)."""
    if a == 0 and b == 0:
        return None
    return f"{_DTC_LETTERS[a >> 6]}{(a >> 4) & 0x3}{a & 0xF:X}{b:02X}"


def decode_dtcs(payload: bytes, status: DtcStatus, ecu: str) -> list[Dtc]:
    """Decode a mode 03 / 07 / 0A reply. Zero pairs are ignored.

    On CAN the reply is <SID> <count> <pairs...> (SAE J1979). ELM327-emulator 4.0.0 leaves the
    count byte out (<SID> <pairs...>, and <SID> 00 for none), as pre-CAN protocols do. The two
    are told apart by length: after ISO-TP reassembly there is no padding, so an odd number of
    bytes after the SID means a count byte is present. The count itself is not trusted.
    """
    sid = _DTC_SIDS[status]
    if not payload or payload[0] != sid:
        raise ParseError(f"Expected a {sid:02X} reply for {status.value} codes", _hex(payload))
    body = payload[1:]
    pairs = body[1:] if len(body) % 2 else body
    out: list[Dtc] = []
    for i in range(0, len(pairs) - 1, 2):
        code = decode_dtc_pair(pairs[i], pairs[i + 1])
        if code is not None:
            out.append(Dtc(code=code, status=status, ecu=ecu, raw=_hex(pairs[i : i + 2])))
    return out


def decode_vin(payload: bytes) -> str | None:
    """Decode a mode 09 PID 02 reply: 49 02 01 + 17 ASCII bytes. None if it is not a VIN."""
    if len(payload) < 20 or payload[:2] != b"\x49\x02":
        return None
    text = payload[3:20].decode("ascii", errors="replace")
    if len(text) != 17 or not text.isascii() or not text.isalnum():
        return None
    return text


def decode_freeze_value(payload: bytes) -> tuple[str, bytes]:
    """Split a mode 02 reply, 42 <PID> <frame> <data...>, into (mode-01 pid, data bytes)."""
    if len(payload) < 3 or payload[0] != 0x42:
        raise ParseError("Expected a 42 (freeze frame) reply", _hex(payload))
    return f"01{payload[1]:02X}", payload[3:]
