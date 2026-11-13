"""ELM327 protocol layer (SPEC §4.4): setup, request/response, ISO-TP reassembly.

Written by hand; no code from python-OBD or ELM327-emulator.

Layers, bottom up:
  clean_lines   raw bytes from the adapter -> text lines
  parse_reply   lines -> an error token, or CAN frames grouped by nothing yet
  reassemble    CAN frames -> one complete ISO-TP payload per ECU
  ELM327Vehicle payloads -> typed values, through vehicle/parsers.py
"""

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from obdient.errors import (
    ParseError,
    TransportError,
    TransportTimeout,
    UnsupportedProtocolError,
    VehicleConnectionError,
)
from obdient.schemas.common import Condition, SnapshotSource
from obdient.schemas.snapshot import (
    AdapterInfo,
    Dtc,
    DtcStatus,
    FreezeFrame,
    ReadError,
    ReadErrorKind,
    Reading,
    SinceClear,
    StatusSinceClear,
)
from obdient.vehicle import allowlist, parsers
from obdient.vehicle.pids import PID_TABLE, SINCE_CLEAR_PIDS
from obdient.vehicle.transports import Transport

logger = logging.getLogger("obdient.elm327")

MAX_PIDS_PER_REQUEST = 6
SUPPORTED_CAN_PROTOCOLS = frozenset({"6", "8"})  # ISO 15765-4 CAN, 11-bit ID, 500 / 250 kbaud

_EXACT_ERRORS: dict[str, ReadErrorKind] = {
    "NO DATA": ReadErrorKind.NO_DATA,
    "UNABLE TO CONNECT": ReadErrorKind.UNABLE_TO_CONNECT,
    "CAN ERROR": ReadErrorKind.CAN_ERROR,
    "BUFFER FULL": ReadErrorKind.BUFFER_FULL,
    "?": ReadErrorKind.UNKNOWN_COMMAND,
    "STOPPED": ReadErrorKind.STOPPED,
}
_ADAPTER_ERROR = re.compile(
    r"BUS INIT:.*ERROR|BUS BUSY|BUS ERROR|FB ERROR|<?DATA ERROR|<RX ERROR"
    r"|ERR\d\d|LV RESET|ACT ALERT"
)
_HEX = re.compile(r"[0-9A-F]+")
_LENGTH_LINE = re.compile(r"[0-9A-F]{3}")
_NUMBERED_LINE = re.compile(r"([0-9A-F]):(.*)")
_VOLTAGE = re.compile(r"(\d+\.\d+)V?")


@dataclass(frozen=True)
class CanFrame:
    ecu: str  # "7E8"; "" when headers are off
    data: bytes  # 8 data bytes or fewer, starting with the ISO-TP PCI byte


@dataclass(frozen=True)
class ElmReply:
    lines: list[str]  # cleaned, non-empty, without "SEARCHING..."
    error: ReadErrorKind | None
    frames: list[CanFrame]
    detail: str | None = None  # the adapter's line for ADAPTER_ERROR, the raw reply for PARSE_ERROR


def clean_lines(raw: bytes) -> list[str]:
    """Drop NULs, split on any run of CR / LF, strip, drop empty and SEARCHING lines."""
    text = raw.replace(b"\x00", b"").decode("ascii", errors="replace")
    lines = (line.strip() for line in re.split(r"[\r\n]+", text))
    return [line for line in lines if line and not line.startswith("SEARCHING")]


def _error_of(line: str) -> ReadErrorKind | None:
    if line in _EXACT_ERRORS:
        return _EXACT_ERRORS[line]
    if _ADAPTER_ERROR.fullmatch(line):
        return ReadErrorKind.ADAPTER_ERROR
    return None


def _frames_with_headers(lines: Sequence[str]) -> list[CanFrame] | None:
    frames: list[CanFrame] = []
    for line in lines:
        compact = line.replace(" ", "")
        # 3 hex characters of CAN ID, then at least one whole data byte.
        if len(compact) < 5 or len(compact) % 2 == 0 or not _HEX.fullmatch(compact):
            return None
        frames.append(CanFrame(ecu=compact[:3], data=bytes.fromhex(compact[3:])))
    return frames


def _frames_without_headers(lines: Sequence[str]) -> list[CanFrame] | None:
    """With headers off the adapter strips the PCI bytes; rebuild them so reassemble() works.

    Single frame:  "41 0C 0C 30"
    Multi-frame:   "014" (total length), then "0: 49 02 01 ...", "1: ...", ...
    """
    frames: list[CanFrame] = []
    total: int | None = None
    for line in lines:
        compact = line.replace(" ", "")
        numbered = _NUMBERED_LINE.fullmatch(compact)
        if numbered:
            index, body = int(numbered.group(1), 16), numbered.group(2)
            if total is None or len(body) % 2 or not _HEX.fullmatch(body):
                return None
            data = bytes.fromhex(body)
            if index == 0 and not frames:
                pci = bytes([0x10 | (total >> 8), total & 0xFF])
            else:
                pci = bytes([0x20 | index])
            frames.append(CanFrame(ecu="", data=pci + data))
        elif _LENGTH_LINE.fullmatch(compact):
            total = int(compact, 16)
        elif len(compact) % 2 == 0 and _HEX.fullmatch(compact) and len(compact) <= 14:
            data = bytes.fromhex(compact)
            frames.append(CanFrame(ecu="", data=bytes([len(data)]) + data))
        else:
            return None
    return frames


def parse_reply(raw: bytes, headers: bool) -> ElmReply:
    """Turn the bytes before the prompt into an error token or a list of CAN frames."""
    lines = clean_lines(raw)
    for line in lines:
        kind = _error_of(line)
        if kind is not None:
            detail = line if kind is ReadErrorKind.ADAPTER_ERROR else None
            return ElmReply(lines=lines, error=kind, frames=[], detail=detail)
    frames = _frames_with_headers(lines) if headers else _frames_without_headers(lines)
    if frames is None:
        detail = raw.decode("ascii", errors="replace")
        return ElmReply(lines=lines, error=ReadErrorKind.PARSE_ERROR, frames=[], detail=detail)
    return ElmReply(lines=lines, error=None, frames=frames)


def reassemble(frames: Sequence[CanFrame]) -> dict[str, bytes]:
    """ISO-TP: join each ECU's frames into its full payload. Raises ParseError.

    PCI high nibble 0 = single frame (low nibble = length); 1 = first frame (12-bit length);
    2 = consecutive frame (low nibble = sequence 1, 2, ... wrapping F -> 0). Padding beyond the
    declared length is dropped. Flow control is the adapter's job (ATCAF1).
    """
    done: dict[str, bytes] = {}
    partial: dict[str, tuple[int, bytearray, int]] = {}  # ecu -> (total, buffer, next sequence)
    for frame in frames:
        ecu, data = frame.ecu, frame.data
        raw = data.hex().upper()
        if not data:
            raise ParseError(f"Empty frame from ECU {ecu}", raw)
        kind, low = data[0] >> 4, data[0] & 0x0F
        if kind == 0:
            if low == 0 or low > len(data) - 1:
                raise ParseError(f"Single frame from {ecu} declares {low} bytes", raw)
            _keep(done, ecu, data[1 : 1 + low])
        elif kind == 1:
            if len(data) < 2:
                raise ParseError(f"First frame from {ecu} is too short", raw)
            total = (low << 8) | data[1]
            partial[ecu] = (total, bytearray(data[2:]), 1)
        elif kind == 2:
            if ecu not in partial:
                raise ParseError(f"Consecutive frame from {ecu} without a first frame", raw)
            total, buffer, expected = partial[ecu]
            if low != expected:
                raise ParseError(f"Frame {low:X} from {ecu} arrived when {expected:X} was due", raw)
            buffer += data[1:]
            partial[ecu] = (total, buffer, (expected + 1) & 0x0F)
        else:
            raise ParseError(f"Unexpected ISO-TP frame type {kind} from {ecu}", raw)
    for ecu, (total, buffer, _) in partial.items():
        if len(buffer) < total:
            raise ParseError(
                f"ECU {ecu} sent {len(buffer)} of {total} bytes", bytes(buffer).hex().upper()
            )
        _keep(done, ecu, bytes(buffer[:total]))
    return done


def _keep(done: dict[str, bytes], ecu: str, payload: bytes) -> None:
    """First complete message per ECU wins, except that a negative response (7F ..., such as
    0x78 "response pending") gives way to the message that follows it."""
    if ecu not in done or done[ecu][:1] == b"\x7f":
        done[ecu] = payload


def _now() -> datetime:
    return datetime.now(UTC)


class ELM327Vehicle:
    """`Vehicle` over an ELM327-compatible adapter. Headers are always on."""

    def __init__(
        self,
        transport: Transport,
        source: SnapshotSource,
        default_timeout_s: float = 2.0,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.source = source
        self._transport = transport
        self.timeout_s = default_timeout_s
        self._clock = clock
        self._errors: list[ReadError] = []
        self._supported: list[str] | None = None
        self._first_bitmap: bytes | None = None
        self.ecus: set[str] = set()  # every ECU that has answered so far

    # ---- connection ---------------------------------------------------------

    def connect(self) -> AdapterInfo:
        try:
            self._transport.open()
        except TransportError as exc:
            raise VehicleConnectionError(str(exc)) from exc

        reset = self._at("ATZ", 5.0)
        if not any("ELM327" in line or "STN" in line for line in reset):
            raise VehicleConnectionError(
                f"Adapter did not identify itself after ATZ: {' / '.join(reset)}"
            )
        for command in ("ATE0", "ATL0", "ATS0", "ATH1", "ATCAF1", "ATAT1", "ATSP0"):
            reply = self._at(command, self.timeout_s)
            echoed = command == "ATE0" and reply == ["ATE0", "OK"]  # echo was still on
            if reply != ["OK"] and not echoed:
                raise VehicleConnectionError(f"Setup command {command} failed: {' / '.join(reply)}")

        id_string = self._at_value("ATI")
        description = self._at_value("AT@1")
        stn_id = self._at_value("STI")
        stn_device = self._at_value("STDI")
        voltage_match = _VOLTAGE.fullmatch(self._at_value("ATRV") or "")
        voltage = float(voltage_match.group(1)) if voltage_match else None

        payloads, error = self._request("0100", 10.0, retry=False)  # protocol search
        if error is not None:
            raise VehicleConnectionError(
                f"Vehicle not responding ({error.kind.value}); ignition may be off."
            )
        self._first_bitmap = self._engine(payloads)[1][2:]

        protocol = self._at_value("ATDPN")
        number = (protocol or "").removeprefix("A")
        if number not in SUPPORTED_CAN_PROTOCOLS:
            raise UnsupportedProtocolError(
                f"Protocol {number or protocol} is not supported in v0.1 (CAN 11-bit only)."
            )
        return AdapterInfo(
            id_string=id_string,
            device_description=description,
            stn_id=stn_id,
            stn_device=stn_device,
            protocol_number=protocol,
            voltage=voltage,
        )

    def close(self) -> None:
        self._transport.close()

    # ---- Vehicle methods ----------------------------------------------------

    def supported_pids(self) -> list[str]:
        if self._supported is not None:
            return list(self._supported)
        found: list[str] = []
        for base in (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0):
            if base == 0 and self._first_bitmap is not None:
                data = self._first_bitmap
            else:
                payloads, error = self._request(f"01{base:02X}")
                if error is not None:
                    self._errors.append(error)
                    break
                data = self._engine(payloads)[1][2:]
            try:
                pids = parsers.decode_supported(base, data)
            except ParseError as exc:
                self._record(f"01{base:02X}", ReadErrorKind.PARSE_ERROR, exc.raw)
                break
            found += pids
            if f"01{base + 0x20:02X}" not in pids:  # the last bit says whether a next range exists
                break
        self._supported = sorted(found)
        return list(self._supported)

    def vin(self) -> str | None:
        payloads, error = self._request("0902")
        if error is not None:
            self._errors.append(error)
            return None
        payload = self._engine(payloads)[1]
        vin = parsers.decode_vin(payload)
        if vin is None:
            self._record("0902", ReadErrorKind.PARSE_ERROR, payload.hex().upper())
        return vin

    def status(self) -> StatusSinceClear | None:
        payloads, error = self._request("0101")
        if error is not None:
            self._errors.append(error)
            return None
        data = self._engine(payloads)[1][2:]
        try:
            status = parsers.decode_status(data)
        except ParseError as exc:
            self._record("0101", ReadErrorKind.PARSE_ERROR, exc.raw)
            return None
        if parsers.is_compression_ignition(data):
            self._record("0101", ReadErrorKind.UNSUPPORTED, "compression-ignition monitors")
        return status

    def read_dtcs(self) -> list[Dtc]:
        out: list[Dtc] = []
        for command, status in (
            ("03", DtcStatus.STORED),
            ("07", DtcStatus.PENDING),
            ("0A", DtcStatus.PERMANENT),
        ):
            payloads, error = self._request(command)
            if error is not None:
                self._record_unless_no_data(error)  # the adapter's NO DATA means "none"
                continue
            for ecu, payload in payloads.items():  # codes are kept from every ECU
                try:
                    out += parsers.decode_dtcs(payload, status, ecu)
                except ParseError as exc:
                    self._record(command, ReadErrorKind.PARSE_ERROR, exc.raw)
        return out

    def freeze_frame(self) -> FreezeFrame | None:
        payloads, error = self._request("020200")  # which code triggered the freeze frame
        if error is not None:
            self._record_unless_no_data(error)
            return None
        ecu, payload = self._engine(payloads)
        trigger = parsers.decode_dtc_pair(payload[3], payload[4]) if len(payload) >= 5 else None
        if trigger is None:
            return None

        readings: list[Reading] = []
        for pid in self._freeze_frame_pids():
            command = f"02{pid[2:]}00"
            payloads, error = self._request(command)
            if error is not None:
                self._record_unless_no_data(error)
                continue
            reply_ecu, payload = self._engine(payloads)
            try:
                reply_pid, data = parsers.decode_freeze_value(payload)
                if reply_pid != pid:
                    raise ParseError(f"Asked for {pid}, got {reply_pid}", payload.hex().upper())
                readings.append(self._reading(pid, reply_ecu, data, Condition.UNKNOWN))
            except ParseError as exc:
                self._record(command, ReadErrorKind.PARSE_ERROR, exc.raw)
        return FreezeFrame(trigger_code=trigger, ecu=ecu, readings=readings)

    def since_clear(self) -> SinceClear:
        supported = set(self.supported_pids())
        raw = self._read_raw([pid for pid in SINCE_CLEAR_PIDS if pid in supported])

        def value(pid: str) -> float | None:
            if pid not in raw:
                return None
            try:
                decoded = parsers.decode_pid(pid, raw[pid][1])[0]
            except ParseError as exc:
                self._record(pid, ReadErrorKind.PARSE_ERROR, exc.raw)
                return None
            return decoded if isinstance(decoded, float) else None

        warmups = value("0130")
        return SinceClear(
            warmups=int(warmups) if warmups is not None else None,
            distance_km=value("0131"),
            minutes_mil_on=value("014D"),
            minutes_since_clear=value("014E"),
            distance_mil_on_km=value("0121"),
        )

    def read_pids(
        self, pids: Sequence[str], condition: Condition = Condition.UNKNOWN
    ) -> list[Reading]:
        supported = set(self.supported_pids())
        wanted: list[str] = []
        for pid in dict.fromkeys(pids):  # de-duplicate, keep order
            if pid in supported:
                wanted.append(pid)
            else:
                self._record(pid, ReadErrorKind.UNSUPPORTED, None)  # never sent
        raw = self._read_raw(wanted)
        readings: list[Reading] = []
        for pid in wanted:
            if pid not in raw:
                continue
            ecu, data = raw[pid]
            try:
                readings.append(self._reading(pid, ecu, data, condition))
            except ParseError as exc:
                self._record(pid, ReadErrorKind.PARSE_ERROR, exc.raw)
        return readings

    def drain_errors(self) -> list[ReadError]:
        errors, self._errors = self._errors, []
        return errors

    # ---- request planning ---------------------------------------------------

    def _read_raw(self, pids: Sequence[str]) -> dict[str, tuple[str, bytes]]:
        """Read mode 01 PIDs. Returns {pid: (ecu, data bytes)}; records one error per failed PID."""
        out: dict[str, tuple[str, bytes]] = {}
        groupable = [pid for pid in pids if pid in PID_TABLE]
        alone = [pid for pid in pids if pid not in PID_TABLE]  # unknown length: cannot be split
        for start in range(0, len(groupable), MAX_PIDS_PER_REQUEST):
            chunk = groupable[start : start + MAX_PIDS_PER_REQUEST]
            if len(chunk) > 1:
                out.update(self._read_group(chunk))
            alone += [pid for pid in chunk if pid not in out]  # fall back to one request per PID
        for pid in alone:
            payloads, error = self._request(pid)
            if error is not None:
                self._errors.append(error)
                continue
            ecu, payload = self._engine(payloads)
            if len(payload) < 2 or f"01{payload[1]:02X}" != pid:
                self._record(pid, ReadErrorKind.PARSE_ERROR, payload.hex().upper())
                continue
            out[pid] = (ecu, payload[2:])
        return out

    def _read_group(self, chunk: Sequence[str]) -> dict[str, tuple[str, bytes]]:
        """One multi-PID request. Any failure returns what was readable; nothing is recorded."""
        payloads, error = self._request("01" + "".join(pid[2:] for pid in chunk))
        if error is not None:
            return {}
        ecu, payload = self._engine(payloads)
        try:
            return {
                pid: (ecu, data) for pid, data in parsers.split_mode01(payload[1:], chunk).items()
            }
        except ParseError:
            return {}

    def _freeze_frame_pids(self) -> list[str]:
        """PIDs stored in freeze frame 00 that we can decode."""
        found: list[str] = []
        for base in (0x00, 0x20, 0x40):
            payloads, error = self._request(f"02{base:02X}00")
            if error is not None:
                self._record_unless_no_data(error)
                break
            payload = self._engine(payloads)[1]
            try:
                pids = parsers.decode_supported(base, payload[3:])
            except ParseError as exc:
                self._record(f"02{base:02X}00", ReadErrorKind.PARSE_ERROR, exc.raw)
                break
            found += pids
            if f"01{base + 0x20:02X}" not in pids:
                break
        return [
            pid
            for pid in found
            if pid != "0102" and pid in PID_TABLE and PID_TABLE[pid].decode is not None
        ]

    def _reading(self, pid: str, ecu: str, data: bytes, condition: Condition) -> Reading:
        value, unit = parsers.decode_pid(pid, data)
        spec = PID_TABLE.get(pid)
        return Reading(
            pid=pid,
            name=spec.name if spec else pid,
            value=value,
            unit=unit,
            ts=self._clock(),
            ecu=ecu,
            condition=condition,
            raw=data.hex().upper(),
        )

    # ---- wire level ---------------------------------------------------------

    def _at(self, command: str, timeout_s: float) -> list[str]:
        """Send a setup command during connect(); a transport failure aborts the connection."""
        allowed = allowlist.parse(command)
        try:
            raw = self._transport.send(allowed, timeout_s)
        except TransportError as exc:
            raise VehicleConnectionError(f"Adapter not responding to {command}: {exc}") from exc
        lines = clean_lines(raw)
        logger.debug("%s -> %s", command, lines)
        return lines

    def _at_value(self, command: str) -> str | None:
        """The reply to an identification command, or None for '?' or silence."""
        lines = self._at(command, self.timeout_s)
        if not lines or lines[-1] == "?":
            return None
        return lines[-1]

    def _request(
        self, command: str, timeout_s: float | None = None, retry: bool = True
    ) -> tuple[dict[str, bytes], ReadError | None]:
        """Send one OBD request. Returns ({ecu: payload}, None) or ({}, the error).

        Payloads are complete ISO-TP messages that are positive replies to this request's mode.
        The caller decides whether an error is recorded: NO DATA sometimes just means "none".
        """
        allowed = allowlist.parse(command)
        timeout_s = timeout_s if timeout_s is not None else self.timeout_s
        raw: bytes | None = None
        for attempt in range(1, 3 if retry else 2):  # one retry on timeout
            try:
                raw = self._transport.send(allowed, timeout_s)
                break
            except TransportTimeout:
                logger.debug("%s timed out (attempt %d)", allowed.text, attempt)
            except TransportError as exc:
                return {}, self._error(allowed.text, ReadErrorKind.ADAPTER_ERROR, str(exc))
        if raw is None:
            return {}, self._error(allowed.text, ReadErrorKind.TIMEOUT, None)

        reply = parse_reply(raw, headers=True)
        logger.debug("%s -> %s", allowed.text, reply.lines)
        if reply.error is not None:
            return {}, self._error(allowed.text, reply.error, reply.detail)
        try:
            payloads = reassemble(reply.frames)
        except ParseError as exc:
            return {}, self._error(allowed.text, ReadErrorKind.PARSE_ERROR, f"{exc} [{exc.raw}]")

        assert allowed.mode is not None
        positive = {
            ecu: payload
            for ecu, payload in sorted(payloads.items())
            if payload and payload[0] == allowed.mode + 0x40
        }
        if positive:
            self.ecus.update(positive)
            return positive, None
        for payload in payloads.values():
            if len(payload) >= 3 and payload[0] == 0x7F:
                detail = f"negative response 0x{payload[2]:02X}"
                return {}, self._error(allowed.text, ReadErrorKind.NO_DATA, detail)
        if not payloads:
            return {}, self._error(allowed.text, ReadErrorKind.NO_DATA, "empty reply")
        detail = raw.decode("ascii", errors="replace")
        return {}, self._error(allowed.text, ReadErrorKind.PARSE_ERROR, detail)

    @staticmethod
    def _engine(payloads: dict[str, bytes]) -> tuple[str, bytes]:
        """For mode 01 / 02 / 09 values the engine ECU wins: the lowest responding ID."""
        ecu = min(payloads)
        return ecu, payloads[ecu]

    def _error(self, request: str, kind: ReadErrorKind, detail: str | None) -> ReadError:
        return ReadError(request=request, kind=kind, detail=detail, ts=self._clock())

    def _record(self, request: str, kind: ReadErrorKind, detail: str | None) -> None:
        self._errors.append(self._error(request, kind, detail))

    def _record_unless_no_data(self, error: ReadError) -> None:
        """Skip the adapter's plain NO DATA token. An ECU's negative response (which carries a
        detail) is still recorded: "refused" is not the same as "nothing there"."""
        if error.kind is not ReadErrorKind.NO_DATA or error.detail is not None:
            self._errors.append(error)
