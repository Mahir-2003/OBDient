"""Transports: the only code that writes bytes to an adapter (SPEC §4.3)."""

from typing import Protocol

from obdient.vehicle.allowlist import AllowedCommand

PROMPT = b">"


class Transport(Protocol):
    def open(self) -> None:
        """Raises TransportError."""
        ...

    def close(self) -> None: ...

    def send(self, command: AllowedCommand, timeout_s: float) -> bytes:
        """Write `command.wire`, read until the '>' prompt, return the bytes before it.

        Discards anything already buffered before writing and never interprets the reply.
        Raises TransportTimeout (no prompt in time) or TransportClosed.
        """
        ...
