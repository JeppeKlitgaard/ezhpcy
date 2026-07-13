import base64
import json
import os
import socket
import threading
import time
from multiprocessing import Pipe
from pathlib import Path

import pytest

import ezhpcy.tunnel.ipc as ipc
from ezhpcy.tunnel.ipc import (
    AuthenticatedIPCBackend,
    BrokerUnavailableError,
    FramedConnection,
    IPCAddress,
    IPCAuthenticationError,
    accept_worker_stream,
    create_broker_backend,
    load_broker_backend,
    ready_worker_stream,
    request_worker_stream,
)

AUTHKEY = b"a" * 32
BIND_ADDRESS = IPCAddress("127.0.0.1", 0, allow_zero_port=True)


def connection_pair() -> tuple[FramedConnection, FramedConnection]:
    left, right = Pipe(duplex=True)
    return FramedConnection(left), FramedConnection(right)


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


def test_protocol_frame_uses_one_connection_message() -> None:
    sender, receiver = Pipe(duplex=True)
    connection = FramedConnection(sender)
    try:
        connection.send_data(b"payload")
        assert receiver.recv_bytes() == bytes((ipc._DATA,)) + b"payload"
    finally:
        connection.close()
        receiver.close()


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


def test_authenticated_connection_uses_send_bytes_without_pickle() -> None:
    server = AuthenticatedIPCBackend(BIND_ADDRESS, AUTHKEY)
    listener = server.listen()
    client_backend = AuthenticatedIPCBackend(listener.address, AUTHKEY)
    received: list[bytes] = []

    def serve() -> None:
        connection = listener.accept()
        received.append(connection.recv_bytes(4))
        connection.send_bytes(b"pong")
        connection.close()

    thread = threading.Thread(target=serve)
    thread.start()
    client = client_backend.connect(timeout=1)
    client.send_bytes(b"ping")
    assert client.recv_bytes(4) == b"pong"
    client.close()
    thread.join(timeout=1)
    listener.close()

    assert received == [b"ping"]


def test_ipc_address_constructor_validates_loopback_and_port() -> None:
    assert IPCAddress("127.0.0.1", 12345).as_tuple() == ("127.0.0.1", 12345)
    with pytest.raises(ValueError, match="IPv4 loopback"):
        IPCAddress("0.0.0.0", 12345)
    with pytest.raises(ValueError, match="port is invalid"):
        IPCAddress("127.0.0.1", 0)


def test_wrong_authkey_is_rejected() -> None:
    server = AuthenticatedIPCBackend(BIND_ADDRESS, AUTHKEY)
    listener = server.listen()
    wrong_client = AuthenticatedIPCBackend(listener.address, b"b" * 32)
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
    descriptor = tmp_path / "broker.json"
    server = create_broker_backend(
        descriptor_path=descriptor,
        authkey=AUTHKEY,
    )
    listener = server.listen()
    client_backend = load_broker_backend(descriptor)
    payload = json.loads(descriptor.read_text(encoding="utf-8"))

    assert descriptor.is_file()
    assert client_backend.address == listener.address
    assert client_backend.address.host == "127.0.0.1"
    assert client_backend.address.port != 0
    assert client_backend.authkey == AUTHKEY
    assert "authkey" not in repr(client_backend)
    assert payload["version"] == ipc.RUNTIME_DESCRIPTOR_VERSION
    assert payload["host"] == "127.0.0.1"
    assert payload["port"] == listener.address.port
    assert "family" not in payload
    assert "pid" not in payload

    received: list[bytes] = []

    def serve() -> None:
        connection = listener.accept()
        received.append(connection.recv_bytes(4))
        connection.send_bytes(b"pong")
        connection.close()

    thread = threading.Thread(target=serve)
    thread.start()
    client = client_backend.connect(timeout=1)
    client.send_bytes(b"ping")
    assert client.recv_bytes(4) == b"pong"
    client.close()
    thread.join(timeout=1)
    listener.close()

    assert received == [b"ping"]
    assert not descriptor.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_runtime_descriptor_is_owner_only_on_posix(tmp_path: Path) -> None:
    descriptor = tmp_path / "runtime" / "broker.json"
    backend = create_broker_backend(descriptor_path=descriptor)
    listener = backend.listen()
    try:
        assert descriptor.stat().st_mode & 0o077 == 0
        assert descriptor.parent.stat().st_mode & 0o077 == 0
    finally:
        listener.close()


def test_missing_runtime_descriptor_fails_quickly(tmp_path: Path) -> None:
    with pytest.raises(BrokerUnavailableError, match="broker is not running"):
        load_broker_backend(tmp_path / "missing.json")


def test_ipc_endpoint_absence_fails_quickly() -> None:
    with socket.socket() as reserved:
        reserved.bind(BIND_ADDRESS.as_tuple())
        address = IPCAddress(*reserved.getsockname())
    backend = AuthenticatedIPCBackend(address, AUTHKEY)
    started = time.monotonic()
    with pytest.raises(BrokerUnavailableError, match="broker is not running"):
        backend.connect(timeout=0.05)
    assert time.monotonic() - started < 1


def test_listener_close_cancels_blocked_accept() -> None:
    listener = AuthenticatedIPCBackend(BIND_ADDRESS, AUTHKEY).listen()
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


def test_runtime_descriptor_rejects_non_loopback_address(tmp_path: Path) -> None:
    descriptor = tmp_path / "broker.json"
    descriptor.write_text(
        json.dumps(
            {
                "version": ipc.RUNTIME_DESCRIPTOR_VERSION,
                "host": "0.0.0.0",
                "port": 12345,
                "authkey": base64.b64encode(AUTHKEY).decode("ascii"),
                "instance_id": "test-instance",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(BrokerUnavailableError, match="runtime information is invalid"):
        load_broker_backend(descriptor)


def test_latest_broker_descriptor_wins_and_old_close_preserves_it(
    tmp_path: Path,
) -> None:
    descriptor = tmp_path / "broker.json"
    first = create_broker_backend(
        descriptor_path=descriptor,
        authkey=b"a" * 32,
    )
    second = create_broker_backend(
        descriptor_path=descriptor,
        authkey=b"b" * 32,
    )
    first_listener = first.listen()
    second_listener = second.listen()
    try:
        current = load_broker_backend(descriptor)
        assert current.address == second_listener.address
        assert current.authkey == b"b" * 32

        first_listener.close()
        assert descriptor.is_file()
        assert load_broker_backend(descriptor).instance_id == second.instance_id
    finally:
        first_listener.close()
        second_listener.close()

    assert not descriptor.exists()
