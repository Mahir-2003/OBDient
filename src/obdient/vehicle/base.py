"""The read-only Vehicle protocol (SPEC §4.1).

Everything above this interface works the same against the simulator, the emulator, a
replayed session or the real car.
"""

from collections.abc import Sequence
from typing import Protocol

from obdient.schemas.common import Condition, SnapshotSource
from obdient.schemas.snapshot import (
    AdapterInfo,
    Dtc,
    FreezeFrame,
    ReadError,
    Reading,
    SinceClear,
    StatusSinceClear,
)


class Vehicle(Protocol):
    """A car that can only be read.

    After `connect()` succeeds no method raises for a per-request failure: it returns what it
    could read (`None`, `[]`, or a partial list) and records a `ReadError`, which the caller
    collects with `drain_errors()`.

    There is no method that writes, clears or commands. Do not add one.
    """

    source: SnapshotSource

    def connect(self) -> AdapterInfo:
        """Raises VehicleConnectionError or UnsupportedProtocolError."""
        ...

    def close(self) -> None: ...

    def supported_pids(self) -> list[str]:
        """Mode 01 PIDs, sorted, e.g. ["0101", "0103", ...]."""
        ...

    def vin(self) -> str | None: ...

    def status(self) -> StatusSinceClear | None:
        """PID 0101: MIL, DTC count and readiness, which arrive in one request."""
        ...

    def read_dtcs(self) -> list[Dtc]:
        """Modes 03, 07 and 0A; one Dtc per (code, status, ecu)."""
        ...

    def freeze_frame(self) -> FreezeFrame | None: ...

    def since_clear(self) -> SinceClear: ...

    def read_pids(
        self, pids: Sequence[str], condition: Condition = Condition.UNKNOWN
    ) -> list[Reading]:
        """Failed PIDs are left out and recorded; unsupported PIDs are never sent."""
        ...

    def drain_errors(self) -> list[ReadError]:
        """Return and clear the errors accumulated since the last call."""
        ...
