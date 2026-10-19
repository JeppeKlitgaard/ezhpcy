"""Tests for `ezhpcy.ssh`, against the in-process SSH server."""

import asyncio
import shlex
import socket
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Any, cast

import asyncssh
import pytest

from ezhpcy import ssh
from ezhpcy.constants import EZHPCY_VERSION, LOOPBACK_HOST, PIXI_VERSION
from ezhpcy.ssh import RemoteHost
from ezhpcy.types import ConnectionInfo, RemoteState
from tests.support.sshserver import Invocation, SSHTestServer, StreamHandler, reply

LARGE_OUTPUT_BYTES = 5_000_000
# How long a test waits for something that should happen at once.
WAIT_SECONDS = 5


def _conn_info(server: SSHTestServer, keepalive_seconds: int = 30) -> ConnectionInfo:
    # `model_construct`, because `ConnectionInfo.host` accepts domain names only.
    return ConnectionInfo.model_construct(
        user=server.user,
        host=server.host,
        ssh_keepalive_interval_seconds=keepalive_seconds,
    )


async def _connect(server: SSHTestServer, **options: Any) -> RemoteHost:
    """Connect with the server's own client key and host key only."""
    defaults: dict[str, Any] = {
        "port": server.port,
        "known_hosts": server.known_hosts,
        "client_keys": [server.client_key],
        "agent_path": None,
    }
    return await ssh.connect(_conn_info(server), **(defaults | options))


async def _wait_for(event: threading.Event) -> bool:
    """Wait for an event that a fake sets on the server's thread."""
    return await asyncio.to_thread(event.wait, WAIT_SECONDS)


async def _hang(invocation: Invocation) -> None:
    """A fake that runs until the client goes away."""
    await invocation.stdin.read()


# Connecting


async def test_connect_keeps_alive_as_configured(
    ssh_server: SSHTestServer, monkeypatch: pytest.MonkeyPatch
):
    received: dict[str, object] = {}
    original_connect = asyncssh.connect

    async def recording_connect(host: str, **options: object):
        received.update(options)
        return await original_connect(host, **options)

    monkeypatch.setattr(asyncssh, "connect", recording_connect)

    async with await ssh.connect(
        _conn_info(ssh_server, keepalive_seconds=75),
        port=ssh_server.port,
        known_hosts=ssh_server.known_hosts,
        client_keys=[ssh_server.client_key],
        agent_path=None,
    ):
        pass

    assert received["keepalive_interval"] == 75
    assert received["keepalive_count_max"] == ssh.SERVER_ALIVE_COUNT_MAX == 3


async def test_connect_ignores_the_users_openssh_config(
    ssh_server: SSHTestServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    # Would send the connection through a command that doesn't exist.
    (home / ".ssh" / "config").write_text(
        "Host *\n    ProxyCommand ezhpcy-test-no-such-command %h %p\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    ssh_server.command("true", reply())

    async with await _connect(ssh_server) as host:
        assert await host.run(["true"]) == ""


# Running commands


async def test_run_quotes_arguments_and_returns_stdout(ssh_server: SSHTestServer):
    ssh_server.command("printf", reply(stdout="output\n", stderr="ignored\n"))
    args = ["printf", "%s\n", "two words", "it's", "$HOME; echo unsafe"]

    async with await _connect(ssh_server) as host:
        assert await host.run(args) == "output\n"

    assert ssh_server.commands_run == [args]


async def test_run_reads_large_output_on_both_streams(ssh_server: SSHTestServer):
    async def noisy(invocation: Invocation) -> None:
        invocation.stderr.write("e" * LARGE_OUTPUT_BYTES)
        invocation.stdout.write("o" * LARGE_OUTPUT_BYTES)
        await invocation.stderr.drain()
        await invocation.stdout.drain()

    ssh_server.command("noisy", noisy)

    async with await _connect(ssh_server) as host:
        output = await asyncio.wait_for(host.run(["noisy"]), 30)

    assert output == "o" * LARGE_OUTPUT_BYTES


async def test_run_sends_stdin(ssh_server: SSHTestServer):
    async def cat(invocation: Invocation) -> None:
        invocation.stdout.write(await invocation.stdin.read())

    ssh_server.command("cat", cat)

    async with await _connect(ssh_server) as host:
        assert await host.run(["cat"], stdin="#!/bin/bash\necho hi\n") == (
            "#!/bin/bash\necho hi\n"
        )


async def test_run_replaces_output_that_is_not_utf8(ssh_server: SSHTestServer):
    def latin1(invocation: Invocation) -> None:
        channel = cast(asyncssh.SSHServerChannel[bytes], invocation.process.channel)
        channel.set_encoding(None)
        channel.write(b"caf\xe9\n")
        channel.write_stderr(b"\xff\n")

    ssh_server.command("latin1", latin1)
    ssh_server.command("true", reply())

    async with await _connect(ssh_server) as host:
        assert await host.run(["latin1"]) == "caf�\n"
        # The connection is still usable.
        assert await host.run(["true"]) == ""


async def test_run_raises_process_error_with_stderr(ssh_server: SSHTestServer):
    ssh_server.command(
        "bjobs",
        reply(stdout="partial\n", stderr="  Job <42> is not found\n", exit_status=255),
    )

    async with await _connect(ssh_server) as host:
        with pytest.raises(asyncssh.ProcessError) as raised:
            await host.run(["bjobs", "42"])

    # The schedulers match on this message.
    assert str(raised.value) == "Remote command failed (255): Job <42> is not found"
    assert raised.value.reason == str(raised.value)
    assert raised.value.exit_status == 255
    assert raised.value.stdout == "partial\n"
    assert raised.value.stderr == "  Job <42> is not found\n"
    assert raised.value.command == "bjobs 42"


async def test_run_raises_when_a_signal_ends_the_command(ssh_server: SSHTestServer):
    def killed(invocation: Invocation) -> None:
        invocation.stderr.write("going down\n")
        invocation.process.exit_with_signal("KILL")

    ssh_server.command("worker", killed)

    async with await _connect(ssh_server) as host:
        with pytest.raises(asyncssh.ProcessError) as raised:
            await host.run(["worker"])

    assert str(raised.value) == "Remote command failed (signal KILL): going down"
    assert raised.value.exit_signal is not None


async def test_run_raises_when_the_connection_is_lost(ssh_server: SSHTestServer):
    started = threading.Event()

    async def hang(invocation: Invocation) -> None:
        started.set()
        await _hang(invocation)

    ssh_server.command("sleep", hang)
    ssh_server.command("true", reply())

    async with await _connect(ssh_server) as host:
        running = asyncio.create_task(host.run(["sleep", "3600"]))
        assert await _wait_for(started)
        # The server confirmed `sleep` before starting it, and packets arrive in
        # order, so once this returns the client knows `sleep` is running.
        # Dropped earlier, the client would get `ConnectionLost` from opening it.
        await host.run(["true"])
        ssh_server.drop()

        with pytest.raises(asyncssh.ProcessError) as raised:
            await asyncio.wait_for(running, WAIT_SECONDS)

    assert str(raised.value) == "Remote command failed (connection lost): "
    assert raised.value.exit_status is None


async def test_cancelling_run_closes_the_channel(ssh_server: SSHTestServer):
    channel_closed = threading.Event()

    async def hang(invocation: Invocation) -> None:
        await _hang(invocation)
        channel_closed.set()

    ssh_server.command("sleep", hang)

    async with await _connect(ssh_server) as host:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.2):
                await host.run(["sleep", "3600"])

        assert await _wait_for(channel_closed)
        # The connection is still usable.
        ssh_server.command("true", reply())
        assert await host.run(["true"]) == ""


async def test_run_pixi_uses_ezhpcy_xdg_directories(ssh_server: SSHTestServer):
    ssh_server.command("env", reply(stdout="pixi 0.1\n"))
    remote_state = RemoteState(
        cache_dir=PurePosixPath("/cache"), state_dir=PurePosixPath("/state")
    )

    async with await _connect(ssh_server) as host:
        output = await host.run_pixi(["exec", "--spec=openssh", "sshd"], remote_state)

    assert output == "pixi 0.1\n"
    assert ssh_server.commands_run == [
        [
            "env",
            f"PIXI_HOME=/cache/ezhpcy/{EZHPCY_VERSION}/pixi/{PIXI_VERSION}",
            f"PIXI_CACHE_DIR=/cache/ezhpcy/{EZHPCY_VERSION}/pixi_cache/{PIXI_VERSION}",
            f"/cache/ezhpcy/{EZHPCY_VERSION}/pixi/{PIXI_VERSION}/bin/pixi",
            "exec",
            "--spec=openssh",
            "sshd",
        ]
    ]


async def test_run_login_shell_quotes_the_nested_command(ssh_server: SSHTestServer):
    received: list[str] = []

    async def submit(invocation: Invocation) -> None:
        received.append(await invocation.stdin.read())
        invocation.stdout.write("Job <42> is submitted.\n")

    ssh_server.command("bash -lc", submit)
    command = ["bsub", "-J", "worker name", "single'quote", "$HOME; echo unsafe"]

    async with await _connect(ssh_server) as host:
        output = await host.run_login_shell(command, stdin="echo worker\n")

    assert output == "Job <42> is submitted.\n"
    assert received == ["echo worker\n"]
    [argv] = ssh_server.commands_run
    assert argv == ["bash", "-lc", shlex.join(command)]
    assert shlex.split(argv[2]) == command


async def test_start_login_shell_runs_under_a_terminal(ssh_server: SSHTestServer):
    async def shell(invocation: Invocation) -> None:
        width, height, *_ = invocation.process.get_terminal_size()
        invocation.stdout.write(f"{invocation.term_type} {width}x{height}\n")
        invocation.stderr.write("on stderr\n")
        invocation.stdout.write(f"got {await invocation.stdin.readline()}")

    ssh_server.command("bash -lc", shell)
    command = ["bsub", "-Is", "-J", "worker name", "$HOME; echo unsafe"]

    async with await _connect(ssh_server) as host:
        process = await host.start_login_shell(command)
        process.stdin.write(b"exec worker\n")
        output = await asyncio.wait_for(process.stdout.read(), WAIT_SECONDS)
        await process.wait_closed()

    assert output == b"vt100 80x24\non stderr\ngot exec worker\n"
    assert process.exit_status == 0
    [argv] = ssh_server.commands_run
    assert argv == ["bash", "-lc", shlex.join(command)]


async def test_remote_state_maps_the_xdg_directories(ssh_server: SSHTestServer):
    ssh_server.command(
        "bash -lc",
        reply(stdout='{"cache_dir":"/home/alice/.cache","state_dir":"/xdg/state"}'),
    )

    async with await _connect(ssh_server) as host:
        remote_state = await host.remote_state()

    assert remote_state == RemoteState(
        cache_dir=PurePosixPath("/home/alice/.cache"),
        state_dir=PurePosixPath("/xdg/state"),
    )
    script = ssh_server.commands_run[0][2]
    assert "XDG_CACHE_HOME" in script
    assert "XDG_STATE_HOME" in script


# SFTP


async def test_sftp_opens_an_sftp_session(ssh_server: SSHTestServer):
    async with await _connect(ssh_server) as host, host.sftp() as sftp:
        async with sftp.open("ready", "w") as remote_file:
            await remote_file.write("12345\n")
        assert await sftp.realpath(".") == "/home/alice"

    assert ssh_server.local_path("ready").read_text(encoding="utf-8") == "12345\n"


async def test_ensure_directory_creates_missing_parents(ssh_server: SSHTestServer):
    async with await _connect(ssh_server) as host, host.sftp() as sftp:
        await ssh.ensure_directory(sftp, PurePosixPath(".cache/ezhpcy/logs"))

    assert ssh_server.local_path(".cache/ezhpcy/logs").is_dir()


async def test_ensure_directory_leaves_an_existing_one_alone(
    ssh_server: SSHTestServer, monkeypatch: pytest.MonkeyPatch
):
    ssh_server.local_path(".cache/ezhpcy").mkdir(parents=True)
    makedirs_calls: list[object] = []

    async def makedirs(_sftp: object, path: object, **_options: object) -> None:
        makedirs_calls.append(path)

    monkeypatch.setattr(asyncssh.SFTPClient, "makedirs", makedirs)

    async with await _connect(ssh_server) as host, host.sftp() as sftp:
        await ssh.ensure_directory(sftp, PurePosixPath(".cache/ezhpcy"))

    # `makedirs` would try every component from `/`.
    assert makedirs_calls == []


async def test_ensure_directory_refuses_a_file(ssh_server: SSHTestServer):
    ssh_server.local_path(".cache").mkdir(parents=True)
    ssh_server.local_path(".cache/ezhpcy").write_text("", encoding="utf-8")

    async with await _connect(ssh_server) as host, host.sftp() as sftp:
        with pytest.raises(asyncssh.SFTPError, match="not a directory"):
            await ssh.ensure_directory(sftp, PurePosixPath(".cache/ezhpcy"))


# Forwarding


async def test_open_tcp_reaches_a_port_from_the_login_node(ssh_server: SSHTestServer):
    async def banner(
        _reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        writer.write(b"SSH-2.0-worker\r\n")
        await writer.drain()
        writer.close()

    worker = await asyncio.start_server(banner, "127.0.0.1", 0)
    port = worker.sockets[0].getsockname()[1]
    ssh_server.allow_forwarding(port)
    try:
        async with await _connect(ssh_server) as host:
            reader, writer = await host.open_tcp("127.0.0.1", port)
            assert await reader.readline() == b"SSH-2.0-worker\r\n"
            writer.close()
    finally:
        worker.close()
        await worker.wait_closed()


async def _echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Echo until the client's EOF, then close."""
    while data := await reader.read(64 * 1024):
        writer.write(data)
        await writer.drain()
    writer.close()


@asynccontextmanager
async def _destination(
    ssh_server: SSHTestServer, handler: StreamHandler, port: int = 0
) -> AsyncGenerator[int]:
    """Serve `handler` on a loopback port that the login node may forward to."""
    server = await asyncio.start_server(handler, LOOPBACK_HOST, port)
    port = server.sockets[0].getsockname()[1]
    ssh_server.allow_forwarding(port)
    try:
        yield port
    finally:
        server.close()
        server.close_clients()
        await server.wait_closed()


def _closed_port() -> int:
    """A loopback port that nothing listens on."""
    with socket.socket() as probe:
        probe.bind((LOOPBACK_HOST, 0))
        return probe.getsockname()[1]


async def _open(
    listener: asyncssh.SSHListener,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection(LOOPBACK_HOST, listener.get_port())


async def _round_trip(
    listener: asyncssh.SSHListener, payload: bytes = b"ping\n"
) -> bytes:
    """Send `payload` and EOF through `listener`, and return everything sent back."""
    reader, writer = await _open(listener)
    writer.write(payload)
    writer.write_eof()
    try:
        return await asyncio.wait_for(reader.read(), WAIT_SECONDS)
    finally:
        writer.close()


async def _refused(listener: asyncssh.SSHListener) -> bool:
    """
    Whether `listener` closes a connection without forwarding it. The client sees
    EOF, or a reset when the socket was closed before reading what it sent.
    """
    try:
        return await _round_trip(listener) == b""
    except ConnectionResetError:
        return True


async def test_forward_local_port_listens_on_loopback(ssh_server: SSHTestServer):
    async with (
        _destination(ssh_server, _echo) as port,
        await _connect(ssh_server) as host,
    ):
        listener = await host.forward_local_port(LOOPBACK_HOST, port)
        _reader, writer = await _open(listener)
        try:
            assert writer.get_extra_info("peername")[0] == LOOPBACK_HOST
        finally:
            writer.close()

    assert listener.get_port() not in {0, port}


async def test_forward_local_port_listens_on_no_other_address() -> None:
    """
    Connecting through loopback can't tell `127.0.0.1` from `0.0.0.0`, and
    asyncssh's listener doesn't say what it is bound to, so check what it is asked.
    """
    asked: list[tuple[object, ...]] = []

    class Connection:
        async def forward_local_port(self, *args: object, **_kwargs: object) -> None:
            asked.append(args)

    host = RemoteHost(
        cast(asyncssh.SSHClientConnection, Connection()),
        ConnectionInfo.model_construct(user="alice", host="login.example.com"),
        asyncio.get_running_loop().create_future(),
    )

    await host.forward_local_port("n-62-11-19", 2222)

    # A port the OS picks, on loopback only.
    assert asked == [("127.0.0.1", 0, "n-62-11-19", 2222)]


async def test_forward_local_port_passes_on_the_clients_eof(
    ssh_server: SSHTestServer,
):
    payload = bytes(range(256)) * 4096
    async with (
        _destination(ssh_server, _echo) as port,
        await _connect(ssh_server) as host,
    ):
        listener = await host.forward_local_port(LOOPBACK_HOST, port)
        # The destination only replies in full, and closes, after the client's EOF.
        assert await _round_trip(listener, payload) == payload


async def test_forward_local_port_passes_on_the_destinations_eof(
    ssh_server: SSHTestServer,
):
    received: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

    async def banner_then_read(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        writer.write(b"SSH-2.0-worker\r\n")
        writer.write_eof()
        received.set_result(await reader.read())
        writer.close()

    async with (
        _destination(ssh_server, banner_then_read) as port,
        await _connect(ssh_server) as host,
    ):
        listener = await host.forward_local_port(LOOPBACK_HOST, port)
        reader, writer = await _open(listener)
        try:
            assert await asyncio.wait_for(reader.read(), WAIT_SECONDS) == (
                b"SSH-2.0-worker\r\n"
            )
            # The client can still send after the destination's EOF.
            writer.write(b"after EOF")
            writer.write_eof()
            assert await asyncio.wait_for(received, WAIT_SECONDS) == b"after EOF"
        finally:
            writer.close()


async def test_forward_local_port_serves_concurrent_connections(
    ssh_server: SSHTestServer,
):
    payloads = [f"connection {index}\n".encode() * 1000 for index in range(50)]
    async with (
        _destination(ssh_server, _echo) as port,
        await _connect(ssh_server) as host,
    ):
        listener = await host.forward_local_port(LOOPBACK_HOST, port)
        replies = await asyncio.gather(
            *(_round_trip(listener, payload) for payload in payloads)
        )

    assert replies == payloads


async def test_forward_local_port_survives_a_refusing_destination(
    ssh_server: SSHTestServer,
):
    port = _closed_port()
    ssh_server.allow_forwarding(port)
    async with await _connect(ssh_server) as host:
        listener = await host.forward_local_port(LOOPBACK_HOST, port)
        # The channel can't be opened, so the local connection is closed.
        assert await _refused(listener)

        async with _destination(ssh_server, _echo, port):
            assert await _round_trip(listener) == b"ping\n"


async def test_forward_local_port_calls_the_accept_handler(
    ssh_server: SSHTestServer,
):
    clients: list[tuple[str, int]] = []

    def accept(client_host: str, client_port: int) -> bool:
        clients.append((client_host, client_port))
        return len(clients) == 1

    async with (
        _destination(ssh_server, _echo) as port,
        await _connect(ssh_server) as host,
    ):
        listener = await host.forward_local_port(
            LOOPBACK_HOST, port, accept_handler=accept
        )
        reader, writer = await _open(listener)
        client = writer.get_extra_info("sockname")[:2]
        writer.write(b"ping\n")
        writer.write_eof()
        assert await asyncio.wait_for(reader.read(), WAIT_SECONDS) == b"ping\n"
        writer.close()
        # Refused by the handler: closed without reaching the destination.
        assert await _refused(listener)

    assert clients[0] == client
    assert len(clients) == 2


async def test_closing_the_forwarded_port_keeps_open_connections(
    ssh_server: SSHTestServer,
):
    async with (
        _destination(ssh_server, _echo) as port,
        await _connect(ssh_server) as host,
    ):
        listener = await host.forward_local_port(LOOPBACK_HOST, port)
        reader, writer = await _open(listener)
        writer.write(b"before\n")
        assert await asyncio.wait_for(reader.readline(), WAIT_SECONDS) == b"before\n"

        listener.close()

        with pytest.raises(ConnectionRefusedError):
            await asyncio.wait_for(_open(listener), WAIT_SECONDS)
        # asyncssh leaves open connections alone, and `wait_closed` waits for
        # them, as `asyncio.Server.wait_closed` does.
        waiting = asyncio.create_task(listener.wait_closed())
        writer.write(b"after\n")
        assert await asyncio.wait_for(reader.readline(), WAIT_SECONDS) == b"after\n"
        assert not waiting.done()
        writer.close()
        await asyncio.wait_for(waiting, WAIT_SECONDS)


async def test_losing_the_login_connection_closes_the_forwarded_port(
    ssh_server: SSHTestServer,
):
    async with (
        _destination(ssh_server, _echo) as port,
        await _connect(ssh_server) as host,
    ):
        listener = await host.forward_local_port(LOOPBACK_HOST, port)
        reader, writer = await _open(listener)
        writer.write(b"before\n")
        assert await asyncio.wait_for(reader.readline(), WAIT_SECONDS) == b"before\n"

        ssh_server.drop()
        await asyncio.wait_for(host.closed, WAIT_SECONDS)

        # Open connections are closed, and so is the listener.
        assert await asyncio.wait_for(reader.read(), WAIT_SECONDS) == b""
        writer.close()
        await asyncio.wait_for(listener.wait_closed(), WAIT_SECONDS)
        with pytest.raises(ConnectionRefusedError):
            await asyncio.wait_for(_open(listener), WAIT_SECONDS)


# Connection loss


async def test_closed_on_purpose_is_not_a_loss(ssh_server: SSHTestServer):
    host = await _connect(ssh_server)
    assert not host.closed.done()

    await host.close()

    assert await asyncio.wait_for(host.closed, WAIT_SECONDS) is None


async def test_closed_reports_a_dropped_connection(ssh_server: SSHTestServer):
    async with await _connect(ssh_server) as host:
        ssh_server.drop()

        error = await asyncio.wait_for(host.closed, WAIT_SECONDS)

    assert isinstance(error, asyncssh.ConnectionLost)


async def test_closed_reports_unanswered_keepalives(ssh_server: SSHTestServer):
    async with await _connect(
        ssh_server, keepalive_interval=0.1, keepalive_count_max=2
    ) as host:
        # Keepalives are answered while the network works.
        await asyncio.sleep(0.5)
        assert not host.closed.done()

        ssh_server.blackhole()

        error = await asyncio.wait_for(host.closed, WAIT_SECONDS)

    assert isinstance(error, asyncssh.ConnectionLost)
    assert "keepalive" in str(error)
