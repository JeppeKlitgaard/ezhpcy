"""Authenticated, versioned IPC for the foreground tunnel broker."""

from __future__ import annotations

import base64
import json
import os
import secrets
import socket
import threading
import uuid
from dataclasses import InitVar, dataclass, field
from multiprocessing.connection import (
    AuthenticationError,
    Connection,
    Listener,
    answer_challenge,
    deliver_challenge,
)
from pathlib import Path
from typing import Protocol

from ezhpcy.config import config

PROTOCOL_VERSION = 1
RUNTIME_DESCRIPTOR_VERSION = 1
MAX_CONTROL_SIZE = 4096
MAX_DATA_SIZE = 1024 * 1024
IPC_BACKLOG = 32

_LOOPBACK_HOST = "127.0.0.1"


@dataclass(frozen=True)
class IPCAddress:
    host: str
    port: int
    allow_zero_port: InitVar[bool] = False

    def __post_init__(self, allow_zero_port: bool) -> None:
        if self.host != _LOOPBACK_HOST:
            raise ValueError("broker IPC address must use IPv4 loopback")
        minimum_port = 0 if allow_zero_port else 1
        if type(self.port) is not int or not minimum_port <= self.port <= 65535:
            raise ValueError("broker IPC port is invalid")

    def as_tuple(self) -> tuple[str, int]:
        return self.host, self.port


_DEFAULT_BIND_ADDRESS = IPCAddress(_LOOPBACK_HOST, 0, allow_zero_port=True)

_MAX_WIRE_SIZE = 1 + MAX_DATA_SIZE
_HELLO = 1
_READY = 2
_ERROR = 3
_DATA = 4
_EOF = 5


class IPCError(Exception):
    """Base class for actionable broker IPC failures."""


class BrokerUnavailableError(IPCError):
    """The foreground broker is not accepting connections."""


class IPCAuthenticationError(IPCError):
    """The broker and proxy do not share the same capability key."""


class ProtocolError(IPCError):
    """The peer sent an invalid or unsupported IPC message."""


class MessageConnection(Protocol):
    def recv_bytes(self, maxlength: int | None = None) -> bytes: ...

    def send_bytes(self, data: bytes) -> None: ...

    def close(self) -> None: ...


class IPCListener(Protocol):
    address: IPCAddress

    def accept(self) -> MessageConnection: ...

    def close(self) -> None: ...


class IPCBackend(Protocol):
    def listen(self) -> IPCListener: ...

    def connect(self, *, timeout: float) -> MessageConnection: ...


class FramedConnection:
    """Send typed protocol messages over a full-duplex IPC connection."""

    def __init__(self, connection: MessageConnection) -> None:
        self.connection = connection
        self._write_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._closed = False

    def receive_frame(self) -> tuple[int, bytes]:
        try:
            message = self.connection.recv_bytes(_MAX_WIRE_SIZE)
        except EOFError as error:
            raise EOFError("broker IPC connection closed") from error
        except OSError as error:
            raise ProtocolError(
                "broker IPC message was invalid or exceeded the size limit"
            ) from error
        if not message:
            raise ProtocolError("broker IPC contained an empty message")
        kind, payload = message[0], message[1:]
        limit = MAX_DATA_SIZE if kind == _DATA else MAX_CONTROL_SIZE
        if len(payload) > limit:
            raise ProtocolError(f"IPC frame exceeds the {limit}-byte limit")
        return kind, payload

    def send_frame(self, kind: int, payload: bytes = b"") -> None:
        limit = MAX_DATA_SIZE if kind == _DATA else MAX_CONTROL_SIZE
        if len(payload) > limit:
            raise ProtocolError(f"IPC frame exceeds the {limit}-byte limit")
        with self._write_lock:
            self.connection.send_bytes(bytes((kind,)) + payload)

    def send_data(self, data: bytes) -> None:
        self.send_frame(_DATA, data)

    def send_eof(self) -> None:
        self.send_frame(_EOF)

    def receive_data(self) -> bytes | None:
        kind, payload = self.receive_frame()
        if kind == _DATA:
            return payload
        if kind == _EOF:
            return None
        if kind == _ERROR:
            message = _decode_control(payload).get("message", "broker rejected stream")
            raise IPCError(str(message))
        raise ProtocolError(f"unexpected IPC frame type {kind}")

    def close(self) -> None:
        with self._close_lock:
            if not self._closed:
                self._closed = True
                self.connection.close()


def _encode_control(message: dict[str, object]) -> bytes:
    return json.dumps(message, separators=(",", ":")).encode("utf-8")


def _decode_control(payload: bytes) -> dict[str, object]:
    try:
        message = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtocolError("invalid IPC control message") from error
    if not isinstance(message, dict):
        raise ProtocolError("IPC control message must be an object")
    return message


def request_worker_stream(connection: FramedConnection) -> None:
    connection.send_frame(
        _HELLO,
        _encode_control(
            {"version": PROTOCOL_VERSION, "operation": "open-worker-stream"}
        ),
    )
    kind, payload = connection.receive_frame()
    message = _decode_control(payload)
    if kind == _ERROR:
        raise IPCError(str(message.get("message", "broker rejected stream")))
    if kind != _READY or message.get("version") != PROTOCOL_VERSION:
        raise ProtocolError("broker returned an invalid readiness response")


def accept_worker_stream(connection: FramedConnection) -> None:
    kind, payload = connection.receive_frame()
    if kind != _HELLO:
        reject_worker_stream(connection, "expected worker-stream request")
        raise ProtocolError("expected worker-stream request")
    message = _decode_control(payload)
    if message.get("version") != PROTOCOL_VERSION:
        reject_worker_stream(
            connection,
            f"unsupported IPC protocol version; expected {PROTOCOL_VERSION}",
        )
        raise ProtocolError("unsupported IPC protocol version")
    if message.get("operation") != "open-worker-stream":
        reject_worker_stream(connection, "unsupported IPC operation")
        raise ProtocolError("unsupported IPC operation")


def ready_worker_stream(connection: FramedConnection) -> None:
    connection.send_frame(_READY, _encode_control({"version": PROTOCOL_VERSION}))


def reject_worker_stream(connection: FramedConnection, message: str) -> None:
    connection.send_frame(
        _ERROR,
        _encode_control({"version": PROTOCOL_VERSION, "message": message[:1024]}),
    )


@dataclass(frozen=True)
class AuthenticatedIPCBackend:
    """An authenticated TCP endpoint bound exclusively to IPv4 loopback."""

    address: IPCAddress
    authkey: bytes = field(repr=False)
    descriptor_path: Path | None = None
    instance_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    publish_descriptor: bool = False

    def __post_init__(self) -> None:
        if len(self.authkey) < 32:
            raise ValueError("broker authkey must contain at least 32 random bytes")

    def listen(self) -> IPCListener:
        try:
            listener = Listener(
                self.address.as_tuple(),
                family="AF_INET",
                backlog=IPC_BACKLOG,
                authkey=self.authkey,
            )
        except OSError as error:
            raise IPCError("could not create the broker IPC listener") from error

        address = IPCAddress(*listener.address)
        wrapped = _AuthenticatedListener(listener, self, address)
        try:
            if self.publish_descriptor:
                _publish_runtime_descriptor(self, address)
        except BaseException:
            wrapped.close()
            raise
        return wrapped

    def connect(self, *, timeout: float = 2.0) -> MessageConnection:
        connection = None
        try:
            with socket.create_connection(
                self.address.as_tuple(), timeout=max(0.0, timeout)
            ) as client_socket:
                client_socket.settimeout(None)
                connection = Connection(client_socket.detach())
            answer_challenge(connection, self.authkey)
            deliver_challenge(connection, self.authkey)
        except AuthenticationError as error:
            if connection is not None:
                connection.close()
            raise IPCAuthenticationError(
                "broker authentication failed; restart the foreground broker"
            ) from error
        except OSError as error:
            if connection is not None:
                connection.close()
            raise BrokerUnavailableError(
                "foreground broker is not running; start `ezhpcy tunnel broker`"
            ) from error
        return connection


class _AuthenticatedListener:
    def __init__(
        self,
        listener: Listener,
        backend: AuthenticatedIPCBackend,
        address: IPCAddress,
    ) -> None:
        self.listener = listener
        self.backend = backend
        self.address = address
        self._closed = False
        self._lock = threading.Lock()

    def accept(self) -> MessageConnection:
        try:
            return self.listener.accept()
        except AuthenticationError as error:
            raise IPCAuthenticationError(
                "rejected a local client with an invalid broker authkey"
            ) from error

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            # Listener.close() does not reliably wake an accept blocked in
            # another thread. A connection without an authkey returns
            # immediately, then closes; the server-side HMAC handshake rejects
            # it and releases the broker accept loop.
            try:
                with socket.create_connection(self.address.as_tuple(), timeout=0.1):
                    pass
            except OSError:
                pass
            self.listener.close()
            if self.backend.publish_descriptor:
                _remove_runtime_descriptor(self.backend)


def default_descriptor_path() -> Path:
    return config.local_file.runtime_dir / "broker.json"


def create_broker_backend(
    *,
    address: IPCAddress = _DEFAULT_BIND_ADDRESS,
    descriptor_path: Path | None = None,
    authkey: bytes | None = None,
) -> AuthenticatedIPCBackend:
    """Create the server backend and its per-run authentication capability."""
    descriptor = descriptor_path or default_descriptor_path()
    return AuthenticatedIPCBackend(
        address=address,
        authkey=authkey or secrets.token_bytes(32),
        descriptor_path=descriptor,
        publish_descriptor=True,
    )


def load_broker_backend(
    descriptor_path: Path | None = None,
) -> AuthenticatedIPCBackend:
    """Load the broker endpoint and capability without exposing either in argv."""
    path = descriptor_path or default_descriptor_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise BrokerUnavailableError(
            "foreground broker is not running; start `ezhpcy tunnel broker`"
        ) from error
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BrokerUnavailableError(
            "broker runtime information is invalid; restart the foreground broker"
        ) from error

    try:
        version = payload["version"]
        host = payload["host"]
        port = payload["port"]
        encoded_authkey = payload["authkey"]
        instance_id = payload["instance_id"]
        if version != RUNTIME_DESCRIPTOR_VERSION:
            raise ValueError("unsupported runtime descriptor version")
        if not all(
            isinstance(value, str) for value in (host, encoded_authkey, instance_id)
        ):
            raise ValueError("runtime descriptor values have invalid types")
        address = IPCAddress(host, port)
        authkey = base64.b64decode(encoded_authkey, validate=True)
    except (KeyError, TypeError, ValueError) as error:
        raise BrokerUnavailableError(
            "broker runtime information is invalid; restart the foreground broker"
        ) from error

    try:
        return AuthenticatedIPCBackend(
            address=address,
            authkey=authkey,
            descriptor_path=path,
            instance_id=instance_id,
        )
    except ValueError as error:
        raise BrokerUnavailableError(
            "broker runtime information is invalid; restart the foreground broker"
        ) from error


def _publish_runtime_descriptor(
    backend: AuthenticatedIPCBackend, address: IPCAddress
) -> None:
    path = backend.descriptor_path
    if path is None:
        raise IPCError("broker runtime descriptor path is not configured")
    directory = path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    payload = {
        "version": RUNTIME_DESCRIPTOR_VERSION,
        "host": address.host,
        "port": address.port,
        "authkey": base64.b64encode(backend.authkey).decode("ascii"),
        "instance_id": backend.instance_id,
    }
    temporary = directory / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(payload, file, separators=(",", ":"))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _remove_runtime_descriptor(backend: AuthenticatedIPCBackend) -> None:
    path = backend.descriptor_path
    if path is None:
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("instance_id") == backend.instance_id:
            path.unlink()
    except FileNotFoundError, OSError, json.JSONDecodeError:
        pass
