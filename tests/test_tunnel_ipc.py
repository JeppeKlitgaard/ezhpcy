import base64
import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

import ezhpcy.ipc.protocol as protocol
import ezhpcy.ipc.runtime as runtime
from ezhpcy import ipc
from ezhpcy.ipc import (
    AuthenticatedIPCBackend,
    create_broker_backend,
    load_broker_backend,
)
from ezhpcy.ipc.common import (
    BrokerUnavailableError,
    IPCAddress,
    IPCAuthenticationError,
    IPCError,
)
from ezhpcy.types import ResolvedConfig

AUTHKEY = b"a" * 32
BIND_ADDRESS = IPCAddress("127.0.0.1", 0, allow_zero_port=True)


def test_descriptor_path_is_namespaced_by_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ipc.config.local_file, "runtime_dir", tmp_path)

    assert ipc._get_descriptor_path("gpu") == (
        tmp_path / "profile-descriptors" / "gpu.json"
    )


def test_resolved_config_descriptor_path_uses_digest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ipc.config.local_file, "runtime_dir", tmp_path)
    configuration = ResolvedConfig.model_validate(
        {
            "user": "alice",
            "host": "login.example.com",
            "scheduler": "LSF",
            "queue": "gpu",
            "cores": 8,
            "lsf_submission_environment": {"Z": "last", "A": "first"},
            "password": "secret",
        }
    )
    expected_digest = configuration.descriptor_digest()

    assert (
        ipc._get_descriptor_path(resolved_config=configuration)
        == tmp_path / "anonymous-descriptors" / f"{expected_digest}.json"
    )


def test_anonymous_descriptor_identity_is_order_independent() -> None:
    first = ResolvedConfig.model_validate(
        {
            "user": "alice",
            "host": "login.example.com",
            "lsf_submission_environment": {"A": "first", "Z": "last"},
        }
    )
    second = ResolvedConfig.model_validate(
        {
            "host": "login.example.com",
            "user": "alice",
            "lsf_submission_environment": {"Z": "last", "A": "first"},
        }
    )

    assert ipc._get_descriptor_path(resolved_config=first) == ipc._get_descriptor_path(
        resolved_config=second
    )


def test_anonymous_descriptor_identity_excludes_password_sources() -> None:
    password = ResolvedConfig.model_validate(
        {
            "user": "alice",
            "host": "login.example.com",
            "password": "secret",
        }
    )
    password_file = ResolvedConfig.model_validate(
        {
            "user": "alice",
            "host": "login.example.com",
            "password_file": "password.txt",
        }
    )

    assert ipc._get_descriptor_path(
        resolved_config=password
    ) == ipc._get_descriptor_path(resolved_config=password_file)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("user", "bob"),
        ("host", "other.example.com"),
        ("queue", "cpu"),
        ("cores", 16),
        ("interactive_submission_command", ["/site/bin/interactive"]),
    ],
)
def test_anonymous_descriptor_identity_includes_job_configuration(
    field: str, value: object
) -> None:
    base_values: dict[str, object] = {
        "user": "alice",
        "host": "login.example.com",
        "queue": "gpu",
        "cores": 8,
    }
    changed_values = {**base_values, field: value}

    assert ipc._get_descriptor_path(
        resolved_config=ResolvedConfig.model_validate(base_values)
    ) != ipc._get_descriptor_path(
        resolved_config=ResolvedConfig.model_validate(changed_values)
    )


def test_descriptor_path_requires_exactly_one_identity() -> None:
    configuration = ResolvedConfig.model_validate(
        {"user": "alice", "host": "login.example.com"}
    )

    with pytest.raises(ValueError, match="resolved configuration is required"):
        ipc._get_descriptor_path()
    with pytest.raises(ValueError, match="mutually exclusive"):
        ipc._get_descriptor_path("gpu", resolved_config=configuration)


def test_anonymous_backend_publishes_and_loads_by_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ipc.config.local_file, "runtime_dir", tmp_path)
    configuration = ResolvedConfig.model_validate(
        {
            "user": "alice",
            "host": "login.example.com",
            "queue": "gpu",
        }
    )
    backend = create_broker_backend(
        resolved_config=configuration,
        authkey=AUTHKEY,
    )
    listener = backend.listen(lambda _connection: None)
    descriptor = ipc._get_descriptor_path(resolved_config=configuration)
    try:
        loaded = load_broker_backend(resolved_config=configuration)

        assert descriptor.is_file()
        assert loaded.address == listener.address
        assert loaded.authkey == AUTHKEY
    finally:
        listener.close()

    assert not descriptor.exists()


def start_server(backend, handler, error_handler=None):
    server = backend.listen(handler, error_handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    return server, thread


def test_worker_stream_rejection_is_bounded_and_actionable() -> None:
    client, server = socket.socketpair()
    thread = threading.Thread(
        target=protocol.reject_worker_stream,
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
    server_backend = AuthenticatedIPCBackend(BIND_ADDRESS, AUTHKEY)
    errors: list[Exception] = []
    rejected = threading.Event()

    def report(error: Exception) -> None:
        errors.append(error)
        rejected.set()

    server, thread = start_server(server_backend, lambda _connection: None, report)
    wrong_client = AuthenticatedIPCBackend(server.address, b"b" * 32)
    with pytest.raises(IPCAuthenticationError, match="authentication failed"):
        wrong_client.connect(timeout=1)
    assert rejected.wait(timeout=1)
    server.close()
    thread.join(timeout=1)

    assert errors and isinstance(errors[0], IPCAuthenticationError)


def test_runtime_descriptor_publishes_capability_and_is_removed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ipc.config.local_file, "runtime_dir", tmp_path)
    descriptor = ipc._get_descriptor_path("test")
    server = create_broker_backend(
        profile="test",
        authkey=AUTHKEY,
    )
    received: list[bytes] = []

    def serve(connection) -> None:
        received.append(connection.recv(4))
        connection.sendall(b"pong")

    listener, thread = start_server(server, serve)
    client_backend = load_broker_backend(profile="test")
    payload = json.loads(descriptor.read_text(encoding="utf-8"))

    assert descriptor.is_file()
    assert client_backend.address == listener.address
    assert client_backend.address.host == "127.0.0.1"
    assert client_backend.address.port != 0
    assert client_backend.authkey == AUTHKEY
    assert "authkey" not in repr(client_backend)
    assert payload["version"] == runtime.RUNTIME_DESCRIPTOR_VERSION
    assert payload["host"] == "127.0.0.1"
    assert payload["port"] == listener.address.port

    client = client_backend.connect(timeout=1)
    client.sendall(b"ping")
    assert client.recv(4) == b"pong"
    client.close()
    listener.close()
    thread.join(timeout=1)

    assert received == [b"ping"]
    assert not descriptor.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_runtime_descriptor_is_owner_only_on_posix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ipc.config.local_file, "runtime_dir", tmp_path)
    descriptor = ipc._get_descriptor_path("test")
    backend = create_broker_backend(profile="test")
    listener = backend.listen(lambda _connection: None)
    try:
        assert descriptor.stat().st_mode & 0o077 == 0
        assert descriptor.parent.stat().st_mode & 0o077 == 0
    finally:
        listener.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and symlink semantics")
def test_runtime_descriptor_rejects_symlinked_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real_directory = tmp_path / "real-runtime"
    real_directory.mkdir(mode=0o700)
    linked_directory = tmp_path / "linked-runtime"
    linked_directory.symlink_to(real_directory, target_is_directory=True)
    monkeypatch.setattr(ipc.config.local_file, "runtime_dir", linked_directory)
    backend = create_broker_backend(profile="test")

    with pytest.raises(IPCError, match="must not be a symbolic link"):
        backend.listen(lambda _connection: None)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file type semantics")
def test_runtime_descriptor_rejects_non_directory_runtime_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime_path = tmp_path / "runtime"
    runtime_path.touch()
    monkeypatch.setattr(ipc.config.local_file, "runtime_dir", runtime_path)
    backend = create_broker_backend(profile="test")

    with pytest.raises(IPCError, match="is not a directory"):
        backend.listen(lambda _connection: None)


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership semantics")
def test_runtime_descriptor_rejects_directory_owned_by_another_user(
    tmp_path: Path, monkeypatch
) -> None:
    runtime_directory = tmp_path / "runtime"
    runtime_directory.mkdir(mode=0o700)
    monkeypatch.setattr(
        runtime.os, "getuid", lambda: runtime_directory.stat().st_uid + 1
    )
    monkeypatch.setattr(ipc.config.local_file, "runtime_dir", runtime_directory)
    backend = create_broker_backend(profile="test")

    with pytest.raises(IPCError, match="not owned by the current user"):
        backend.listen(lambda _connection: None)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_runtime_descriptor_restricts_precreated_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime_directory = tmp_path / "runtime"
    runtime_directory.mkdir(mode=0o777)
    runtime_directory.chmod(0o777)
    monkeypatch.setattr(ipc.config.local_file, "runtime_dir", runtime_directory)
    backend = create_broker_backend(profile="test")
    listener = backend.listen(lambda _connection: None)
    try:
        assert runtime_directory.stat().st_mode & 0o777 == 0o700
    finally:
        listener.close()


def test_missing_runtime_descriptor_fails_quickly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ipc.config.local_file, "runtime_dir", tmp_path)
    with pytest.raises(BrokerUnavailableError, match="broker is not running"):
        load_broker_backend(profile="missing")


def test_broker_backends_do_not_accept_a_descriptor_path(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="descriptor_path"):
        create_broker_backend(descriptor_path=tmp_path / "broker.json")
    with pytest.raises(TypeError, match="descriptor_path"):
        load_broker_backend(descriptor_path=tmp_path / "broker.json")


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
    backend = AuthenticatedIPCBackend(BIND_ADDRESS, AUTHKEY)
    server, thread = start_server(backend, lambda _connection: None)
    server.close()
    thread.join(timeout=1)

    assert not thread.is_alive()


def test_listener_closed_before_serving_returns_immediately() -> None:
    backend = AuthenticatedIPCBackend(BIND_ADDRESS, AUTHKEY)
    server = backend.listen(lambda _connection: None)
    server.close()
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    thread.join(timeout=1)

    assert not thread.is_alive()


def test_stalled_authentication_does_not_block_valid_client() -> None:
    errors: list[Exception] = []
    rejected = threading.Event()
    backend = AuthenticatedIPCBackend(BIND_ADDRESS, AUTHKEY, authentication_timeout=0.1)

    def serve(connection) -> None:
        assert connection.recv(4) == b"ping"
        connection.sendall(b"pong")

    def report(error: Exception) -> None:
        errors.append(error)
        rejected.set()

    server, thread = start_server(backend, serve, report)
    with socket.create_connection(server.address.as_tuple()) as _stalled:
        client = AuthenticatedIPCBackend(server.address, AUTHKEY).connect(timeout=1)
        client.sendall(b"ping")
        assert client.recv(4) == b"pong"
        client.close()
        assert rejected.wait(timeout=1)

    server.close()
    thread.join(timeout=1)
    assert errors and "timed out" in str(errors[0])


def test_client_authentication_has_an_overall_deadline() -> None:
    with socket.socket() as listener:
        listener.bind(BIND_ADDRESS.as_tuple())
        listener.listen()
        address = IPCAddress(*listener.getsockname())
        release = threading.Event()

        def stall() -> None:
            connection, _address = listener.accept()
            with connection:
                release.wait(timeout=1)

        thread = threading.Thread(target=stall)
        thread.start()
        backend = AuthenticatedIPCBackend(address, AUTHKEY)
        started = time.monotonic()
        with pytest.raises(IPCAuthenticationError, match="authentication failed"):
            backend.connect(timeout=0.05)
        elapsed = time.monotonic() - started
        release.set()
        thread.join(timeout=1)

    assert elapsed < 1
    assert not thread.is_alive()


def test_client_rejects_invalid_server_proof() -> None:
    with socket.socket() as listener:
        listener.bind(BIND_ADDRESS.as_tuple())
        listener.listen()
        address = IPCAddress(*listener.getsockname())

        def impersonate_broker() -> None:
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

        thread = threading.Thread(target=impersonate_broker)
        thread.start()
        backend = AuthenticatedIPCBackend(address, AUTHKEY)
        with pytest.raises(IPCAuthenticationError, match="authentication failed"):
            backend.connect(timeout=1)
        thread.join(timeout=1)

    assert not thread.is_alive()


def test_server_close_interrupts_stalled_authentication() -> None:
    backend = AuthenticatedIPCBackend(BIND_ADDRESS, AUTHKEY, authentication_timeout=10)
    server, thread = start_server(backend, lambda _connection: None)
    with socket.create_connection(server.address.as_tuple()) as stalled:
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

    def fail(_connection) -> None:
        raise RuntimeError("handler failed")

    def report(error: Exception) -> None:
        errors.append(error)
        reported.set()

    backend = AuthenticatedIPCBackend(BIND_ADDRESS, AUTHKEY)
    server, thread = start_server(backend, fail, report)
    client = AuthenticatedIPCBackend(server.address, AUTHKEY).connect(timeout=1)
    assert reported.wait(timeout=1)
    client.close()
    server.close()
    thread.join(timeout=1)

    assert errors and isinstance(errors[0], RuntimeError)
    assert not thread.is_alive()


def test_runtime_descriptor_rejects_non_loopback_address(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ipc.config.local_file, "runtime_dir", tmp_path)
    descriptor = ipc._get_descriptor_path("test")
    descriptor.parent.mkdir(parents=True)
    descriptor.write_text(
        json.dumps(
            {
                "version": runtime.RUNTIME_DESCRIPTOR_VERSION,
                "host": "0.0.0.0",
                "port": 12345,
                "authkey": base64.b64encode(AUTHKEY).decode("ascii"),
                "instance_id": "test-instance",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(BrokerUnavailableError, match="runtime information is invalid"):
        load_broker_backend(profile="test")


def test_latest_broker_descriptor_wins_and_old_close_preserves_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ipc.config.local_file, "runtime_dir", tmp_path)
    descriptor = ipc._get_descriptor_path("test")
    first = create_broker_backend(
        profile="test",
        authkey=b"a" * 32,
    )
    second = create_broker_backend(
        profile="test",
        authkey=b"b" * 32,
    )
    first_listener = first.listen(lambda _connection: None)
    second_listener = second.listen(lambda _connection: None)
    try:
        current = load_broker_backend(profile="test")
        assert current.address == second_listener.address
        assert current.authkey == b"b" * 32

        first_listener.close()
        assert descriptor.is_file()
        assert load_broker_backend(profile="test").instance_id == second.instance_id
    finally:
        first_listener.close()
        second_listener.close()

    assert not descriptor.exists()
