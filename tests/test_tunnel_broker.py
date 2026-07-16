import io
import os
import socket
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ezhpcy import ipc
from ezhpcy.ipc import create_broker_backend, load_broker_backend
from ezhpcy.ipc.common import IPCError
from ezhpcy.tunnel.broker import ForegroundBroker, relay_proxy_stdio


class ClientBackend:
    def __init__(self, connection: socket.socket) -> None:
        self.connection = connection

    def connect(self, *, timeout: float) -> socket.socket:
        return self.connection


class EchoTransport:
    def __init__(self, *, active: bool = True) -> None:
        self.active = active
        self.destinations: list[tuple[str, int]] = []
        self._lock = threading.Lock()

    def is_active(self) -> bool:
        return self.active

    def open_channel(self, _kind, *, dest_addr, src_addr):
        assert src_addr == ("ezhpcy-proxy", 0)
        broker_channel, worker = socket.socketpair()
        with self._lock:
            self.destinations.append(dest_addr)

        def echo() -> None:
            worker.sendall(b"worker:")
            request = bytearray()
            while chunk := worker.recv(65536):
                request.extend(chunk)
            worker.sendall(request)
            worker.shutdown(socket.SHUT_WR)
            worker.close()

        threading.Thread(target=echo, daemon=True).start()
        return broker_channel


class FailingTransport(EchoTransport):
    def open_channel(self, _kind, *, dest_addr, src_addr):
        raise OSError("channel rejected")


class ClientFirstTransport(EchoTransport):
    def open_channel(self, _kind, *, dest_addr, src_addr):
        broker_channel, worker = socket.socketpair()

        def respond_after_client() -> None:
            request = bytearray()
            while chunk := worker.recv(65536):
                request.extend(chunk)
            worker.sendall(b"worker:" + request)
            worker.shutdown(socket.SHUT_WR)
            worker.close()

        threading.Thread(target=respond_after_client, daemon=True).start()
        return broker_channel


class BannerTransport(EchoTransport):
    def open_channel(self, _kind, *, dest_addr, src_addr):
        broker_channel, worker = socket.socketpair()

        def serve_ssh_bytes() -> None:
            worker.sendall(b"SSH-2.0-test-worker\r\n")
            request = bytearray()
            while chunk := worker.recv(65536):
                request.extend(chunk)
            worker.sendall(b"worker:" + request)
            worker.shutdown(socket.SHUT_WR)
            worker.close()

        threading.Thread(target=serve_ssh_bytes, daemon=True).start()
        return broker_channel


class ClosingBannerTransport(EchoTransport):
    def open_channel(self, _kind, *, dest_addr, src_addr):
        broker_channel, worker = socket.socketpair()

        def send_banner_and_close() -> None:
            worker.sendall(b"SSH-2.0-test-worker\r\n")
            worker.close()

        threading.Thread(target=send_banner_and_close, daemon=True).start()
        return broker_channel


class FilenoOnlyStdin:
    def __init__(self, descriptor: int) -> None:
        self.descriptor = descriptor
        self.buffered_read_called = False

    def fileno(self) -> int:
        return self.descriptor

    def read(self, _size: int) -> bytes:
        self.buffered_read_called = True
        raise AssertionError("ProxyCommand stdin must use the raw descriptor")


def run_proxy(broker: ForegroundBroker, payload: bytes) -> bytes:
    client, server = socket.socketpair()
    broker_thread = threading.Thread(
        target=broker._serve_client,
        args=(server,),
    )
    broker_thread.start()
    output = io.BytesIO()
    relay_proxy_stdio(ClientBackend(client), io.BytesIO(payload), output)
    broker_thread.join(timeout=1)
    assert not broker_thread.is_alive()
    return output.getvalue()


def test_two_sequential_proxies_reuse_one_transport() -> None:
    transport = EchoTransport()
    broker = ForegroundBroker(transport, ("worker.internal", 3333), MagicMock())

    assert run_proxy(broker, b"first") == b"worker:first"
    assert run_proxy(broker, b"second") == b"worker:second"
    assert transport.destinations == [
        ("worker.internal", 3333),
        ("worker.internal", 3333),
    ]


def test_two_simultaneous_proxies_get_independent_channels() -> None:
    transport = EchoTransport()
    broker = ForegroundBroker(transport, ("worker.internal", 3333), MagicMock())
    barrier = threading.Barrier(3)
    outputs: dict[str, bytes] = {}

    def connect(name: str) -> None:
        barrier.wait()
        outputs[name] = run_proxy(broker, name.encode())

    threads = [threading.Thread(target=connect, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert outputs == {"a": b"worker:a", "b": b"worker:b"}
    assert transport.destinations == [
        ("worker.internal", 3333),
        ("worker.internal", 3333),
    ]


def test_proxy_does_not_leave_a_buffered_stdin_reader_at_shutdown() -> None:
    broker = ForegroundBroker(
        ClosingBannerTransport(), ("worker.internal", 3333), MagicMock()
    )
    client, server = socket.socketpair()
    broker_thread = threading.Thread(
        target=broker._serve_client,
        args=(server,),
    )
    broker_thread.start()
    read_descriptor, write_descriptor = os.pipe()
    stdin = FilenoOnlyStdin(read_descriptor)
    output = io.BytesIO()
    try:
        relay_proxy_stdio(ClientBackend(client), stdin, output)
        assert output.getvalue() == b"SSH-2.0-test-worker\r\n"
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
        broker_thread.join(timeout=1)


def test_authentication_transport_loss_is_actionable_and_opens_no_channel() -> None:
    transport = EchoTransport(active=False)
    broker = ForegroundBroker(transport, ("worker.internal", 3333), MagicMock())
    client, server = socket.socketpair()
    thread = threading.Thread(
        target=broker._serve_client,
        args=(server,),
    )
    thread.start()

    with pytest.raises(IPCError, match="restart the foreground broker"):
        relay_proxy_stdio(ClientBackend(client), io.BytesIO(), io.BytesIO())
    thread.join(timeout=1)
    assert transport.destinations == []


def test_worker_channel_open_failure_is_actionable() -> None:
    broker = ForegroundBroker(
        FailingTransport(), ("wrong-worker.internal", 3333), MagicMock()
    )
    client, server = socket.socketpair()
    thread = threading.Thread(
        target=broker._serve_client,
        args=(server,),
    )
    thread.start()

    with pytest.raises(IPCError, match="worker channel could not be opened"):
        relay_proxy_stdio(ClientBackend(client), io.BytesIO(), io.BytesIO())
    thread.join(timeout=1)


def test_proxy_sends_client_bytes_before_worker_sends_any_bytes() -> None:
    broker = ForegroundBroker(
        ClientFirstTransport(), ("worker.internal", 3333), MagicMock()
    )

    assert run_proxy(broker, b"client-identification") == (
        b"worker:client-identification"
    )


def test_loopback_broker_relays_server_banner_while_waiting_for_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ipc.config.local_file, "runtime_dir", tmp_path)
    server_backend = create_broker_backend(
        profile="test",
        authkey=b"a" * 32,
    )
    broker = ForegroundBroker(
        BannerTransport(), ("worker.internal", 3333), server_backend
    )
    broker_thread = threading.Thread(target=broker.serve_forever, daemon=True)
    broker_thread.start()
    output = io.BytesIO()

    relay_proxy_stdio(
        load_broker_backend(profile="test"),
        io.BytesIO(b"SSH-2.0-test-client\r\n"),
        output,
    )
    broker.close()
    broker_thread.join(timeout=1)

    assert output.getvalue() == (
        b"SSH-2.0-test-worker\r\nworker:SSH-2.0-test-client\r\n"
    )
    assert not broker_thread.is_alive()
