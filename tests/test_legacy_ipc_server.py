import socket
import threading
import time
from collections.abc import Callable

import pytest

from ezhpcy.ipc import IPCError, protocol
from ezhpcy.ipc._legacy_server import (
    AuthenticatedIPCServer,
    create_tunnel_backend,
    reject_worker_stream,
)
from ezhpcy.ipc.client import connect
from ezhpcy.ipc.descriptor import TunnelDescriptor

AUTHKEY = b"a" * 32


def start_server(
    handler: Callable[[socket.socket], None],
    error_handler: Callable[[Exception], None] | None = None,
    *,
    authentication_timeout: float = protocol.AUTHENTICATION_TIMEOUT,
) -> tuple[AuthenticatedIPCServer, threading.Thread]:
    server = AuthenticatedIPCServer(
        authkey=AUTHKEY,
        authentication_timeout=authentication_timeout,
        client_handler=handler,
        error_handler=error_handler,
    )
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    return server, thread


def descriptor_for(
    server: AuthenticatedIPCServer, authkey: bytes = AUTHKEY
) -> TunnelDescriptor:
    return TunnelDescriptor("test", port=server.port, authkey=authkey)


def test_backend_publishes_its_descriptor_until_closed() -> None:
    backend = create_tunnel_backend(alias="test", authkey=AUTHKEY, debug=True)
    received: list[bytes] = []

    def serve(connection: socket.socket) -> None:
        received.append(connection.recv(4))
        connection.sendall(b"pong")

    listener = backend.listen(serve)
    thread = threading.Thread(target=listener.serve_forever)
    thread.start()
    descriptor = TunnelDescriptor.load("test")

    assert descriptor == TunnelDescriptor(
        "test",
        port=listener.port,
        authkey=AUTHKEY,
        instance_id=backend.instance_id,
        debug=True,
    )
    with connect(descriptor, timeout=1) as client:
        client.sendall(b"ping")
        assert client.recv(4) == b"pong"
    listener.close()
    thread.join(timeout=1)

    assert received == [b"ping"]
    assert not descriptor.path.exists()


def test_worker_stream_rejection_is_bounded_and_actionable() -> None:
    client, server = socket.socketpair()
    thread = threading.Thread(
        target=reject_worker_stream,
        args=(server, "worker failed: " + "x" * 2000),
    )
    thread.start()

    with pytest.raises(IPCError, match="worker failed") as raised:
        protocol.wait_for_worker_stream(client)
    thread.join(timeout=1)
    client.close()
    server.close()

    assert len(str(raised.value).encode()) <= protocol.MAX_ERROR_SIZE


def test_wrong_authkey_is_rejected() -> None:
    errors: list[Exception] = []
    rejected = threading.Event()

    def report(error: Exception) -> None:
        errors.append(error)
        rejected.set()

    server, thread = start_server(lambda _connection: None, report)
    with pytest.raises(IPCError, match="authentication failed"):
        connect(descriptor_for(server, b"b" * 32), timeout=1)
    assert rejected.wait(timeout=1)
    server.close()
    thread.join(timeout=1)

    assert errors and "rejected a local client" in str(errors[0])


def test_listener_close_cancels_blocked_accept() -> None:
    server, thread = start_server(lambda _connection: None)
    server.close()
    thread.join(timeout=1)

    assert not thread.is_alive()


def test_listener_closed_before_serving_returns_immediately() -> None:
    server = AuthenticatedIPCServer(
        authkey=AUTHKEY,
        authentication_timeout=protocol.AUTHENTICATION_TIMEOUT,
        client_handler=lambda _connection: None,
        error_handler=None,
    )
    server.close()
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    thread.join(timeout=1)

    assert not thread.is_alive()


def test_stalled_authentication_does_not_block_valid_client() -> None:
    errors: list[Exception] = []
    rejected = threading.Event()

    def serve(connection: socket.socket) -> None:
        assert connection.recv(4) == b"ping"
        connection.sendall(b"pong")

    def report(error: Exception) -> None:
        errors.append(error)
        rejected.set()

    server, thread = start_server(serve, report, authentication_timeout=0.1)
    with socket.create_connection(("127.0.0.1", server.port)) as _stalled:
        with connect(descriptor_for(server), timeout=1) as client:
            client.sendall(b"ping")
            assert client.recv(4) == b"pong"
        assert rejected.wait(timeout=1)

    server.close()
    thread.join(timeout=1)
    assert errors and "timed out" in str(errors[0])


def test_server_close_interrupts_stalled_authentication() -> None:
    server, thread = start_server(lambda _connection: None, authentication_timeout=10)
    with socket.create_connection(("127.0.0.1", server.port)) as stalled:
        greeting_size = len(protocol.AUTH_MAGIC) + protocol.AUTH_NONCE_SIZE
        greeting = bytearray()
        while len(greeting) < greeting_size:
            greeting.extend(stalled.recv(greeting_size - len(greeting)))

        started = time.monotonic()
        server.close()
        elapsed = time.monotonic() - started
        thread.join(timeout=1)
        stalled.settimeout(1)
        assert stalled.recv(1) == b""

    assert elapsed < 1
    assert not thread.is_alive()


def test_unexpected_client_handler_error_is_reported() -> None:
    errors: list[Exception] = []
    reported = threading.Event()

    def fail(_connection: socket.socket) -> None:
        raise RuntimeError("handler failed")

    def report(error: Exception) -> None:
        errors.append(error)
        reported.set()

    server, thread = start_server(fail, report)
    client = connect(descriptor_for(server), timeout=1)
    assert reported.wait(timeout=1)
    client.close()
    server.close()
    thread.join(timeout=1)

    assert errors and isinstance(errors[0], RuntimeError)
    assert not thread.is_alive()
