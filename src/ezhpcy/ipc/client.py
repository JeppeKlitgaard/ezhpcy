"""
The proxy's side of the tunnel IPC.

Synchronous implementation for `ezhpcy proxy`
"""

import logging
lazy import os
lazy import socket
lazy import threading
lazy import time
lazy from typing import BinaryIO

from ezhpcy.ipc.protocol import (
    AUTHENTICATION_TIMEOUT,
    authenticate_client,
    wait_for_worker_stream,
)
lazy from ezhpcy.constants import LOOPBACK_HOST
lazy from ezhpcy.ipc import IPCError
lazy from ezhpcy.ipc.descriptor import TunnelDescriptor, tunnel_not_running

logger = logging.getLogger(__name__)


def connect(
    descriptor: TunnelDescriptor, *, timeout: float = AUTHENTICATION_TIMEOUT
) -> socket.socket:
    """Connect to the tunnel and authenticate, each within `timeout` seconds."""
    logger.debug(
        "Proxy connecting to tunnel: port=%d timeout_seconds=%g",
        descriptor.port,
        timeout,
    )
    try:
        stream = socket.create_connection(
            (LOOPBACK_HOST, descriptor.port), timeout=timeout
        )
    except OSError as error:
        raise tunnel_not_running(descriptor.alias) from error

    try:
        authenticate_client(stream, descriptor.authkey, timeout)
        stream.settimeout(None)
    except (IPCError, OSError) as error:
        stream.close()
        raise IPCError("tunnel authentication failed; restart the tunnel") from error
    return stream


def relay_proxy_stdio(stream: socket.socket, stdin: BinaryIO, stdout: BinaryIO) -> None:
    """
    Relay stdin and stdout over an authenticated tunnel stream, once the tunnel
    has opened its channel to the worker. Stdout carries only the worker's bytes.
    """
    started = time.monotonic()
    sent = 0
    received = 0
    try:
        wait_for_worker_stream(stream)
        logger.debug("Proxy relay ready; worker stream accepted by the tunnel")

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
                stream.shutdown(socket.SHUT_WR)
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
        logger.debug("Proxy relay reached tunnel EOF: bytes_received=%d", received)
    finally:
        logger.debug(
            "Proxy relay finished: duration_seconds=%.3f bytes_sent=%d "
            "bytes_received=%d",
            time.monotonic() - started,
            sent,
            received,
        )
