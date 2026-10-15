"""
The async tunnel server, driven by the synchronous proxy client over real loopback
sockets, through the in-process SSH server to a stand-in worker.
"""

import asyncio
import contextlib
import io
import logging
import socket
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any

import pytest

from ezhpcy import ssh
from ezhpcy.ipc import IPCError
from ezhpcy.ipc.client import connect, relay_proxy_stdio
from ezhpcy.ipc.descriptor import TunnelDescriptor
from ezhpcy.ipc.protocol import wait_for_worker_stream
from ezhpcy.ssh import RemoteHost
from ezhpcy.tunnel.server import TunnelServer
from ezhpcy.types import ConnectionInfo
from tests.support.sshserver import SSHTestServer

# How long a test waits for something that should happen at once.
WAIT_SECONDS = 5
BANNER = b"SSH-2.0-worker\r\n"

type WorkerHandler = Callable[
    [asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]
]


async def login(server: SSHTestServer) -> RemoteHost:
    # `model_construct`, because `ConnectionInfo.host` accepts domain names only.
    conn_info = ConnectionInfo.model_construct(
        user=server.user, host=server.host, ssh_keepalive_interval_seconds=30
    )
    return await ssh.connect(
        conn_info,
        port=server.port,
        known_hosts=server.known_hosts,
        client_keys=[server.client_key],
        agent_path=None,
    )


@asynccontextmanager
async def worker(
    ssh_server: SSHTestServer, handler: WorkerHandler
) -> AsyncGenerator[int]:
    """A stand-in for the worker's sshd, reachable from the login node; yields its port."""
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    ssh_server.allow_forwarding(port)
    try:
        yield port
    finally:
        server.close()
        server.close_clients()
        await server.wait_closed()


@asynccontextmanager
async def tunnel(
    ssh_server: SSHTestServer, worker_port: int, **options: Any
) -> AsyncGenerator[tuple[TunnelServer, TunnelDescriptor]]:
    """A tunnel server to the worker, and the descriptor a proxy would read."""
    async with (
        await login(ssh_server) as host,
        TunnelServer(host, ("127.0.0.1", worker_port), **options) as server,
    ):
        yield server, TunnelDescriptor("test", port=server.port, authkey=server.authkey)


def run_proxy(descriptor: TunnelDescriptor, stdin: bytes) -> bytes:
    """Run `ezhpcy proxy`'s relay with `stdin`; return what it wrote to stdout."""
    stdout = io.BytesIO()
    with connect(descriptor, timeout=WAIT_SECONDS) as stream:
        stream.settimeout(WAIT_SECONDS)
        relay_proxy_stdio(stream, io.BytesIO(stdin), stdout)
    return stdout.getvalue()


def open_stream(descriptor: TunnelDescriptor) -> socket.socket:
    """Connect as a proxy and wait until the tunnel has opened the worker channel."""
    stream = connect(descriptor, timeout=WAIT_SECONDS)
    stream.settimeout(WAIT_SECONDS)
    wait_for_worker_stream(stream)
    return stream


def receive_all(stream: socket.socket) -> bytes:
    received = bytearray()
    while chunk := stream.recv(65536):
        received.extend(chunk)
    return bytes(received)


async def banner_then_echo(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Send a banner at once, then echo what the client sent, after its EOF."""
    writer.write(BANNER)
    writer.write(b"worker:" + await reader.read())
    writer.close()


@pytest.fixture
def tunnel_log() -> Iterator[list[logging.LogRecord]]:
    """The tunnel server's log records (the `ezhpcy` logger doesn't propagate)."""
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger("ezhpcy.tunnel.server")
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield records
    logger.removeHandler(handler)
    logger.setLevel(level)


def messages(records: list[logging.LogRecord], level: int) -> list[str]:
    return [record.getMessage() for record in records if record.levelno == level]


async def logged(records: list[logging.LogRecord], prefix: str) -> str:
    """
    Wait for a message starting with `prefix`. A proxy can be done before the
    tunnel has finished with it, and closing the tunnel would cut that short.
    """
    async with asyncio.timeout(WAIT_SECONDS):
        while True:
            for record in records:
                if (message := record.getMessage()).startswith(prefix):
                    return message
            await asyncio.sleep(0.01)


async def test_proxy_reaches_the_worker_through_the_tunnel(
    ssh_server: SSHTestServer, tunnel_log: list[logging.LogRecord]
) -> None:
    async with (
        worker(ssh_server, banner_then_echo) as port,
        tunnel(ssh_server, port) as (_server, descriptor),
    ):
        first = await asyncio.to_thread(run_proxy, descriptor, b"SSH-2.0-client\r\n")
        second = await asyncio.to_thread(run_proxy, descriptor, b"again")
        relayed = await logged(tunnel_log, "Worker channel reached EOF: connection=2")

    assert first == BANNER + b"worker:SSH-2.0-client\r\n"
    assert second == BANNER + b"worker:again"
    assert "bytes_to_worker=5 bytes_to_proxy=" in relayed
    assert messages(tunnel_log, logging.ERROR) == []


async def test_the_worker_sees_the_proxys_end_of_file_before_it_answers(
    ssh_server: SSHTestServer,
) -> None:
    async def answer_after_end_of_file(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        writer.write(b"worker:" + await reader.read())
        writer.close()

    async with (
        worker(ssh_server, answer_after_end_of_file) as port,
        tunnel(ssh_server, port) as (_server, descriptor),
    ):
        output = await asyncio.to_thread(run_proxy, descriptor, b"client-first")

    assert output == b"worker:client-first"


async def test_the_proxy_sees_the_workers_end_of_file_and_may_still_send(
    ssh_server: SSHTestServer,
) -> None:
    received_after_eof = asyncio.get_running_loop().create_future()

    async def banner_then_half_close(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        writer.write(BANNER)
        writer.write_eof()
        received_after_eof.set_result(await reader.read())
        writer.close()

    def proxy(descriptor: TunnelDescriptor) -> bytes:
        with open_stream(descriptor) as stream:
            from_worker = receive_all(stream)
            stream.sendall(b"after the worker's EOF")
            stream.shutdown(socket.SHUT_WR)
        return from_worker

    async with (
        worker(ssh_server, banner_then_half_close) as port,
        tunnel(ssh_server, port) as (_server, descriptor),
    ):
        from_worker = await asyncio.to_thread(proxy, descriptor)
        async with asyncio.timeout(WAIT_SECONDS):
            assert await received_after_eof == b"after the worker's EOF"

    assert from_worker == BANNER


async def test_fifty_proxies_are_served_at_once(ssh_server: SSHTestServer) -> None:
    clients = 50
    connected = 0
    all_connected = asyncio.Event()

    async def echo_once_all_are_connected(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        nonlocal connected
        connected += 1
        if connected == clients:
            all_connected.set()
        async with asyncio.timeout(WAIT_SECONDS):
            await all_connected.wait()
        writer.write(await reader.read())
        writer.close()

    loop = asyncio.get_running_loop()
    async with (
        worker(ssh_server, echo_once_all_are_connected) as port,
        tunnel(ssh_server, port) as (_server, descriptor),
    ):
        with ThreadPoolExecutor(clients) as executor:
            outputs = await asyncio.gather(
                *(
                    loop.run_in_executor(
                        executor, run_proxy, descriptor, f"proxy {i}".encode()
                    )
                    for i in range(clients)
                )
            )

    assert outputs == [f"proxy {i}".encode() for i in range(clients)]


async def test_a_proxy_without_the_key_is_refused_and_serving_continues(
    ssh_server: SSHTestServer, tunnel_log: list[logging.LogRecord]
) -> None:
    async with (
        worker(ssh_server, banner_then_echo) as port,
        tunnel(ssh_server, port) as (_server, descriptor),
    ):
        impostor = TunnelDescriptor("test", port=descriptor.port, authkey=b"b" * 32)
        with pytest.raises(IPCError, match="authentication failed"):
            await asyncio.to_thread(run_proxy, impostor, b"")
        output = await asyncio.to_thread(run_proxy, descriptor, b"ok")

    assert output == BANNER + b"worker:ok"
    assert messages(tunnel_log, logging.ERROR) == [
        "Tunnel client error: rejected a local client during tunnel authentication"
    ]


async def test_a_stalled_handshake_times_out_without_blocking_others(
    ssh_server: SSHTestServer, tunnel_log: list[logging.LogRecord]
) -> None:
    async with (
        worker(ssh_server, banner_then_echo) as port,
        tunnel(ssh_server, port, authentication_timeout=0.2) as (_server, descriptor),
    ):
        stalled = await asyncio.to_thread(
            socket.create_connection, ("127.0.0.1", descriptor.port)
        )
        with stalled:
            output = await asyncio.to_thread(run_proxy, descriptor, b"ok")
            stalled.settimeout(WAIT_SECONDS)
            # The tunnel sends its greeting, then hangs up when time runs out.
            await asyncio.to_thread(receive_all, stalled)

    assert output == BANNER + b"worker:ok"
    assert messages(tunnel_log, logging.ERROR) == [
        "Tunnel client error: local client authentication timed out"
    ]


async def test_a_channel_the_login_node_refuses_is_reported_and_serving_continues(
    ssh_server: SSHTestServer,
) -> None:
    # The test server forwards only to ports a test allows.
    async with tunnel(ssh_server, 9) as (_server, descriptor):
        for _attempt in range(2):
            with pytest.raises(
                IPCError,
                match=r"^worker channel could not be opened: .*not allowed by the test",
            ):
                await asyncio.to_thread(run_proxy, descriptor, b"")


async def test_a_lost_login_connection_is_reported_without_opening_a_channel(
    ssh_server: SSHTestServer,
) -> None:
    connections = 0

    async def count(
        _reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        nonlocal connections
        connections += 1
        writer.close()

    async with (
        worker(ssh_server, count) as port,
        tunnel(ssh_server, port) as (server, descriptor),
    ):
        ssh_server.drop()
        async with asyncio.timeout(WAIT_SECONDS):
            await server.host.closed
        with pytest.raises(IPCError, match=r"^login-node transport was lost; restart"):
            await asyncio.to_thread(run_proxy, descriptor, b"")

    assert connections == 0


async def test_losing_the_login_connection_mid_session_ends_the_relay(
    ssh_server: SSHTestServer, tunnel_log: list[logging.LogRecord]
) -> None:
    async def hold(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(BANNER)
        await reader.read()
        writer.close()

    async with (
        worker(ssh_server, hold) as port,
        tunnel(ssh_server, port) as (_server, descriptor),
    ):
        stream = await asyncio.to_thread(open_stream, descriptor)
        with stream:
            assert await asyncio.to_thread(stream.recv, len(BANNER)) == BANNER
            ssh_server.drop()
            assert await asyncio.to_thread(receive_all, stream) == b""

    assert any(
        message.startswith("Worker channel relay failed: connection=1")
        for message in messages(tunnel_log, logging.WARNING)
    )


async def test_closing_does_not_wait_for_a_proxy_that_stopped_reading(
    ssh_server: SSHTestServer,
) -> None:
    async def flood(
        _reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        chunk = b"x" * 65536
        # Until the tunnel closes the channel, and with it this connection.
        with contextlib.suppress(ConnectionError):
            while True:
                writer.write(chunk)
                await writer.drain()

    async with (
        worker(ssh_server, flood) as port,
        tunnel(ssh_server, port) as (server, descriptor),
    ):
        # The proxy never reads, so the tunnel's buffer towards it fills up.
        with await asyncio.to_thread(open_stream, descriptor):
            await asyncio.sleep(0.5)
            async with asyncio.timeout(WAIT_SECONDS):
                await server.close()


async def test_closing_the_server_ends_connected_proxies(
    ssh_server: SSHTestServer,
) -> None:
    worker_saw_close = asyncio.Event()
    closed = 0

    async def hold(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal closed
        await reader.read()
        closed += 1
        if closed == 2:
            worker_saw_close.set()
        writer.close()

    async with (
        worker(ssh_server, hold) as port,
        tunnel(ssh_server, port) as (server, descriptor),
    ):
        streams = [await asyncio.to_thread(open_stream, descriptor) for _ in range(2)]
        try:
            async with asyncio.timeout(WAIT_SECONDS):
                await server.close()
            for stream in streams:
                assert await asyncio.to_thread(receive_all, stream) == b""
            async with asyncio.timeout(WAIT_SECONDS):
                await worker_saw_close.wait()
            with pytest.raises(IPCError, match="no tunnel is running"):
                await asyncio.to_thread(run_proxy, descriptor, b"")
        finally:
            for stream in streams:
                stream.close()
