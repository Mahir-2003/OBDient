"""TCP transport, used for ELM327-emulator on 127.0.0.1:35000."""

import socket
import time

from obdient.errors import TransportClosed, TransportError, TransportTimeout
from obdient.vehicle.allowlist import AllowedCommand
from obdient.vehicle.transports import PROMPT


class TcpTransport:
    def __init__(
        self, host: str = "127.0.0.1", port: int = 35000, connect_timeout_s: float = 3.0
    ) -> None:
        self._host = host
        self._port = port
        self._connect_timeout_s = connect_timeout_s
        self._sock: socket.socket | None = None

    def open(self) -> None:
        try:
            self._sock = socket.create_connection(
                (self._host, self._port), timeout=self._connect_timeout_s
            )
        except OSError as exc:
            raise TransportError(f"Cannot connect to {self._host}:{self._port}: {exc}") from exc

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def send(self, command: AllowedCommand, timeout_s: float) -> bytes:
        # A runtime check on purpose: the type hint alone would not stop a plain string.
        if not isinstance(command, AllowedCommand):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(
                "Transport.send accepts only AllowedCommand (see vehicle/allowlist.py)."
            )
        sock = self._sock
        if sock is None:
            raise TransportClosed("Transport is not open.")
        try:
            self._discard_buffered(sock)
            sock.settimeout(timeout_s)
            sock.sendall(command.wire)
            return self._read_until_prompt(sock, time.monotonic() + timeout_s)
        except TimeoutError as exc:
            raise TransportTimeout(f"No prompt within {timeout_s} s after {command.text}") from exc
        except OSError as exc:
            raise TransportClosed(f"Connection lost during {command.text}: {exc}") from exc

    @staticmethod
    def _discard_buffered(sock: socket.socket) -> None:
        sock.setblocking(False)
        try:
            while sock.recv(4096):
                pass
        except BlockingIOError:
            pass  # nothing buffered

    @staticmethod
    def _read_until_prompt(sock: socket.socket, deadline: float) -> bytes:
        buffer = bytearray()
        while PROMPT not in buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            sock.settimeout(remaining)
            chunk = sock.recv(4096)
            if not chunk:
                raise TransportClosed("Adapter closed the connection.")
            buffer += chunk
        return bytes(buffer[: buffer.index(PROMPT)])
