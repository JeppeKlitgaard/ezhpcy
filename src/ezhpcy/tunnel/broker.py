"""Foreground broker and ProxyCommand stream relays."""

from __future__ import annotations

import os
import socket
import threading
from typing import BinaryIO, Callable

import paramiko

from ezhpcy.tunnel.ipc import (
    FramedConnection,
    IPCBackend,
    IPCError,
    ProtocolError,
    accept_worker_stream,
    ready_worker_stream,
    reject_worker_stream,
    request_worker_stream,
)
from ezhpcy.tunnel.relay import _shutdown_write

ErrorHandler = Callable[[Exception], None]


def _channel_to_ipc(channel: paramiko.Channel, connection: FramedConnection) -> None:
    received_data = False
    while data := channel.recv(64 * 1024):
        received_data = True
        connection.send_data(data)
    if received_data:
        connection.send_eof()
    else:
        reject_worker_stream(
            connection,
            "worker connection closed before sending an SSH banner; verify that "
            "the worker daemon is running and that WORKER_HOST and --worker-port "
            "match its endpoint",
        )


def _ipc_to_channel(connection: FramedConnection, channel: paramiko.Channel) -> None:
    while (data := connection.receive_data()) is not None:
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
        self._listener = None
        self._connections: set[FramedConnection] = set()
        self._threads: set[threading.Thread] = set()
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._started = threading.Event()
        self._startup_error: Exception | None = None

    def serve_forever(self) -> None:
        try:
            try:
                self._listener = self.backend.listen()
            except Exception as error:
                self._startup_error = error
                self._report(error)
                return
            finally:
                self._started.set()

            while not self._stopped.is_set():
                try:
                    stream = self._listener.accept()
                except Exception as error:
                    if self._stopped.is_set():
                        break
                    self._report(error)
                    continue
                connection = FramedConnection(stream)
                thread = threading.Thread(
                    target=self._serve_client,
                    args=(connection,),
                    daemon=True,
                    name="ezhpcy-broker-client",
                )
                with self._lock:
                    self._connections.add(connection)
                    self._threads.add(thread)
                thread.start()
        finally:
            self.close()

    def wait_until_ready(self, timeout: float = 2.0) -> None:
        """Wait until the listener and its runtime descriptor are available."""
        if not self._started.wait(timeout):
            raise IPCError("broker IPC listener did not start in time")
        if self._startup_error is not None:
            raise self._startup_error

    def _serve_client(self, connection: FramedConnection) -> None:
        channel = None
        try:
            accept_worker_stream(connection)
            if not self.transport.is_active():
                reject_worker_stream(
                    connection,
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
                    connection,
                    f"worker channel could not be opened: {error}",
                )
                return
            ready_worker_stream(connection)
            outgoing = threading.Thread(
                target=self._forward_channel,
                args=(channel, connection),
                daemon=True,
                name="ezhpcy-worker-to-proxy",
            )
            outgoing.start()
            _ipc_to_channel(connection, channel)
            outgoing.join()
        except (EOFError, IPCError, OSError, socket.error) as error:
            if not isinstance(error, (EOFError, ProtocolError)):
                self._report(error)
        finally:
            if channel is not None:
                channel.close()
            connection.close()
            with self._lock:
                self._connections.discard(connection)
                self._threads.discard(threading.current_thread())

    def _forward_channel(
        self, channel: paramiko.Channel, connection: FramedConnection
    ) -> None:
        try:
            _channel_to_ipc(channel, connection)
        except (EOFError, IPCError, OSError) as error:
            try:
                reject_worker_stream(
                    connection,
                    "worker stream was interrupted; restart the broker if the "
                    f"login-node connection was lost ({error})",
                )
            except EOFError, IPCError, OSError:
                pass

    def _report(self, error: Exception) -> None:
        if self.error_handler is not None:
            self.error_handler(error)

    def close(self) -> None:
        self._stopped.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
        with self._lock:
            connections = list(self._connections)
        for connection in connections:
            connection.close()


def relay_proxy_stdio(
    backend: IPCBackend,
    stdin: BinaryIO,
    stdout: BinaryIO,
    *,
    connect_timeout: float = 2.0,
) -> None:
    """Connect to the broker and reserve stdout exclusively for SSH bytes."""
    connection = FramedConnection(backend.connect(timeout=connect_timeout))
    try:
        request_worker_stream(connection)

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
                    connection.send_data(data)
                connection.send_eof()
            except OSError, IPCError, ValueError:
                pass

        sender = threading.Thread(
            target=send_stdin, daemon=True, name="ezhpcy-proxy-stdin"
        )
        sender.start()
        while (data := connection.receive_data()) is not None:
            stdout.write(data)
            stdout.flush()
    finally:
        connection.close()
