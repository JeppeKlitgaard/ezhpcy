"""Authenticated, versioned IPC for the foreground tunnel broker."""

import base64
import hmac
import json
import os
import secrets
import socket
import socketserver
import stat
import struct
import threading
import time
import uuid
from dataclasses import InitVar, dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from ezhpcy.config import config

RUNTIME_DESCRIPTOR_VERSION = 1
MAX_ERROR_SIZE = 1024
IPC_BACKLOG = 32
AUTHENTICATION_TIMEOUT = 2.0

_LOOPBACK_HOST = "127.0.0.1"
_AUTH_MAGIC = b"EZHPCY\x00\x01"
_AUTH_NONCE_SIZE = 32
_AUTH_DIGEST_SIZE = 32
_READY = 0
_ERROR = 1

# Wire protocol
#
# The broker sends ``_AUTH_MAGIC`` (including the protocol version) and a
# random server nonce. The proxy replies with its nonce and an HMAC proving
# possession of the descriptor's capability key; the broker returns a
# role-separated HMAC so authentication is mutual. The broker then sends either
# ``_READY`` or ``_ERROR || uint16-length || UTF-8 message``. After ``_READY``,
# framing ends permanently and both directions carry raw worker SSH bytes.
#
# This prevents a local process that cannot read the owner-only descriptor from
# using or impersonating the broker. It does not defend against denial of
# service, compromise of the same OS account, or an administrator/root process;
# worker confidentiality and authentication remain the responsibility of SSH.


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


class IPCError(Exception):
    """Base class for actionable broker IPC failures."""


class BrokerUnavailableError(IPCError):
    """The foreground broker is not accepting connections."""


class IPCAuthenticationError(IPCError):
    """The broker and proxy do not share the same capability key."""


class _AuthenticationError(Exception):
    """The fixed-size HMAC exchange failed."""


class ProtocolError(IPCError):
    """The peer sent an invalid or unsupported IPC message."""


class IPCServer(Protocol):
    address: IPCAddress

    def serve_forever(self) -> None: ...

    def close(self) -> None: ...


class IPCBackend(Protocol):
    def listen(
        self,
        client_handler: Callable[[socket.socket], None],
        error_handler: Callable[[Exception], None] | None = None,
    ) -> IPCServer: ...

    def connect(self, *, timeout: float) -> socket.socket: ...


def _receive_stream_bytes(stream: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.recv(size - len(chunks))
        if not chunk:
            raise EOFError("broker IPC connection closed")
        chunks.extend(chunk)
    return bytes(chunks)


def wait_for_worker_stream(stream: socket.socket) -> None:
    status = _receive_stream_bytes(stream, 1)[0]
    if status == _READY:
        return
    if status != _ERROR:
        raise ProtocolError("broker returned an invalid readiness response")
    size = struct.unpack("!H", _receive_stream_bytes(stream, 2))[0]
    if size > MAX_ERROR_SIZE:
        raise ProtocolError("broker returned an oversized error response")
    message = _receive_stream_bytes(stream, size).decode("utf-8", errors="replace")
    raise IPCError(message or "broker rejected stream")


def ready_worker_stream(stream: socket.socket) -> None:
    stream.sendall(bytes((_READY,)))


def reject_worker_stream(stream: socket.socket, message: str) -> None:
    payload = message.encode("utf-8")[:MAX_ERROR_SIZE]
    stream.sendall(bytes((_ERROR,)) + struct.pack("!H", len(payload)) + payload)


@dataclass(frozen=True)
class AuthenticatedIPCBackend:
    """An authenticated TCP endpoint bound exclusively to IPv4 loopback."""

    address: IPCAddress
    authkey: bytes = field(repr=False)
    descriptor_path: Path | None = None
    instance_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    publish_descriptor: bool = False
    authentication_timeout: float = AUTHENTICATION_TIMEOUT

    def __post_init__(self) -> None:
        if len(self.authkey) < 32:
            raise ValueError("broker authkey must contain at least 32 random bytes")
        if self.authentication_timeout <= 0:
            raise ValueError("broker authentication timeout must be positive")

    def listen(
        self,
        client_handler: Callable[[socket.socket], None],
        error_handler: Callable[[Exception], None] | None = None,
    ) -> IPCServer:
        try:
            server = _AuthenticatedServer(
                self.address.as_tuple(),
                self,
                client_handler,
                error_handler,
            )
        except OSError as error:
            raise IPCError("could not create the broker IPC listener") from error

        try:
            if self.publish_descriptor:
                _publish_runtime_descriptor(self, server.address)
        except BaseException:
            server.close()
            raise
        return server

    def connect(self, *, timeout: float = 2.0) -> socket.socket:
        try:
            client_socket = socket.create_connection(
                self.address.as_tuple(), timeout=max(0.0, timeout)
            )
        except OSError as error:
            raise BrokerUnavailableError(
                "foreground broker is not running; start `ezhpcy tunnel broker`"
            ) from error

        try:
            _authenticate_client(client_socket, self.authkey, timeout)
            client_socket.settimeout(None)
        except (_AuthenticationError, OSError, TimeoutError) as error:
            client_socket.close()
            raise IPCAuthenticationError(
                "broker authentication failed; restart the foreground broker"
            ) from error
        return client_socket


def _deadline(timeout: float) -> float:
    return time.monotonic() + max(0.0, timeout)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("broker authentication timed out")
    return remaining


def _send_exact(stream: socket.socket, data: bytes, deadline: float) -> None:
    stream.settimeout(_remaining(deadline))
    stream.sendall(data)


def _receive_exact(stream: socket.socket, size: int, deadline: float) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        stream.settimeout(_remaining(deadline))
        chunk = stream.recv(size - len(chunks))
        if not chunk:
            raise _AuthenticationError("peer closed during authentication")
        chunks.extend(chunk)
    return bytes(chunks)


def _client_digest(authkey: bytes, server_nonce: bytes, client_nonce: bytes) -> bytes:
    return hmac.digest(
        authkey, b"ezhpcy-client" + server_nonce + client_nonce, "sha256"
    )


def _server_digest(authkey: bytes, server_nonce: bytes, client_nonce: bytes) -> bytes:
    return hmac.digest(
        authkey, b"ezhpcy-server" + server_nonce + client_nonce, "sha256"
    )


def _authenticate_client(stream: socket.socket, authkey: bytes, timeout: float) -> None:
    deadline = _deadline(timeout)
    greeting = _receive_exact(stream, len(_AUTH_MAGIC) + _AUTH_NONCE_SIZE, deadline)
    if greeting[: len(_AUTH_MAGIC)] != _AUTH_MAGIC:
        raise _AuthenticationError("broker authentication protocol is invalid")
    server_nonce = greeting[len(_AUTH_MAGIC) :]
    client_nonce = secrets.token_bytes(_AUTH_NONCE_SIZE)
    _send_exact(
        stream,
        client_nonce + _client_digest(authkey, server_nonce, client_nonce),
        deadline,
    )
    proof = _receive_exact(stream, _AUTH_DIGEST_SIZE, deadline)
    expected = _server_digest(authkey, server_nonce, client_nonce)
    if not hmac.compare_digest(proof, expected):
        raise _AuthenticationError("broker authentication proof is invalid")


def _authenticate_server(stream: socket.socket, authkey: bytes, timeout: float) -> None:
    deadline = _deadline(timeout)
    server_nonce = secrets.token_bytes(_AUTH_NONCE_SIZE)
    _send_exact(stream, _AUTH_MAGIC + server_nonce, deadline)
    response = _receive_exact(stream, _AUTH_NONCE_SIZE + _AUTH_DIGEST_SIZE, deadline)
    client_nonce = response[:_AUTH_NONCE_SIZE]
    proof = response[_AUTH_NONCE_SIZE:]
    expected = _client_digest(authkey, server_nonce, client_nonce)
    if not hmac.compare_digest(proof, expected):
        raise _AuthenticationError("client authentication proof is invalid")
    _send_exact(
        stream,
        _server_digest(authkey, server_nonce, client_nonce),
        deadline,
    )


class _AuthenticatedHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server: _AuthenticatedServer = self.server  # type: ignore[assignment]
        server._serve_authenticated(self.request)  # type: ignore[arg-type]


class _AuthenticatedServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = IPC_BACKLOG

    def __init__(
        self,
        address: tuple[str, int],
        backend: AuthenticatedIPCBackend,
        client_handler: Callable[[socket.socket], None],
        error_handler: Callable[[Exception], None] | None,
    ) -> None:
        self.backend = backend
        self.client_handler = client_handler
        self.error_handler = error_handler
        self._clients: set[socket.socket] = set()
        self._clients_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._closed = False
        self._serving = threading.Event()
        self._stop_requested = threading.Event()
        super().__init__(address, _AuthenticatedHandler)

    @property
    def address(self) -> IPCAddress:
        return IPCAddress(*self.server_address[:2])

    def verify_request(
        self, request: socket.socket, client_address: tuple[str, int]
    ) -> bool:
        with self._clients_lock:
            self._clients.add(request)
        return True

    def serve_forever(self) -> None:
        if self._stop_requested.is_set():
            return
        self._serving.set()
        if self._stop_requested.is_set():
            self._serving.clear()
            return
        try:
            super().serve_forever(poll_interval=0.05)
        finally:
            self._serving.clear()

    def _serve_authenticated(self, stream: socket.socket) -> None:
        try:
            try:
                _authenticate_server(
                    stream,
                    self.backend.authkey,
                    self.backend.authentication_timeout,
                )
            except TimeoutError:
                self._report(
                    IPCAuthenticationError("local client authentication timed out")
                )
                return
            except _AuthenticationError, OSError:
                if not self._closed:
                    self._report(
                        IPCAuthenticationError(
                            "rejected a local client during broker authentication"
                        )
                    )
                return
            if self._closed:
                return
            stream.settimeout(None)
            self.client_handler(stream)
        finally:
            with self._clients_lock:
                self._clients.discard(stream)
            stream.close()

    def _report(self, error: Exception) -> None:
        if self.error_handler is not None:
            self.error_handler(error)

    def handle_error(
        self, request: socket.socket, client_address: tuple[str, int]
    ) -> None:
        import sys

        error = sys.exception()
        if isinstance(error, Exception):
            self._report(error)
        else:
            super().handle_error(request, client_address)

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._stop_requested.set()
            if self._serving.is_set():
                self.shutdown()
            self.server_close()
            with self._clients_lock:
                clients = list(self._clients)
                self._clients.clear()
            for client in clients:
                try:
                    client.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                client.close()
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
    payload = {
        "version": RUNTIME_DESCRIPTOR_VERSION,
        "host": address.host,
        "port": address.port,
        "authkey": base64.b64encode(backend.authkey).decode("ascii"),
        "instance_id": backend.instance_id,
    }
    directory = path.parent
    directory_fd = _prepare_runtime_directory(directory)
    temporary_name = f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    temporary = directory / temporary_name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        if directory_fd is None:
            descriptor = os.open(temporary, flags, 0o600)
        else:
            descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(payload, file, separators=(",", ":"))
            file.flush()
            os.fsync(file.fileno())
        if directory_fd is None:
            os.replace(temporary, path)
        else:
            os.replace(
                temporary_name,
                path.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
    finally:
        try:
            try:
                if directory_fd is None:
                    temporary.unlink()
                else:
                    os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        finally:
            if directory_fd is not None:
                os.close(directory_fd)


def _prepare_runtime_directory(directory: Path) -> int | None:
    """Create a private runtime directory and pin its audited POSIX inode."""
    try:
        directory.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        pass
    except OSError as error:
        raise IPCError("could not create the broker runtime directory") from error

    if os.name != "posix":
        # Python 3.14 applies an owner-and-administrators-only ACL when mode 0700
        # creates a directory on Windows. Files then inherit that protected ACL.
        return None

    try:
        metadata = directory.lstat()
    except OSError as error:
        raise IPCError("could not inspect the broker runtime directory") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise IPCError("broker runtime directory must not be a symbolic link")
    if not stat.S_ISDIR(metadata.st_mode):
        raise IPCError("broker runtime path is not a directory")
    if metadata.st_uid != os.getuid():
        raise IPCError("broker runtime directory is not owned by the current user")

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        directory_fd = os.open(directory, flags)
    except OSError as error:
        raise IPCError(
            "could not securely open the broker runtime directory"
        ) from error

    try:
        metadata = os.fstat(directory_fd)
        if not stat.S_ISDIR(metadata.st_mode):
            raise IPCError("broker runtime path is not a directory")
        if metadata.st_uid != os.getuid():
            raise IPCError("broker runtime directory is not owned by the current user")
        try:
            os.fchmod(directory_fd, 0o700)
        except OSError as error:
            raise IPCError(
                "could not restrict the broker runtime directory permissions"
            ) from error
        if stat.S_IMODE(os.fstat(directory_fd).st_mode) != 0o700:
            raise IPCError("broker runtime directory permissions are not private")
    except BaseException:
        os.close(directory_fd)
        raise
    return directory_fd


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
