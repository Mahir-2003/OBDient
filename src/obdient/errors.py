"""All exception types (SPEC §1.1).

This module sits at the bottom of the import graph: it imports nothing from the package.
"""

from enum import StrEnum


class ObdientError(Exception):
    """Base class for every error OBDient raises on purpose."""


# ---- vehicle ---------------------------------------------------------------


class ForbiddenReason(StrEnum):
    """Why the allowlist refused a command (SPEC §4.2). Re-exported by vehicle/allowlist.py."""

    MALFORMED = "malformed"
    AT_NOT_PERMITTED = "at_not_permitted"
    MODE_NOT_PERMITTED = "mode_not_permitted"
    UDS_SERVICE = "uds_service"
    BAD_SHAPE = "bad_shape"  # permitted mode, wrong length or structure


class ForbiddenCommandError(ObdientError):
    """The allowlist refused a command before anything was sent."""

    def __init__(self, command: str, reason: ForbiddenReason) -> None:
        self.command = command
        self.reason = reason
        super().__init__(
            f"Refused '{command}' ({reason.value}). OBDient is read-only: only OBD modes "
            "01, 02, 03, 07, 09, 0A and a fixed AT/ST setup list may be sent."
        )


class TransportError(ObdientError):
    """The link to the adapter failed."""


class TransportTimeout(TransportError):
    """No prompt arrived in time."""


class TransportClosed(TransportError):
    """The link is closed or was dropped."""


class VehicleConnectionError(ObdientError):
    """Adapter or ECU unreachable during connect()."""


class UnsupportedProtocolError(ObdientError):
    """The vehicle does not speak CAN 11-bit (SPEC §4.4)."""


class ParseError(ObdientError):
    """A reply could not be decoded."""

    def __init__(self, message: str, raw: str = "") -> None:
        self.raw = raw
        super().__init__(message)


# ---- knowledge -------------------------------------------------------------


class IndexNotBuiltError(ObdientError):
    """The OBDex index file is missing."""


class NhtsaUnavailable(ObdientError):
    """An NHTSA endpoint failed and nothing usable is cached."""


class UnknownEvidenceError(ObdientError, KeyError):
    """An evidence ID is not in the store."""


# ---- agent / runner --------------------------------------------------------


class LlmError(ObdientError):
    """The model provider failed after retries."""


class InternalReportError(ObdientError):
    """A code-built report failed the verifier: a bug."""


# ---- scenarios / storage ---------------------------------------------------


class ScenarioError(ObdientError):
    """A scenario file is invalid."""


class NotFoundError(ObdientError):
    """A stored object does not exist."""
