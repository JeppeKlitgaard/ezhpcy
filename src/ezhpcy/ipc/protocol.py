"""
The tunnel IPC wire protocol, and the proxy's (synchronous) side of it.

1. The tunnel sends `AUTH_MAGIC` and a random nonce.
2. The proxy answers with its own nonce and an HMAC of both nonces, keyed with
   the descriptor's authkey.
3. The tunnel answers with its own HMAC of both nonces. Each side now knows that
   the other holds the key.
4. Once its channel to the worker is open, the tunnel sends one status byte:
   `READY`, or `ERROR` followed by a 2-byte length and a UTF-8 message.
"""

lazy import hmac
lazy import secrets
lazy import socket
lazy import struct
lazy import time

lazy from ezhpcy.ipc import IPCError

MAX_ERROR_SIZE = 1024
AUTHENTICATION_TIMEOUT = 2.0

AUTH_MAGIC = b"EZHPCY\x00\x01"
AUTH_NONCE_SIZE = 32
AUTH_DIGEST_SIZE = 32
READY = 0
ERROR = 1


def client_digest(authkey: bytes, server_nonce: bytes, client_nonce: bytes) -> bytes:
    return hmac.digest(
        authkey, b"ezhpcy-client" + server_nonce + client_nonce, "sha256"
    )


def server_digest(authkey: bytes, server_nonce: bytes, client_nonce: bytes) -> bytes:
    return hmac.digest(
        authkey, b"ezhpcy-server" + server_nonce + client_nonce, "sha256"
    )


def _deadline(timeout: float) -> float:
    return time.monotonic() + max(0.0, timeout)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("tunnel authentication timed out")
    return remaining


def _send_exact(stream: socket.socket, data: bytes, deadline: float) -> None:
    stream.settimeout(_remaining(deadline))
    stream.sendall(data)


def _receive_exact(
    stream: socket.socket, size: int, deadline: float | None = None
) -> bytes:
    """Read exactly `size` bytes, before `deadline` (a `time.monotonic()` value)."""
    data = bytearray()
    while len(data) < size:
        if deadline is not None:
            stream.settimeout(_remaining(deadline))
        chunk = stream.recv(size - len(data))
        if not chunk:
            raise IPCError("tunnel IPC connection closed")
        data.extend(chunk)
    return bytes(data)


def authenticate_client(stream: socket.socket, authkey: bytes, timeout: float) -> None:
    deadline = _deadline(timeout)
    greeting = _receive_exact(stream, len(AUTH_MAGIC) + AUTH_NONCE_SIZE, deadline)
    if greeting[: len(AUTH_MAGIC)] != AUTH_MAGIC:
        raise IPCError("tunnel authentication protocol is invalid")
    server_nonce = greeting[len(AUTH_MAGIC) :]
    client_nonce = secrets.token_bytes(AUTH_NONCE_SIZE)
    _send_exact(
        stream,
        client_nonce + client_digest(authkey, server_nonce, client_nonce),
        deadline,
    )
    proof = _receive_exact(stream, AUTH_DIGEST_SIZE, deadline)
    expected = server_digest(authkey, server_nonce, client_nonce)
    if not hmac.compare_digest(proof, expected):
        raise IPCError("tunnel authentication proof is invalid")


def wait_for_worker_stream(stream: socket.socket) -> None:
    """Wait for the tunnel's status byte; raise `IPCError` with its rejection."""
    status = _receive_exact(stream, 1)[0]
    if status == READY:
        return
    if status != ERROR:
        raise IPCError("tunnel returned an invalid readiness response")
    size = struct.unpack("!H", _receive_exact(stream, 2))[0]
    if size > MAX_ERROR_SIZE:
        raise IPCError("tunnel returned an oversized error response")
    message = _receive_exact(stream, size).decode("utf-8", errors="replace")
    raise IPCError(message or "tunnel rejected stream")
