"""SPEC §4.2: the required allowlist tests, in full."""

import logging
import re
from pathlib import Path

import pytest

from obdient.errors import ForbiddenCommandError, ForbiddenReason
from obdient.vehicle import allowlist
from obdient.vehicle.allowlist import AT_COMMANDS, AllowedCommand, is_allowed, parse

SRC = Path(__file__).resolve().parents[2] / "src" / "obdient"

SPEC_AT_COMMANDS = {
    "ATZ", "ATE0", "ATL0", "ATS0", "ATS1", "ATH0", "ATH1",
    "ATSP0", "ATDPN", "ATRV", "ATI", "AT@1", "ATCAF1", "ATAT1", "ATAT2",
    "ATSH7DF", "ATSH7E0",
    "STI", "STDI",
}  # fmt: skip

MODE = ForbiddenReason.MODE_NOT_PERMITTED
UDS = ForbiddenReason.UDS_SERVICE
SHAPE = ForbiddenReason.BAD_SHAPE
AT = ForbiddenReason.AT_NOT_PERMITTED
MALFORMED = ForbiddenReason.MALFORMED


# ---- permitted --------------------------------------------------------------


def test_at_set_is_exactly_the_spec_list() -> None:
    assert AT_COMMANDS == SPEC_AT_COMMANDS


@pytest.mark.parametrize("command", sorted(SPEC_AT_COMMANDS))
def test_every_at_command_parses_in_any_spelling(command: str) -> None:
    spaced = f"  {command[:2]} {command[2:]} "
    for spelling in (command, command.lower(), spaced, spaced.lower()):
        allowed = parse(spelling)
        assert (allowed.text, allowed.kind, allowed.mode) == (command, "at", None)
        assert allowed.wire == command.encode() + b"\r"


@pytest.mark.parametrize(
    "command,text,mode",
    [
        ("0100", "0100", 0x01),
        ("010C", "010C", 0x01),
        ("010c", "010C", 0x01),
        ("01 0C 0D", "010C0D", 0x01),
        ("01040506070B0C", "01040506070B0C", 0x01),
        ("020200", "020200", 0x02),
        ("020C00", "020C00", 0x02),
        ("03", "03", 0x03),
        ("07", "07", 0x07),
        ("0A", "0A", 0x0A),
        ("0a", "0A", 0x0A),
        ("0902", "0902", 0x09),
        ("0904", "0904", 0x09),
    ],
)
def test_read_requests_parse(command: str, text: str, mode: int) -> None:
    allowed = parse(command)
    assert (allowed.text, allowed.kind, allowed.mode) == (text, "obd", mode)
    assert allowed.wire == text.encode() + b"\r"
    assert is_allowed(command)


# ---- refused ----------------------------------------------------------------


@pytest.mark.parametrize(
    "command,reason",
    [
        ("04", MODE),
        ("0400", MODE),
        ("08", MODE),
        ("0801", MODE),
        ("05", MODE),
        ("06", MODE),
        ("0600", MODE),
        ("2E F190 00", UDS),
        ("2F", UDS),
        ("31 01 FF00", UDS),
        ("3B", UDS),
        ("3D", UDS),
        ("14", UDS),
        ("14FFFFFF", UDS),
        ("10 03", UDS),
        ("11 01", UDS),
        ("27 01", UDS),
        ("3E00", UDS),
        ("0101010101010101", SHAPE),  # 7 PIDs
        ("01", SHAPE),
        ("020C01", SHAPE),  # frame != 00
        ("0300", SHAPE),
        ("09", SHAPE),
        ("ATPP", AT),
        ("ATSP6", AT),
        ("ATMA", AT),
        ("ATD", AT),
        ("ATWS", AT),
        ("ATSH7E1", AT),
        ("ATSH 7E8", AT),
        ("ATCAF0", AT),
        ("ATAT0", AT),
        ("ATST FF", AT),
        ("STPX", AT),
        ("STBR", AT),
        ("", MALFORMED),
        ("   ", MALFORMED),
        ("0100\r04", MALFORMED),
        ("03;04", MALFORMED),
        ("0G", MALFORMED),
        ("010", MALFORMED),
        ("03\n", MALFORMED),
        ("03\t", MALFORMED),
        ("0100" * 5, MALFORMED),  # longer than 16 characters
    ],
)
def test_refused_with_reason(command: str, reason: ForbiddenReason) -> None:
    with pytest.raises(ForbiddenCommandError) as info:
        parse(command)
    assert info.value.reason is reason
    assert not is_allowed(command)


def test_every_single_byte_except_the_three_read_modes_is_refused() -> None:
    allowed = {"03", "07", "0A"}
    for value in range(256):
        command = f"{value:02X}"
        if command in allowed:
            assert is_allowed(command)
        else:
            with pytest.raises(ForbiddenCommandError):
                parse(command)


def test_refusal_message_and_log(caplog: pytest.LogCaptureFixture) -> None:
    with (
        caplog.at_level(logging.WARNING, logger="obdient.allowlist"),
        pytest.raises(ForbiddenCommandError) as info,
    ):
        parse("04")
    assert str(info.value) == (
        "Refused '04' (mode_not_permitted). OBDient is read-only: only OBD modes "
        "01, 02, 03, 07, 09, 0A and a fixed AT/ST setup list may be sent."
    )
    assert info.value.command == "04"
    assert [r.levelname for r in caplog.records] == ["WARNING"]
    assert "04" in caplog.records[0].getMessage()
    assert "mode_not_permitted" in caplog.records[0].getMessage()


def test_malformed_refusal_shows_the_original_text() -> None:
    with pytest.raises(ForbiddenCommandError) as info:
        parse("0100\r04")
    assert info.value.command == "0100\r04"


# ---- construction guard -----------------------------------------------------


def test_allowed_command_cannot_be_built_outside_the_module() -> None:
    with pytest.raises(TypeError):
        AllowedCommand("04", "obd", 4)
    with pytest.raises(TypeError):
        AllowedCommand("03", "obd", 3)  # even a legal command needs parse()


def test_even_the_private_token_cannot_build_a_forbidden_command() -> None:
    token = allowlist._TOKEN  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(ForbiddenCommandError):
        AllowedCommand("04", "obd", 4, token)
    with pytest.raises(TypeError):
        AllowedCommand("03", "at", None, token)  # fields disagree with the grammar


def test_wire_rechecks_a_tampered_command() -> None:
    command = parse("03")
    object.__setattr__(command, "text", "04")
    with pytest.raises(ForbiddenCommandError):
        _ = command.wire


# ---- source scan ------------------------------------------------------------

WRITE_CALLS = re.compile(
    r"\bimport socket\b|\bimport serial\b|\bfrom serial\b|\bbleak\b"
    r"|\.sendall\(|\.sendto\(|\.sendmsg\(|\.write\(|write_gatt_char"
)
TRANSPORT_WRITES = re.compile(r"\.sendall\(|\.sendto\(|\.sendmsg\(|\.send\(|\.write\(|write_gatt")


def test_only_transports_can_write_to_an_adapter() -> None:
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        relative = path.relative_to(SRC).as_posix()
        if relative == "vehicle/allowlist.py" or relative.startswith("vehicle/transports/"):
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if WRITE_CALLS.search(line):
                offenders.append(f"{relative}:{number}: {line.strip()}")
    assert offenders == []


def test_transports_write_only_allowed_command_wire() -> None:
    offenders: list[str] = []
    files = list((SRC / "vehicle" / "transports").glob("*.py"))
    assert files
    for path in files:
        for number, line in enumerate(path.read_text().splitlines(), 1):
            code = line.split("#")[0]
            if code.lstrip().startswith("def "):
                continue
            if TRANSPORT_WRITES.search(code) and ".wire" not in code:
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert offenders == []
