"""Race-safe threaded listener for authenticated loopback IPC."""

import socket
import threading
from collections.abc import Callable

from ezhpcy.ipc.common import IPCAddress, IPCAuthenticationError
from ezhpcy.ipc.protocol import AuthenticationError, authenticate_server

IPC_BACKLOG = 32
_ACCEPT_POLL_INTERVAL = 0.05


class AuthenticatedIPCServer:
    """Own a loopback listener and one daemon thread per authenticated client."""

    def __init__(
        self,
        address: IPCAddress,
        *,
        authkey: bytes,
        authentication_timeout: float,
        client_handler: Callable[[socket.socket], None],
        error_handler: Callable[[Exception], None] | None,
        close_handler: Callable[[], None] | None = None,
    ) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(address.as_tuple())
            listener.listen(IPC_BACKLOG)
            listener.settimeout(_ACCEPT_POLL_INTERVAL)
        except BaseException:
            listener.close()
            raise

        self.address = IPCAddress(*listener.getsockname()[:2])
        self._listener = listener
        self._authkey = authkey
        self._authentication_timeout = authentication_timeout
        self._client_handler = client_handler
        self._error_handler = error_handler
        self._close_handler = close_handler
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
                self._report(
                    IPCAuthenticationError("local client authentication timed out")
                )
                return
            except AuthenticationError, OSError:
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
            try:
                self._client_handler(stream)
            except Exception as error:
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

        if self._close_handler is not None:
            self._close_handler()
