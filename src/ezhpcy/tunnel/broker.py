"""Foreground broker and ProxyCommand stream relays."""

import itertools
import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from typing import BinaryIO

import paramiko

from ezhpcy.ipc.common import (
    IPCBackend,
)
from ezhpcy.ipc.protocol import (
    ready_worker_stream,
    reject_worker_stream,
    wait_for_worker_stream,
)

ErrorHandler = Callable[[Exception], None]
logger = logging.getLogger(__name__)


def _shutdown_write(stream: socket.socket | paramiko.Channel) -> None:
    """Half-close a socket or Paramiko channel without masking relay errors."""
    shutdown_write = getattr(stream, "shutdown_write", None)
    if shutdown_write is not None:
        shutdown_write()
    else:
        stream.shutdown(socket.SHUT_WR)  # type: ignore[attr-defined]


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
        self._connection_ids = itertools.count(1)
        self._server = backend.listen(self._serve_client, self._report)

    def serve_forever(self) -> None:
        self._server.serve_forever()

    def _serve_client(self, stream: socket.socket) -> None:
        connection_id = next(self._connection_ids)
        started = time.monotonic()
        channel = None
        logger.debug(
            "Broker client accepted: connection=%d destination=%s:%d "
            "transport_active=%s",
            connection_id,
            self.destination[0],
            self.destination[1],
            self.transport.is_active(),
        )
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
            # Paramiko documents SSHException here, but open_channel() also
            # re-raises exceptions saved by its transport thread, including
            # EOFError, socket errors, and exceptions from socket wrappers.
            # Keep this client boundary broad so every channel-open failure is
            # returned to the proxy without terminating the broker.
            except Exception as error:
                reject_worker_stream(
                    stream,
                    f"worker channel could not be opened: {error}",
                )
                return
            logger.debug(
                "Worker channel opened: connection=%d channel_id=%s",
                connection_id,
                getattr(channel, "chanid", None),
            )
            ready_worker_stream(stream)
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
            logger.debug(
                "Broker client closed: connection=%d duration_seconds=%.3f "
                "transport_active=%s",
                connection_id,
                time.monotonic() - started,
                self.transport.is_active(),
            )

    def _forward_channel(
        self, channel: paramiko.Channel, stream: socket.socket
    ) -> None:
        started = time.monotonic()
        try:
            _channel_to_socket(channel, stream)
            logger.debug(
                "Worker channel reached EOF: channel_id=%s duration_seconds=%.3f",
                getattr(channel, "chanid", None),
                time.monotonic() - started,
            )
        except (OSError, paramiko.SSHException) as error:
            # An unexpected mid-session drop
            logger.warning(
                "Worker channel relay failed: channel_id=%s duration_seconds=%.3f "
                "transport_active=%s error=%r",
                getattr(channel, "chanid", None),
                time.monotonic() - started,
                self.transport.is_active(),
                error,
            )
            self._report(error)
            try:
                _shutdown_write(stream)
            except OSError:
                pass

    def _report(self, error: Exception) -> None:
        logger.debug("Broker relay error: error=%r", error)
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
    started = time.monotonic()
    logger.debug(
        "Proxy relay connecting to broker: timeout_seconds=%g", connect_timeout
    )
    stream = backend.connect(timeout=connect_timeout)
    sent = 0
    received = 0
    try:
        wait_for_worker_stream(stream)
        logger.debug("Proxy relay ready; worker stream accepted by the broker")

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
            nonlocal sent
            try:
                while data := read_stdin(64 * 1024):
                    stream.sendall(data)
                    sent += len(data)
                logger.debug("Proxy relay reached stdin EOF: bytes_sent=%d", sent)
                _shutdown_write(stream)
            except (OSError, ValueError) as error:
                # The client half is gone; the main loop reports the outcome.
                logger.debug(
                    "Proxy relay stdin reader stopped: bytes_sent=%d error=%r",
                    sent,
                    error,
                )

        sender = threading.Thread(
            target=send_stdin, daemon=True, name="ezhpcy-proxy-stdin"
        )
        sender.start()
        while data := stream.recv(64 * 1024):
            stdout.write(data)
            stdout.flush()
            received += len(data)
        logger.debug("Proxy relay reached broker EOF: bytes_received=%d", received)
    finally:
        logger.debug(
            "Proxy relay finished: duration_seconds=%.3f bytes_sent=%d "
            "bytes_received=%d",
            time.monotonic() - started,
            sent,
            received,
        )
        stream.close()
