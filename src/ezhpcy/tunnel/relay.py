import select
import socket
import socketserver
from collections.abc import Callable
from contextlib import suppress
from typing import Protocol

import paramiko


class DuplexStream(Protocol):
    def fileno(self) -> int: ...

    def recv(self, bufsize: int) -> bytes: ...

    def sendall(self, data: bytes) -> None: ...

    def close(self) -> None: ...


def _shutdown_write(stream: DuplexStream) -> None:
    """Half-close a socket or Paramiko channel without masking relay errors."""
    shutdown_write = getattr(stream, "shutdown_write", None)
    if shutdown_write is not None:
        shutdown_write()
    else:
        stream.shutdown(socket.SHUT_WR)  # type: ignore[attr-defined]


def relay_streams(left: DuplexStream, right: DuplexStream) -> None:
    """Copy bytes in both directions, preserving EOF as a half-close."""
    readable: list[DuplexStream] = [left, right]
    peers = {left: right, right: left}

    try:
        while readable:
            ready, _, _ = select.select(readable, [], [])
            for source in ready:
                destination = peers[source]
                data = source.recv(64 * 1024)
                if data:
                    destination.sendall(data)
                    continue

                readable.remove(source)
                with suppress(OSError):
                    _shutdown_write(destination)
    finally:
        left.close()
        right.close()


class _DirectTCPIPHandler(socketserver.BaseRequestHandler):
    """Hand an accepted local connection to its relay server."""

    def handle(self) -> None:
        server: DirectTCPIPRelay = self.server  # type: ignore[assignment]
        client: socket.socket = self.request  # type: ignore[assignment]
        origin = (str(self.client_address[0]), int(self.client_address[1]))
        server._serve_client(client, origin)


class DirectTCPIPRelay(socketserver.ThreadingTCPServer):
    """Temporary loopback relay backed by one authenticated SSH transport."""

    daemon_threads = True
    block_on_close = False

    def __init__(
        self,
        transport: paramiko.Transport,
        destination: tuple[str, int],
        *,
        listen_host: str = "127.0.0.1",
        listen_port: int = 0,
        client_error_handler: Callable[[tuple[str, int], Exception], None]
        | None = None,
    ) -> None:
        self.transport = transport
        self.destination = destination
        self.client_error_handler = client_error_handler
        super().__init__((listen_host, listen_port), _DirectTCPIPHandler)

    @property
    def address(self) -> tuple[str, int]:
        host, port = self.server_address[:2]
        return str(host), int(port)

    def _serve_client(self, client: socket.socket, origin: tuple[str, int]) -> None:
        channel = None
        try:
            channel = self.transport.open_channel(
                "direct-tcpip",
                dest_addr=self.destination,
                src_addr=origin,
            )
            relay_streams(client, channel)
        except ConnectionResetError as error:
            if self.client_error_handler is not None:
                self.client_error_handler(origin, error)
        finally:
            client.close()
            if channel is not None:
                channel.close()
