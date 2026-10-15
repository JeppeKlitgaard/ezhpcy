"""The proxy's side of the tunnel IPC, against stand-ins for the tunnel."""

import io
import os
import socket
import struct
import threading
import time
from collections.abc import Callable

import pytest

from ezhpcy.ipc import IPCError, protocol
from ezhpcy.ipc.client import connect, relay_proxy_stdio
from ezhpcy.ipc.descriptor import TunnelDescriptor

AUTHKEY = b"a" * 32


def test_connecting_without_a_tunnel_fails_quickly() -> None:
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    started = time.monotonic()

    with pytest.raises(IPCError, match="no tunnel is running for `gpu`; start"):
        connect(TunnelDescriptor("gpu", port=port, authkey=AUTHKEY), timeout=0.05)
    assert time.monotonic() - started < 1


def test_authentication_has_an_overall_deadline() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        release = threading.Event()

        def stall() -> None:
            connection, _address = listener.accept()
            with connection:
                release.wait(timeout=1)

        thread = threading.Thread(target=stall)
        thread.start()
        descriptor = TunnelDescriptor(
            "gpu", port=listener.getsockname()[1], authkey=AUTHKEY
        )
        started = time.monotonic()
        with pytest.raises(IPCError, match="authentication failed"):
            connect(descriptor, timeout=0.05)
        elapsed = time.monotonic() - started
        release.set()
        thread.join(timeout=1)

    assert elapsed < 1
    assert not thread.is_alive()


def test_an_impostor_without_the_key_is_rejected() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()

        def impersonate_tunnel() -> None:
            connection, _address = listener.accept()
            with connection:
                connection.sendall(
                    protocol.AUTH_MAGIC + b"s" * protocol.AUTH_NONCE_SIZE
                )
                response_size = protocol.AUTH_NONCE_SIZE + protocol.AUTH_DIGEST_SIZE
                response = bytearray()
                while len(response) < response_size:
                    response.extend(connection.recv(response_size - len(response)))
                connection.sendall(b"x" * protocol.AUTH_DIGEST_SIZE)

        thread = threading.Thread(target=impersonate_tunnel)
        thread.start()
        descriptor = TunnelDescriptor(
            "gpu", port=listener.getsockname()[1], authkey=AUTHKEY
        )
        with pytest.raises(IPCError, match="authentication failed"):
            connect(descriptor, timeout=1)
        thread.join(timeout=1)

    assert not thread.is_alive()


def tunnel_stream(
    reply: bytes, worker: Callable[[socket.socket], None] | None = None
) -> socket.socket:
    """
    Return the proxy's end of an authenticated stream, whose tunnel end sends
    `reply` and then, if given, acts as the `worker`.
    """
    proxy_end, tunnel_end = socket.socketpair()

    def serve() -> None:
        with tunnel_end:
            tunnel_end.sendall(reply)
            if worker is not None:
                worker(tunnel_end)

    threading.Thread(target=serve, daemon=True).start()
    return proxy_end


def receive_all(stream: socket.socket) -> bytes:
    received = bytearray()
    while chunk := stream.recv(65536):
        received.extend(chunk)
    return bytes(received)


READY = bytes((protocol.READY,))


def test_relay_waits_for_the_client_before_the_worker_answers() -> None:
    def answer_after_client(stream: socket.socket) -> None:
        stream.sendall(b"worker:" + receive_all(stream))

    output = io.BytesIO()
    with tunnel_stream(READY, answer_after_client) as stream:
        relay_proxy_stdio(stream, io.BytesIO(b"SSH-2.0-client\r\n"), output)

    assert output.getvalue() == b"worker:SSH-2.0-client\r\n"


def test_relay_forwards_the_worker_banner_and_the_reply() -> None:
    def banner_then_answer(stream: socket.socket) -> None:
        stream.sendall(b"SSH-2.0-worker\r\n")
        stream.sendall(b"worker:" + receive_all(stream))

    output = io.BytesIO()
    with tunnel_stream(READY, banner_then_answer) as stream:
        relay_proxy_stdio(stream, io.BytesIO(b"SSH-2.0-client\r\n"), output)

    assert output.getvalue() == b"SSH-2.0-worker\r\nworker:SSH-2.0-client\r\n"


class FilenoOnlyStdin:
    def __init__(self, descriptor: int) -> None:
        self.descriptor = descriptor
        self.buffered_read_called = False

    def fileno(self) -> int:
        return self.descriptor

    def read(self, _size: int) -> bytes:
        self.buffered_read_called = True
        raise AssertionError("ProxyCommand stdin must use the raw descriptor")


def test_relay_does_not_leave_a_buffered_stdin_reader_at_shutdown() -> None:
    def banner_then_close(stream: socket.socket) -> None:
        stream.sendall(b"SSH-2.0-worker\r\n")

    read_descriptor, write_descriptor = os.pipe()
    stdin = FilenoOnlyStdin(read_descriptor)
    output = io.BytesIO()
    try:
        with tunnel_stream(READY, banner_then_close) as stream:
            relay_proxy_stdio(stream, stdin, output)
        assert output.getvalue() == b"SSH-2.0-worker\r\n"
        assert not stdin.buffered_read_called
    finally:
        os.close(write_descriptor)
        deadline = time.monotonic() + 1
        while any(
            thread.name == "ezhpcy-proxy-stdin" for thread in threading.enumerate()
        ):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        os.close(read_descriptor)


def rejection(message: bytes) -> bytes:
    return bytes((protocol.ERROR,)) + struct.pack("!H", len(message)) + message


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        (rejection(b"worker channel could not be opened"), "could not be opened"),
        (rejection(b""), "tunnel rejected stream"),
        (rejection(b"x" * (protocol.MAX_ERROR_SIZE + 1)), "oversized error"),
        (b"\x07", "invalid readiness response"),
        (b"", "tunnel IPC connection closed"),
    ],
)
def test_relay_reports_a_tunnel_that_does_not_open_the_stream(
    reply: bytes, error: str
) -> None:
    output = io.BytesIO()
    with tunnel_stream(reply) as stream, pytest.raises(IPCError, match=error):
        relay_proxy_stdio(stream, io.BytesIO(b"never sent"), output)

    assert output.getvalue() == b""
