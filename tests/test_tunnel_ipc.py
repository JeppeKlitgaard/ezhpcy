import json
import os
import socket
import threading
import time
import uuid
from pathlib import Path

import pytest

import ezhpcy.tunnel.ipc as ipc
from ezhpcy.tunnel.ipc import (
    AuthenticatedIPCBackend,
    BrokerUnavailableError,
    FramedConnection,
    IPCAuthenticationError,
    accept_worker_stream,
    create_broker_backend,
    load_broker_backend,
    ready_worker_stream,
    request_worker_stream,
)


class SocketStream:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock

    def read(self, size: int) -> bytes:
        return self.sock.recv(size)

    def write(self, data: bytes) -> None:
        self.sock.sendall(data)

    def close(self) -> None:
        self.sock.close()


def connection_pair() -> tuple[FramedConnection, FramedConnection]:
    left, right = socket.socketpair()
    return FramedConnection(SocketStream(left)), FramedConnection(SocketStream(right))


def ipc_address(tmp_path: Path, label: str) -> tuple[str, str]:
    unique = f"{label}-{os.getpid()}-{uuid.uuid4().hex}"
    if os.name == "nt":
        return rf"\\.\pipe\ezhpcy-test-{unique}", "AF_PIPE"
    return str(tmp_path / f"{unique}.sock"), "AF_UNIX"


def test_versioned_handshake_accepts_current_version() -> None:
    client, server = connection_pair()
    accepted = threading.Event()

    def serve() -> None:
        accept_worker_stream(server)
        accepted.set()
        ready_worker_stream(server)

    thread = threading.Thread(target=serve)
    thread.start()
    request_worker_stream(client)

    assert accepted.wait(timeout=1)
    client.close()
    server.close()
    thread.join(timeout=1)


def test_version_mismatch_returns_bounded_actionable_error() -> None:
    client, server = connection_pair()
    result: list[Exception] = []

    def serve() -> None:
        try:
            accept_worker_stream(server)
        except Exception as error:
            result.append(error)

    thread = threading.Thread(target=serve)
    thread.start()
    client.send_frame(
        ipc._HELLO,
        json.dumps(
            {"version": ipc.PROTOCOL_VERSION + 1, "operation": "open-worker-stream"}
        ).encode(),
    )
    kind, payload = client.receive_frame()
    message = json.loads(payload)

    assert kind == ipc._ERROR
    assert "unsupported IPC protocol version" in message["message"]
    thread.join(timeout=1)
    assert result and isinstance(result[0], ipc.ProtocolError)
    client.close()
    server.close()


def test_authenticated_connection_uses_send_bytes_without_pickle(
    tmp_path: Path,
) -> None:
    address, family = ipc_address(tmp_path, "bytes")
    backend = AuthenticatedIPCBackend(address, family, b"a" * 32)
    listener = backend.listen()
    received: list[bytes] = []

    def serve() -> None:
        stream = listener.accept()
        received.append(stream.read(4))
        stream.write(b"pong")
        stream.close()

    thread = threading.Thread(target=serve)
    thread.start()
    client = backend.connect(timeout=1)
    client.write(b"ping")
    assert client.read(4) == b"pong"
    client.close()
    thread.join(timeout=1)
    listener.close()

    assert received == [b"ping"]


def test_wrong_authkey_is_rejected(tmp_path: Path) -> None:
    address, family = ipc_address(tmp_path, "auth")
    server = AuthenticatedIPCBackend(address, family, b"a" * 32)
    wrong_client = AuthenticatedIPCBackend(address, family, b"b" * 32)
    listener = server.listen()
    errors: list[Exception] = []

    def accept() -> None:
        try:
            listener.accept()
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=accept)
    thread.start()
    with pytest.raises(IPCAuthenticationError, match="authentication failed"):
        wrong_client.connect(timeout=1)
    thread.join(timeout=1)
    listener.close()

    assert errors and isinstance(errors[0], IPCAuthenticationError)


def test_runtime_descriptor_publishes_capability_and_is_removed(tmp_path: Path) -> None:
    address, _family = ipc_address(tmp_path, "runtime")
    descriptor = tmp_path / "broker.json"
    server = create_broker_backend(
        address,
        descriptor_path=descriptor,
        authkey=b"a" * 32,
    )
    listener = server.listen()
    client_backend = load_broker_backend(descriptor)

    assert descriptor.is_file()
    assert client_backend.address == address
    assert client_backend.authkey == b"a" * 32
    assert "authkey" not in repr(client_backend)

    received: list[bytes] = []

    def serve() -> None:
        stream = listener.accept()
        received.append(stream.read(4))
        stream.write(b"pong")
        stream.close()

    thread = threading.Thread(target=serve)
    thread.start()
    client = client_backend.connect(timeout=1)
    client.write(b"ping")
    assert client.read(4) == b"pong"
    client.close()
    thread.join(timeout=1)
    listener.close()

    assert received == [b"ping"]
    assert not descriptor.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_runtime_descriptor_is_owner_only_on_posix(tmp_path: Path) -> None:
    address, _family = ipc_address(tmp_path, "permissions")
    descriptor = tmp_path / "runtime" / "broker.json"
    backend = create_broker_backend(address, descriptor_path=descriptor)
    listener = backend.listen()
    try:
        assert descriptor.stat().st_mode & 0o077 == 0
        assert descriptor.parent.stat().st_mode & 0o077 == 0
        assert Path(address).stat().st_mode & 0o077 == 0
    finally:
        listener.close()


def test_missing_runtime_descriptor_fails_quickly(tmp_path: Path) -> None:
    with pytest.raises(BrokerUnavailableError, match="broker is not running"):
        load_broker_backend(tmp_path / "missing.json")


def test_ipc_endpoint_absence_fails_quickly(tmp_path: Path) -> None:
    address, family = ipc_address(tmp_path, "absent")
    backend = AuthenticatedIPCBackend(address, family, b"a" * 32)
    started = time.monotonic()
    with pytest.raises(BrokerUnavailableError, match="broker is not running"):
        backend.connect(timeout=0.05)
    assert time.monotonic() - started < 1


@pytest.mark.skipif(os.name != "nt", reason="Windows named-pipe shutdown behavior")
def test_listener_close_cancels_blocked_accept(tmp_path: Path) -> None:
    address, family = ipc_address(tmp_path, "close")
    listener = AuthenticatedIPCBackend(address, family, b"a" * 32).listen()
    finished = threading.Event()

    def accept() -> None:
        try:
            listener.accept()
        except Exception:
            pass
        finally:
            finished.set()

    thread = threading.Thread(target=accept)
    thread.start()
    time.sleep(0.05)
    listener.close()
    thread.join(timeout=1)

    assert finished.is_set()
    assert not thread.is_alive()
