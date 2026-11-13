"""Read-only allowlist (SPEC §4.2).

Every byte sent to an adapter starts life as a string passed to `parse()`. Transports accept
only the `AllowedCommand` it returns, so a request that is not a read cannot be represented
above the transport.
"""

import logging
import re
from dataclasses import InitVar, dataclass
from typing import Literal

from obdient.errors import ForbiddenCommandError, ForbiddenReason

__all__ = [
    "AT_COMMANDS",
    "AllowedCommand",
    "ForbiddenCommandError",
    "ForbiddenReason",
    "is_allowed",
    "parse",
]

logger = logging.getLogger("obdient.allowlist")

MAX_LENGTH = 16  # after normalization

# Setup commands only. Exact match after normalization; nothing else starting with AT or ST.
# [VERIFY M2/M8] May gain entries once the OBDLink CX documentation is read; each addition
# needs a line in SPEC §4.2 and a test.
AT_COMMANDS: frozenset[str] = frozenset(
    {
        "ATZ",  # reset
        "ATE0",  # echo off
        "ATL0",  # linefeeds off
        "ATS0",  # spaces off
        "ATS1",  # spaces on
        "ATH0",  # headers off
        "ATH1",  # headers on
        "ATSP0",  # protocol: automatic
        "ATDPN",  # describe protocol by number
        "ATRV",  # read battery voltage
        "ATI",  # adapter ID string
        "AT@1",  # device description
        "ATCAF1",  # CAN automatic formatting on
        "ATAT1",  # adaptive timing, mode 1
        "ATAT2",  # adaptive timing, mode 2
        "ATSH7DF",  # header: functional address (all ECUs)
        "ATSH7E0",  # header: engine ECU
        "STI",  # OBDLink firmware ID
        "STDI",  # OBDLink device ID
    }
)

# Mode byte -> the full shape a request of that mode must have.
_OBD_SHAPES: dict[int, re.Pattern[str]] = {
    0x01: re.compile(r"01(?:[0-9A-F]{2}){1,6}"),  # live data, 1-6 PIDs
    0x02: re.compile(r"02[0-9A-F]{2}00"),  # freeze frame, one PID, frame 00
    0x03: re.compile(r"03"),  # stored codes
    0x07: re.compile(r"07"),  # pending codes
    0x09: re.compile(r"09[0-9A-F]{2}"),  # vehicle info, one PID
    0x0A: re.compile(r"0A"),  # permanent codes
}

# UDS services that write, reset, unlock or control. Named so the refusal says why.
_UDS_SERVICES: frozenset[int] = frozenset(
    {0x10, 0x11, 0x14, 0x27, 0x28, 0x2E, 0x2F, 0x31, 0x34, 0x35, 0x36, 0x37, 0x3B, 0x3D, 0x3E, 0x85}
)

_LEGAL_CHARS = re.compile(r"[0-9A-Za-z@ ]+")
_HEX_BYTES = re.compile(r"(?:[0-9A-F]{2})+")

_TOKEN = object()

Kind = Literal["at", "obd"]


def _refuse(shown: str, reason: ForbiddenReason) -> ForbiddenCommandError:
    logger.warning("Refused %r (%s)", shown, reason.value)
    return ForbiddenCommandError(shown, reason)


def _classify(command: str) -> tuple[str, Kind, int | None]:
    """Return (normalized text, kind, mode) or raise ForbiddenCommandError."""
    # fullmatch, not match: "$" would let a trailing newline through.
    if not _LEGAL_CHARS.fullmatch(command):
        raise _refuse(command, ForbiddenReason.MALFORMED)
    text = command.strip(" ").upper().replace(" ", "")
    if not text or len(text) > MAX_LENGTH:
        raise _refuse(command, ForbiddenReason.MALFORMED)

    if text in AT_COMMANDS:
        return text, "at", None
    if text.startswith(("AT", "ST")):
        raise _refuse(text, ForbiddenReason.AT_NOT_PERMITTED)

    if not _HEX_BYTES.fullmatch(text):
        raise _refuse(text, ForbiddenReason.MALFORMED)
    mode = int(text[:2], 16)
    shape = _OBD_SHAPES.get(mode)
    if shape is not None:
        if shape.fullmatch(text):
            return text, "obd", mode
        raise _refuse(text, ForbiddenReason.BAD_SHAPE)
    if mode in _UDS_SERVICES:
        raise _refuse(text, ForbiddenReason.UDS_SERVICE)
    raise _refuse(text, ForbiddenReason.MODE_NOT_PERMITTED)


@dataclass(frozen=True, slots=True)
class AllowedCommand:
    """A command that passed the allowlist. Only `parse()` can build one."""

    text: str  # normalized
    kind: Kind
    mode: int | None  # for obd
    token: InitVar[object] = None

    def __post_init__(self, token: object) -> None:
        if token is not _TOKEN:
            raise TypeError("AllowedCommand can only be built by allowlist.parse().")
        # Even with the token, the fields must be what the grammar produces for this text.
        if _classify(self.text) != (self.text, self.kind, self.mode):
            raise TypeError("AllowedCommand fields do not match the allowlist grammar.")

    @property
    def wire(self) -> bytes:
        """The bytes a transport writes. Re-checked here in case the object was tampered with."""
        _classify(self.text)
        return self.text.encode("ascii") + b"\r"


def parse(command: str) -> AllowedCommand:
    """Normalize and check a command. Raises ForbiddenCommandError for anything not a read."""
    text, kind, mode = _classify(command)
    return AllowedCommand(text, kind, mode, _TOKEN)


def is_allowed(command: str) -> bool:
    try:
        _classify(command)
    except ForbiddenCommandError:
        return False
    return True
