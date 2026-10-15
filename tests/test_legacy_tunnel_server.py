import io
import socket
import threading
from unittest.mock import MagicMock

import pytest

from ezhpcy.ipc import IPCError
from ezhpcy.ipc._legacy_server import create_tunnel_backend
from ezhpcy.ipc.client import connect, relay_proxy_stdio
from ezhpcy.ipc.descriptor import TunnelDescriptor
from ezhpcy.tunnel._legacy_server import TunnelServer


class EchoTransport:
    def __init__(self, *, active: bool = True) -> None:
        self.active = active
        self.destinations: list[tuple[str, int]] = []
        self._lock = threading.Lock()

    def is_active(self) -> bool:
        return self.active

    def open_channel(self, _kind, *, dest_addr, src_addr):
        assert src_addr == ("ezhpcy-proxy", 0)
        tunnel_channel, worker = socket.socketpair()
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
        return tunnel_channel


class FailingTransport(EchoTransport):
    def open_channel(self, _kind, *, dest_addr, src_addr):
        raise OSError("channel rejected")


class ClientFirstTransport(EchoTransport):
    def open_channel(self, _kind, *, dest_addr, src_addr):
        tunnel_channel, worker = socket.socketpair()

        def respond_after_client() -> None:
            request = bytearray()
            while chunk := worker.recv(65536):
                request.extend(chunk)
            worker.sendall(b"worker:" + request)
            worker.shutdown(socket.SHUT_WR)
            worker.close()

        threading.Thread(target=respond_after_client, daemon=True).start()
        return tunnel_channel


class BannerTransport(EchoTransport):
    def open_channel(self, _kind, *, dest_addr, src_addr):
        tunnel_channel, worker = socket.socketpair()

        def serve_ssh_bytes() -> None:
            worker.sendall(b"SSH-2.0-test-worker\r\n")
            request = bytearray()
            while chunk := worker.recv(65536):
                request.extend(chunk)
            worker.sendall(b"worker:" + request)
            worker.shutdown(socket.SHUT_WR)
            worker.close()

        threading.Thread(target=serve_ssh_bytes, daemon=True).start()
        return tunnel_channel


def run_proxy(tunnel: TunnelServer, payload: bytes) -> bytes:
    client, server = socket.socketpair()
    tunnel_thread = threading.Thread(
        target=tunnel._serve_client,
        args=(server,),
    )
    tunnel_thread.start()
    output = io.BytesIO()
    with client:
        relay_proxy_stdio(client, io.BytesIO(payload), output)
    tunnel_thread.join(timeout=1)
    server.close()
    assert not tunnel_thread.is_alive()
    return output.getvalue()


def test_two_sequential_proxies_reuse_one_transport() -> None:
    transport = EchoTransport()
    tunnel = TunnelServer(transport, ("worker.internal", 3333), MagicMock())

    assert run_proxy(tunnel, b"first") == b"worker:first"
    assert run_proxy(tunnel, b"second") == b"worker:second"
    assert transport.destinations == [
        ("worker.internal", 3333),
        ("worker.internal", 3333),
    ]


def test_two_simultaneous_proxies_get_independent_channels() -> None:
    transport = EchoTransport()
    tunnel = TunnelServer(transport, ("worker.internal", 3333), MagicMock())
    barrier = threading.Barrier(3)
    outputs: dict[str, bytes] = {}

    def connect(name: str) -> None:
        barrier.wait()
        outputs[name] = run_proxy(tunnel, name.encode())

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


def test_authentication_transport_loss_is_actionable_and_opens_no_channel() -> None:
    transport = EchoTransport(active=False)
    tunnel = TunnelServer(transport, ("worker.internal", 3333), MagicMock())
    client, server = socket.socketpair()
    thread = threading.Thread(
        target=tunnel._serve_client,
        args=(server,),
    )
    thread.start()

    with client, pytest.raises(IPCError, match="restart the tunnel"):
        relay_proxy_stdio(client, io.BytesIO(), io.BytesIO())
    thread.join(timeout=1)
    server.close()
    assert transport.destinations == []


def test_worker_channel_open_failure_is_actionable() -> None:
    tunnel = TunnelServer(
        FailingTransport(), ("wrong-worker.internal", 3333), MagicMock()
    )
    client, server = socket.socketpair()
    thread = threading.Thread(
        target=tunnel._serve_client,
        args=(server,),
    )
    thread.start()

    with client, pytest.raises(IPCError, match="worker channel could not be opened"):
        relay_proxy_stdio(client, io.BytesIO(), io.BytesIO())
    thread.join(timeout=1)
    server.close()


def test_proxy_sends_client_bytes_before_worker_sends_any_bytes() -> None:
    tunnel = TunnelServer(
        ClientFirstTransport(), ("worker.internal", 3333), MagicMock()
    )

    assert run_proxy(tunnel, b"client-identification") == (
        b"worker:client-identification"
    )


def test_loopback_tunnel_relays_server_banner_while_waiting_for_client() -> None:
    server_backend = create_tunnel_backend(alias="test", authkey=b"a" * 32)
    tunnel = TunnelServer(BannerTransport(), ("worker.internal", 3333), server_backend)
    tunnel_thread = threading.Thread(target=tunnel.serve_forever, daemon=True)
    tunnel_thread.start()
    output = io.BytesIO()

    with connect(TunnelDescriptor.load("test")) as stream:
        relay_proxy_stdio(stream, io.BytesIO(b"SSH-2.0-test-client\r\n"), output)
    tunnel.close()
    tunnel_thread.join(timeout=1)

    assert output.getvalue() == (
        b"SSH-2.0-test-worker\r\nworker:SSH-2.0-test-client\r\n"
    )
    assert not tunnel_thread.is_alive()
