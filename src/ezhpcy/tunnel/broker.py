"""Foreground broker and ProxyCommand stream relays."""

from __future__ import annotations

import os
import socket
import threading
from typing import BinaryIO, Callable

import paramiko

from ezhpcy.tunnel.ipc import (
    IPCBackend,
    ready_worker_stream,
    reject_worker_stream,
    wait_for_worker_stream,
)
from ezhpcy.tunnel.relay import _shutdown_write

ErrorHandler = Callable[[Exception], None]
WORKER_BANNER_TIMEOUT = 10.0


def _channel_to_socket(channel: paramiko.Channel, stream: socket.socket) -> None:
    while data := channel.recv(64 * 1024):
        stream.sendall(data)
    _shutdown_write(stream)


def _socket_to_channel(stream: socket.socket, channel: paramiko.Channel) -> None:
    while data := stream.recv(64 * 1024):
        channel.sendall(data)
    _shutdown_write(channel)


class ForegroundBroker:
    """Own one login transport and multiplex worker channels over local IPC."""

    def __init__(
        self,
        transport: paramiko.Transport,
        destination: tuple[str, int],
        backend: IPCBackend,
        *,
        error_handler: ErrorHandler | None = None,
    ) -> None:
        self.transport = transport
        self.destination = destination
        self.backend = backend
        self.error_handler = error_handler
        self._server = backend.listen(self._serve_client, self._report)

    def serve_forever(self) -> None:
        self._server.serve_forever()

    def _serve_client(self, stream: socket.socket) -> None:
        channel = None
        try:
            if not self.transport.is_active():
                reject_worker_stream(
                    stream,
                    "login-node transport was lost; restart the foreground broker",
                )
                return
            try:
                channel = self.transport.open_channel(
                    "direct-tcpip",
                    dest_addr=self.destination,
                    src_addr=("ezhpcy-proxy", 0),
                )
            except Exception as error:
                reject_worker_stream(
                    stream,
                    f"worker channel could not be opened: {error}",
                )
                return
            channel.settimeout(WORKER_BANNER_TIMEOUT)
            try:
                first_data = channel.recv(64 * 1024)
            except (OSError, paramiko.SSHException) as error:
                reject_worker_stream(
                    stream,
                    f"worker connection failed before sending an SSH banner: {error}",
                )
                return
            finally:
                channel.settimeout(None)
            if not first_data:
                reject_worker_stream(
                    stream,
                    "worker connection closed before sending an SSH banner; verify "
                    "that the worker daemon is running and that WORKER_HOST and "
                    "--worker-port match its endpoint",
                )
                return
            ready_worker_stream(stream)
            stream.sendall(first_data)
            outgoing = threading.Thread(
                target=self._forward_channel,
                args=(channel, stream),
                daemon=True,
                name="ezhpcy-worker-to-proxy",
            )
            outgoing.start()
            _socket_to_channel(stream, channel)
            outgoing.join()
        except OSError as error:
            self._report(error)
        finally:
            if channel is not None:
                channel.close()

    def _forward_channel(
        self, channel: paramiko.Channel, stream: socket.socket
    ) -> None:
        try:
            _channel_to_socket(channel, stream)
        except (OSError, paramiko.SSHException) as error:
            self._report(error)
            try:
                _shutdown_write(stream)
            except OSError:
                pass

    def _report(self, error: Exception) -> None:
        if self.error_handler is not None:
            self.error_handler(error)

    def close(self) -> None:
        self._server.close()


def relay_proxy_stdio(
    backend: IPCBackend,
    stdin: BinaryIO,
    stdout: BinaryIO,
    *,
    connect_timeout: float = 2.0,
) -> None:
    """Connect to the broker and reserve stdout exclusively for SSH bytes."""
    stream = backend.connect(timeout=connect_timeout)
    try:
        wait_for_worker_stream(stream)

        try:
            stdin_fd = stdin.fileno()
        except AttributeError, OSError, ValueError:

            def read_stdin(size: int) -> bytes:
                return stdin.read(size)
        else:
            # ProxyCommand stdin is an OpenSSH pipe. Reading its raw descriptor
            # avoids leaving a daemon thread holding BufferedReader's lock when
            # the worker closes its side of the session first.
            def read_stdin(size: int) -> bytes:
                return os.read(stdin_fd, size)

        def send_stdin() -> None:
            try:
                while data := read_stdin(64 * 1024):
                    stream.sendall(data)
                _shutdown_write(stream)
            except OSError, ValueError:
                pass

        sender = threading.Thread(
            target=send_stdin, daemon=True, name="ezhpcy-proxy-stdin"
        )
        sender.start()
        while data := stream.recv(64 * 1024):
            stdout.write(data)
            stdout.flush()
    finally:
        stream.close()
