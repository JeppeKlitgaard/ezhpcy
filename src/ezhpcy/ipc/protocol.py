"""Fixed-size authentication and readiness messages for broker IPC."""

import hmac
import secrets
import socket
import struct
import time

from ezhpcy.ipc.common import IPCError, ProtocolError

MAX_ERROR_SIZE = 1024
AUTHENTICATION_TIMEOUT = 2.0

AUTH_MAGIC = b"EZHPCY\x00\x01"
AUTH_NONCE_SIZE = 32
AUTH_DIGEST_SIZE = 32
_READY = 0
_ERROR = 1


class AuthenticationError(Exception):
    """The fixed-size HMAC exchange failed."""


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
            raise AuthenticationError("peer closed during authentication")
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


def authenticate_client(stream: socket.socket, authkey: bytes, timeout: float) -> None:
    deadline = _deadline(timeout)
    greeting = _receive_exact(stream, len(AUTH_MAGIC) + AUTH_NONCE_SIZE, deadline)
    if greeting[: len(AUTH_MAGIC)] != AUTH_MAGIC:
        raise AuthenticationError("broker authentication protocol is invalid")
    server_nonce = greeting[len(AUTH_MAGIC) :]
    client_nonce = secrets.token_bytes(AUTH_NONCE_SIZE)
    _send_exact(
        stream,
        client_nonce + _client_digest(authkey, server_nonce, client_nonce),
        deadline,
    )
    proof = _receive_exact(stream, AUTH_DIGEST_SIZE, deadline)
    expected = _server_digest(authkey, server_nonce, client_nonce)
    if not hmac.compare_digest(proof, expected):
        raise AuthenticationError("broker authentication proof is invalid")


def authenticate_server(stream: socket.socket, authkey: bytes, timeout: float) -> None:
    deadline = _deadline(timeout)
    server_nonce = secrets.token_bytes(AUTH_NONCE_SIZE)
    _send_exact(stream, AUTH_MAGIC + server_nonce, deadline)
    response = _receive_exact(stream, AUTH_NONCE_SIZE + AUTH_DIGEST_SIZE, deadline)
    client_nonce = response[:AUTH_NONCE_SIZE]
    proof = response[AUTH_NONCE_SIZE:]
    expected = _client_digest(authkey, server_nonce, client_nonce)
    if not hmac.compare_digest(proof, expected):
        raise AuthenticationError("client authentication proof is invalid")
    _send_exact(
        stream,
        _server_digest(authkey, server_nonce, client_nonce),
        deadline,
    )
