"""Authenticated, versioned IPC for the foreground tunnel broker."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import struct
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from multiprocessing.connection import (
    AuthenticationError,
    Client,
    Connection,
    Listener,
)
from pathlib import Path
from typing import Protocol

PROTOCOL_VERSION = 1
RUNTIME_DESCRIPTOR_VERSION = 1
MAX_CONTROL_SIZE = 4096
MAX_DATA_SIZE = 1024 * 1024

_FRAME_HEADER = struct.Struct("!BI")
_MAX_WIRE_SIZE = _FRAME_HEADER.size + MAX_DATA_SIZE
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


class RawIPCStream(Protocol):
    def read(self, size: int) -> bytes: ...

    def write(self, data: bytes) -> None: ...

    def close(self) -> None: ...


class IPCListener(Protocol):
    def accept(self) -> RawIPCStream: ...

    def close(self) -> None: ...


class IPCBackend(Protocol):
    def listen(self) -> IPCListener: ...

    def connect(self, *, timeout: float) -> RawIPCStream: ...


class FramedConnection:
    """Frames control messages and byte chunks over a full-duplex IPC stream."""

    def __init__(self, stream: RawIPCStream) -> None:
        self.stream = stream
        self._write_lock = threading.Lock()

    def _read_exact(self, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            chunk = self.stream.read(size - len(chunks))
            if not chunk:
                raise EOFError("broker IPC connection closed")
            chunks.extend(chunk)
        return bytes(chunks)

    def receive_frame(self) -> tuple[int, bytes]:
        kind, size = _FRAME_HEADER.unpack(self._read_exact(_FRAME_HEADER.size))
        limit = MAX_DATA_SIZE if kind == _DATA else MAX_CONTROL_SIZE
        if size > limit:
            raise ProtocolError(f"IPC frame exceeds the {limit}-byte limit")
        return kind, self._read_exact(size) if size else b""

    def send_frame(self, kind: int, payload: bytes = b"") -> None:
        limit = MAX_DATA_SIZE if kind == _DATA else MAX_CONTROL_SIZE
        if len(payload) > limit:
            raise ProtocolError(f"IPC frame exceeds the {limit}-byte limit")
        with self._write_lock:
            self.stream.write(_FRAME_HEADER.pack(kind, len(payload)) + payload)

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
        self.stream.close()


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


class _ConnectionStream:
    """Expose message-oriented stdlib connections as the existing byte stream API."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self._read_buffer = bytearray()
        self._closed = False
        self._close_lock = threading.Lock()

    def read(self, size: int) -> bytes:
        if size <= 0:
            return b""
        if not self._read_buffer:
            try:
                message = self.connection.recv_bytes(_MAX_WIRE_SIZE)
            except EOFError:
                return b""
            except OSError as error:
                raise ProtocolError(
                    "broker IPC message was invalid or exceeded the size limit"
                ) from error
            if not message:
                raise ProtocolError("broker IPC contained an empty message")
            self._read_buffer.extend(message)
        chunk = bytes(self._read_buffer[:size])
        del self._read_buffer[:size]
        return chunk

    def write(self, data: bytes) -> None:
        if not data:
            raise ProtocolError("broker IPC cannot send an empty message")
        self.connection.send_bytes(data)

    def close(self) -> None:
        with self._close_lock:
            if not self._closed:
                self._closed = True
                self.connection.close()


@dataclass(frozen=True)
class AuthenticatedIPCBackend:
    """A named pipe or Unix socket authenticated by a per-broker capability."""

    address: str
    family: str
    authkey: bytes = field(repr=False)
    descriptor_path: Path | None = None
    instance_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    publish_descriptor: bool = False

    def __post_init__(self) -> None:
        if len(self.authkey) < 32:
            raise ValueError("broker authkey must contain at least 32 random bytes")
        if self.family not in {"AF_PIPE", "AF_UNIX"}:
            raise ValueError(f"unsupported local IPC family {self.family!r}")

    def listen(self) -> IPCListener:
        try:
            listener = Listener(
                self.address,
                family=self.family,
                authkey=self.authkey,
            )
        except OSError as error:
            raise IPCError(
                "could not create the broker IPC endpoint; another broker may "
                "already be running"
            ) from error

        wrapped = _AuthenticatedListener(listener, self)
        try:
            if self.family == "AF_UNIX":
                os.chmod(self.address, 0o600)
            if self.publish_descriptor:
                _publish_runtime_descriptor(self)
        except BaseException:
            wrapped.close()
            raise
        return wrapped

    def connect(self, *, timeout: float = 2.0) -> RawIPCStream:
        if self.family == "AF_PIPE":
            _wait_for_windows_pipe(self.address, timeout)
        try:
            connection = Client(
                self.address,
                family=self.family,
                authkey=self.authkey,
            )
        except AuthenticationError as error:
            raise IPCAuthenticationError(
                "broker authentication failed; restart the foreground broker"
            ) from error
        except OSError as error:
            raise BrokerUnavailableError(
                "foreground broker is not running; start `ezhpcy tunnel broker`"
            ) from error
        return _ConnectionStream(connection)


class _AuthenticatedListener:
    def __init__(self, listener: Listener, backend: AuthenticatedIPCBackend) -> None:
        self.listener = listener
        self.backend = backend
        self._closed = False
        self._lock = threading.Lock()

    def accept(self) -> RawIPCStream:
        try:
            return _ConnectionStream(self.listener.accept())
        except AuthenticationError as error:
            raise IPCAuthenticationError(
                "rejected a local client with an invalid broker authkey"
            ) from error

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            # On Windows, Listener.close() does not wake a thread that is
            # already blocked in PipeListener.accept(). A connection without
            # an authkey returns immediately, then closes; the server-side HMAC
            # handshake rejects it and releases the accept loop so Ctrl+C can
            # join the broker thread cleanly.
            try:
                wakeup = Client(
                    self.backend.address,
                    family=self.backend.family,
                    authkey=None,
                )
                wakeup.close()
            except OSError:
                pass
            self.listener.close()
            if self.backend.publish_descriptor:
                _remove_runtime_descriptor(self.backend)
            if self.backend.family == "AF_UNIX":
                try:
                    Path(self.backend.address).unlink()
                except FileNotFoundError:
                    pass


def default_runtime_directory() -> Path:
    """Return a per-user location for the ephemeral broker descriptor."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / "ezhpcy" / "runtime"
    if runtime_dir := os.environ.get("XDG_RUNTIME_DIR"):
        return Path(runtime_dir) / "ezhpcy"
    if cache_dir := os.environ.get("XDG_CACHE_HOME"):
        return Path(cache_dir) / "ezhpcy" / "runtime"
    return Path.home() / ".cache" / "ezhpcy" / "runtime"


def default_descriptor_path() -> Path:
    return default_runtime_directory() / "broker.json"


def default_ipc_address(descriptor_path: Path | None = None) -> str:
    descriptor = (descriptor_path or default_descriptor_path()).resolve()
    if os.name == "nt":
        digest = hashlib.sha256(str(descriptor).casefold().encode()).hexdigest()[:16]
        return rf"\\.\pipe\ezhpcy-{digest}-broker"
    return str(descriptor.with_suffix(".sock"))


def create_broker_backend(
    address: str | None = None,
    *,
    descriptor_path: Path | None = None,
    authkey: bytes | None = None,
) -> AuthenticatedIPCBackend:
    """Create the server backend and its per-run authentication capability."""
    descriptor = descriptor_path or default_descriptor_path()
    return AuthenticatedIPCBackend(
        address=address or default_ipc_address(descriptor),
        family="AF_PIPE" if os.name == "nt" else "AF_UNIX",
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
        address = payload["address"]
        family = payload["family"]
        encoded_authkey = payload["authkey"]
        instance_id = payload["instance_id"]
        if version != RUNTIME_DESCRIPTOR_VERSION:
            raise ValueError("unsupported runtime descriptor version")
        if not all(
            isinstance(value, str)
            for value in (address, family, encoded_authkey, instance_id)
        ):
            raise ValueError("runtime descriptor values have invalid types")
        expected_family = "AF_PIPE" if os.name == "nt" else "AF_UNIX"
        if family != expected_family:
            raise ValueError("runtime descriptor is for another platform")
        authkey = base64.b64decode(encoded_authkey, validate=True)
    except (KeyError, TypeError, ValueError) as error:
        raise BrokerUnavailableError(
            "broker runtime information is invalid; restart the foreground broker"
        ) from error

    try:
        return AuthenticatedIPCBackend(
            address=address,
            family=family,
            authkey=authkey,
            descriptor_path=path,
            instance_id=instance_id,
        )
    except ValueError as error:
        raise BrokerUnavailableError(
            "broker runtime information is invalid; restart the foreground broker"
        ) from error


def _publish_runtime_descriptor(backend: AuthenticatedIPCBackend) -> None:
    path = backend.descriptor_path
    if path is None:
        raise IPCError("broker runtime descriptor path is not configured")
    directory = path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        directory.chmod(0o700)
    payload = {
        "version": RUNTIME_DESCRIPTOR_VERSION,
        "address": backend.address,
        "family": backend.family,
        "authkey": base64.b64encode(backend.authkey).decode("ascii"),
        "instance_id": backend.instance_id,
        "pid": os.getpid(),
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


def _wait_for_windows_pipe(address: str, timeout: float) -> None:
    if sys.platform != "win32":
        return
    import _winapi

    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BrokerUnavailableError(
                "foreground broker is not running; start `ezhpcy tunnel broker`"
            )
        try:
            _winapi.WaitNamedPipe(address, max(1, min(int(remaining * 1000), 100)))
            return
        except OSError as error:
            if error.winerror not in {2, 121, 231}:
                raise BrokerUnavailableError(
                    "could not connect to the foreground broker; restart it and retry"
                ) from error
            time.sleep(min(0.01, remaining))
