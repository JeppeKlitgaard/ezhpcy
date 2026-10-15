"""Race-safe threaded listener for authenticated loopback IPC."""

from dataclasses import dataclass, field
lazy import hmac
lazy import secrets
lazy import socket
lazy import struct
lazy import threading
lazy import uuid
lazy from collections.abc import Callable

lazy from ezhpcy.ipc import LOOPBACK_HOST, IPCError
lazy from ezhpcy.ipc.descriptor import TunnelDescriptor
lazy from ezhpcy.ipc.protocol import (
    AUTH_DIGEST_SIZE,
    AUTH_MAGIC,
    AUTH_NONCE_SIZE,
    AUTHENTICATION_TIMEOUT,
    ERROR,
    MAX_ERROR_SIZE,
    READY,
    _deadline,
    _receive_exact,
    _send_exact,
    client_digest,
    server_digest,
)

IPC_BACKLOG = 32
_ACCEPT_POLL_INTERVAL = 0.05


def authenticate_server(stream: socket.socket, authkey: bytes, timeout: float) -> None:
    deadline = _deadline(timeout)
    server_nonce = secrets.token_bytes(AUTH_NONCE_SIZE)
    _send_exact(stream, AUTH_MAGIC + server_nonce, deadline)
    response = _receive_exact(stream, AUTH_NONCE_SIZE + AUTH_DIGEST_SIZE, deadline)
    client_nonce = response[:AUTH_NONCE_SIZE]
    proof = response[AUTH_NONCE_SIZE:]
    expected = client_digest(authkey, server_nonce, client_nonce)
    if not hmac.compare_digest(proof, expected):
        raise IPCError("client authentication proof is invalid")
    _send_exact(
        stream,
        server_digest(authkey, server_nonce, client_nonce),
        deadline,
    )


def ready_worker_stream(stream: socket.socket) -> None:
    stream.sendall(bytes((READY,)))


def reject_worker_stream(stream: socket.socket, message: str) -> None:
    payload = message.encode("utf-8")[:MAX_ERROR_SIZE]
    stream.sendall(bytes((ERROR,)) + struct.pack("!H", len(payload)) + payload)


@dataclass(frozen=True)
class AuthenticatedIPCBackend:
    """The tunnel's IPC listener settings, published through its descriptor."""

    alias: str
    authkey: bytes = field(repr=False)
    instance_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    debug: bool = False

    def listen(
        self,
        client_handler: Callable[[socket.socket], None],
        error_handler: Callable[[Exception], None] | None = None,
    ) -> AuthenticatedIPCServer:
        try:
            server = AuthenticatedIPCServer(
                authkey=self.authkey,
                authentication_timeout=AUTHENTICATION_TIMEOUT,
                client_handler=client_handler,
                error_handler=error_handler,
            )
        except OSError as error:
            raise IPCError("could not create the tunnel IPC listener") from error

        descriptor = TunnelDescriptor(
            self.alias,
            port=server.port,
            authkey=self.authkey,
            instance_id=self.instance_id,
            debug=self.debug,
        )
        server.close_handler = descriptor.remove
        try:
            descriptor.publish()
        except BaseException:
            server.close()
            raise
        return server


def create_tunnel_backend(
    *, alias: str, authkey: bytes | None = None, debug: bool = False
) -> AuthenticatedIPCBackend:
    """Create the server backend and its per-run authentication capability."""
    return AuthenticatedIPCBackend(
        alias, authkey=authkey or secrets.token_bytes(32), debug=debug
    )


class AuthenticatedIPCServer:
    """Own a loopback listener and one daemon thread per authenticated client."""

    def __init__(
        self,
        *,
        authkey: bytes,
        authentication_timeout: float,
        client_handler: Callable[[socket.socket], None],
        error_handler: Callable[[Exception], None] | None,
    ) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind((LOOPBACK_HOST, 0))
            listener.listen(IPC_BACKLOG)
            listener.settimeout(_ACCEPT_POLL_INTERVAL)
        except BaseException:
            listener.close()
            raise

        self.port: int = listener.getsockname()[1]
        self.close_handler: Callable[[], None] | None = None
        self._listener = listener
        self._authkey = authkey
        self._authentication_timeout = authentication_timeout
        self._client_handler = client_handler
        self._error_handler = error_handler
        self._clients: set[socket.socket] = set()
        self._clients_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._closed = False
        self._stop_requested = threading.Event()

    def serve_forever(self) -> None:
        while not self._stop_requested.is_set():
            try:
                stream, _address = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._stop_requested.is_set():
                    return
                raise

            with self._clients_lock:
                if self._closed:
                    stream.close()
                    return
                self._clients.add(stream)

            thread = threading.Thread(
                target=self._serve_authenticated,
                args=(stream,),
                daemon=True,
                name="ezhpcy-ipc-client",
            )
            try:
                thread.start()
            except RuntimeError as error:
                with self._clients_lock:
                    self._clients.discard(stream)
                stream.close()
                self._report(error)

    def _serve_authenticated(self, stream: socket.socket) -> None:
        try:
            try:
                authenticate_server(
                    stream,
                    self._authkey,
                    self._authentication_timeout,
                )
            except TimeoutError:
                self._report(IPCError("local client authentication timed out"))
                return
            except IPCError, OSError:
                if not self._closed:
                    self._report(
                        IPCError("rejected a local client during tunnel authentication")
                    )
                return

            if self._closed:
                return
            stream.settimeout(None)
            try:
                self._client_handler(stream)
            # Report any handler failure without taking down the server.
            except Exception as error:  # noqa: BLE001
                self._report(error)
        finally:
            with self._clients_lock:
                self._clients.discard(stream)
            stream.close()

    def _report(self, error: Exception) -> None:
        if self._error_handler is not None:
            self._error_handler(error)

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._stop_requested.set()
            self._listener.close()
            with self._clients_lock:
                clients = list(self._clients)
                self._clients.clear()

        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            client.close()

        if self.close_handler is not None:
            self.close_handler()
