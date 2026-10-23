"""
Forwarding to either destination type, and the inner connection to the worker's
sshd, against the in-process SSH servers. The TCP-only forwarding tests are in
`test_ssh.py`.
"""

import asyncio
import socket
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Any

import asyncssh
import pytest

from ezhpcy import ssh
from ezhpcy.constants import LOOPBACK_HOST
from ezhpcy.ssh import (
    Destination,
    ForwardedPort,
    RemoteHost,
    SocketDestination,
    TCPDestination,
)
from ezhpcy.types import Login
from tests.support.sshserver import Auth, SSHTestServer, reply
from tests.support.worker import worker_credentials, worker_sshd

WAIT_SECONDS = 5
SOCKET = PurePosixPath("/tmp/ezhpcy-0123456789abcdef/app.sock")

# Serves either kind of handler: a TCP stand-in gets asyncio's streams, a socket
# one asyncssh's, which read and write alike.
type Handler = Callable[[Any, Any], Any]
type Serve = Callable[
    [SSHTestServer, Handler], AbstractAsyncContextManager[Destination]
]


@asynccontextmanager
async def _tcp(server: SSHTestServer, handler: Handler) -> AsyncGenerator[Destination]:
    """Serve `handler` on a loopback port that the server may forward to."""
    listener = await asyncio.start_server(handler, LOOPBACK_HOST, 0)
    port = listener.sockets[0].getsockname()[1]
    server.allow_forwarding(port)
    try:
        yield TCPDestination(LOOPBACK_HOST, port)
    finally:
        listener.close()
        listener.close_clients()
        await listener.wait_closed()


@asynccontextmanager
async def _socket(
    server: SSHTestServer, handler: Handler
) -> AsyncGenerator[Destination]:
    """Serve `handler` on a Unix socket path, inside the server."""
    server.allow_socket(SOCKET, handler)
    try:
        yield SocketDestination(SOCKET)
    finally:
        server.allow_socket(SOCKET, None)


@pytest.fixture(params=[_tcp, _socket], ids=["tcp", "socket"])
def serve(request: pytest.FixtureRequest) -> Serve:
    return request.param


def _login(server: SSHTestServer) -> Login:
    return Login(user=server.user, host=server.host)


async def _connect(server: SSHTestServer) -> RemoteHost:
    return await ssh.connect(
        _login(server),
        port=server.port,
        known_hosts=server.known_hosts,
        client_keys=[server.client_key],
        agent_path=None,
    )


async def _echo(reader: Any, writer: Any) -> None:
    """Echo until the client's EOF, then close."""
    while data := await reader.read(64 * 1024):
        writer.write(data)
        await writer.drain()
    writer.close()


async def _open(
    listener: ForwardedPort,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection(LOOPBACK_HOST, listener.port)


async def _round_trip(listener: ForwardedPort, payload: bytes = b"ping\n") -> bytes:
    reader, writer = await _open(listener)
    writer.write(payload)
    writer.write_eof()
    try:
        return await asyncio.wait_for(reader.read(), WAIT_SECONDS)
    finally:
        writer.close()


async def _refused(listener: ForwardedPort) -> bool:
    try:
        return await _round_trip(listener) == b""
    except ConnectionResetError:
        return True


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind((LOOPBACK_HOST, 0))
        return probe.getsockname()[1]


# Either destination


async def test_forwarding_passes_on_the_clients_eof(
    ssh_server: SSHTestServer, serve: Serve
) -> None:
    payload = bytes(range(256)) * 4096
    async with (
        serve(ssh_server, _echo) as destination,
        await _connect(ssh_server) as host,
    ):
        listener = await host.forward_local_port(destination)
        assert await _round_trip(listener, payload) == payload


async def test_forwarding_passes_on_the_destinations_eof(
    ssh_server: SSHTestServer, serve: Serve
) -> None:
    received: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
    loop = asyncio.get_running_loop()

    async def banner_then_read(reader: Any, writer: Any) -> None:
        writer.write(b"HTTP/1.1 200 OK\r\n")
        writer.write_eof()
        data = await reader.read()
        # The handler may run on the server's loop.
        loop.call_soon_threadsafe(received.set_result, data)
        writer.close()

    async with (
        serve(ssh_server, banner_then_read) as destination,
        await _connect(ssh_server) as host,
    ):
        listener = await host.forward_local_port(destination)
        reader, writer = await _open(listener)
        try:
            assert await asyncio.wait_for(reader.read(), WAIT_SECONDS) == (
                b"HTTP/1.1 200 OK\r\n"
            )
            writer.write(b"after EOF")
            writer.write_eof()
            assert await asyncio.wait_for(received, WAIT_SECONDS) == b"after EOF"
        finally:
            writer.close()


async def test_forwarding_serves_concurrent_connections(
    ssh_server: SSHTestServer, serve: Serve
) -> None:
    payloads = [f"connection {index}\n".encode() * 1000 for index in range(50)]
    async with (
        serve(ssh_server, _echo) as destination,
        await _connect(ssh_server) as host,
    ):
        listener = await host.forward_local_port(destination)
        replies = await asyncio.gather(
            *(_round_trip(listener, payload) for payload in payloads)
        )

    assert replies == payloads


async def test_forwarding_survives_a_refused_channel(
    ssh_server: SSHTestServer, serve: Serve
) -> None:
    counts: list[int] = []
    async with await _connect(ssh_server) as host:
        # Find the destination, then stop serving it.
        async with serve(ssh_server, _echo) as destination:
            pass
        listener = await host.forward_local_port(
            destination, on_connections=counts.append
        )
        # The channel can't be opened, so the local connection is closed, and
        # isn't counted as open any more.
        assert await _refused(listener)
        assert counts == [1, 0]

    async with (
        serve(ssh_server, _echo) as destination,
        await _connect(ssh_server) as host,
    ):
        listener = await host.forward_local_port(destination)
        assert await _round_trip(listener) == b"ping\n"


async def test_forwarding_counts_the_open_connections(
    ssh_server: SSHTestServer, serve: Serve
) -> None:
    counts: list[int] = []
    all_closed = asyncio.Event()

    def count(connections: int) -> None:
        counts.append(connections)
        if connections == 0:
            all_closed.set()

    async with (
        serve(ssh_server, _echo) as destination,
        await _connect(ssh_server) as host,
    ):
        listener = await host.forward_local_port(destination, on_connections=count)
        first = await _open(listener)
        second = await _open(listener)
        for reader, writer in (first, second):
            writer.write(b"ping\n")
            assert await asyncio.wait_for(reader.readline(), WAIT_SECONDS) == b"ping\n"
        for reader, writer in (first, second):
            writer.write_eof()
            assert await asyncio.wait_for(reader.read(), WAIT_SECONDS) == b""
            writer.close()
        await asyncio.wait_for(all_closed.wait(), WAIT_SECONDS)

    assert counts == [1, 2, 1, 0]


async def test_open_reaches_either_destination(
    ssh_server: SSHTestServer, serve: Serve
) -> None:
    async with (
        serve(ssh_server, _echo) as destination,
        await _connect(ssh_server) as host,
    ):
        reader, writer = await host.open(destination)
        writer.write(b"ping\n")
        assert await asyncio.wait_for(reader.readline(), WAIT_SECONDS) == b"ping\n"
        writer.close()


# The local port


async def test_forwarding_listens_on_a_fixed_local_port(
    ssh_server: SSHTestServer,
) -> None:
    local_port = _free_port()
    async with (
        _socket(ssh_server, _echo) as destination,
        await _connect(ssh_server) as host,
    ):
        listener = await host.forward_local_port(destination, local_port=local_port)
        assert listener.port == local_port
        assert await _round_trip(listener) == b"ping\n"


async def test_forwarding_to_a_taken_local_port_fails(
    ssh_server: SSHTestServer,
) -> None:
    with socket.socket() as taken:
        taken.bind((LOOPBACK_HOST, 0))
        taken.listen()
        async with await _connect(ssh_server) as host:
            with pytest.raises(OSError):
                await host.forward_local_port(
                    SocketDestination(SOCKET), local_port=taken.getsockname()[1]
                )


# The inner connection


async def test_connect_worker_reaches_the_workers_sshd(
    login: SSHTestServer, tmp_path: Path
) -> None:
    credentials = await asyncio.to_thread(worker_credentials)
    with worker_sshd(login, tmp_path / "worker", credentials) as worker:
        worker.command("hostname", reply(stdout="node-1\n"))
        async with await _connect(login) as host:
            inner = await host.connect_worker(worker.host, worker.port, credentials)
            async with inner:
                assert await inner.run(["hostname"]) == "node-1\n"
            # Closing the inner connection leaves the login connection open.
            assert await asyncio.wait_for(inner.closed, WAIT_SECONDS) is None
            assert not host.closed.done()

    # The worker's key, not the login node's.
    assert worker.auth_attempts == [("publickey", True)]


async def test_connect_worker_refuses_a_host_key_that_isnt_pinned(
    login: SSHTestServer, tmp_path: Path
) -> None:
    credentials = await asyncio.to_thread(worker_credentials)
    # A worker with another host key than the one pinned.
    impostor = SSHTestServer(tmp_path / "impostor")
    impostor.auth = Auth(
        client_keys=[asyncssh.read_private_key(credentials.client_key)]
    )
    with impostor:
        login.allow_forwarding(impostor.port)
        async with await _connect(login) as host:
            with pytest.raises(asyncssh.HostKeyNotVerifiable):
                await host.connect_worker(impostor.host, impostor.port, credentials)


async def test_connect_worker_fails_with_a_client_key_the_worker_refuses(
    login: SSHTestServer, tmp_path: Path
) -> None:
    credentials = await asyncio.to_thread(worker_credentials)
    with worker_sshd(login, tmp_path / "worker", credentials) as worker:
        worker.auth = Auth(client_keys=[login.client_key])
        async with await _connect(login) as host:
            with pytest.raises(asyncssh.PermissionDenied):
                await host.connect_worker(worker.host, worker.port, credentials)


async def test_losing_the_inner_connection_closes_its_forwarded_port(
    login: SSHTestServer, tmp_path: Path
) -> None:
    credentials = await asyncio.to_thread(worker_credentials)
    with worker_sshd(login, tmp_path / "worker", credentials) as worker:
        worker.allow_socket(SOCKET, _echo)
        async with await _connect(login) as host:
            inner = await host.connect_worker(worker.host, worker.port, credentials)
            listener = await inner.forward_local_port(SocketDestination(SOCKET))
            # App traffic goes through the worker's sshd.
            assert await _round_trip(listener) == b"ping\n"

            worker.drop()

            assert isinstance(
                await asyncio.wait_for(inner.closed, WAIT_SECONDS),
                asyncssh.ConnectionLost,
            )
            with pytest.raises(ConnectionRefusedError):
                await asyncio.wait_for(_open(listener), WAIT_SECONDS)
            # The login connection is another matter.
            assert not host.closed.done()
