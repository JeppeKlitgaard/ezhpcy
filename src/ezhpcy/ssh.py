"""
The login-node connection.

Covers connecting to login node, quoting remote commands, login shells and pixi helpers.
"""

from contextlib import asynccontextmanager
lazy import asyncio
lazy import shlex
lazy from collections.abc import AsyncGenerator, Callable, Coroutine
lazy from pathlib import PurePosixPath
lazy from types import TracebackType
lazy from typing import Self, cast

import asyncssh

from ezhpcy.logging import get_logger
lazy from ezhpcy.constants import LOOPBACK_HOST
lazy from ezhpcy.types import Login, RemoteState

logger = get_logger(__name__)

# Keepalive probes that may go unanswered before the connection counts as lost,
# as OpenSSH's ServerAliveCountMax.
SERVER_ALIVE_COUNT_MAX = 3


class SSHClient(asyncssh.SSHClient):
    """
    Records how its connection ended in `closed`.
    """

    def __init__(self) -> None:
        self.closed: asyncio.Future[Exception | None] = (
            asyncio.get_running_loop().create_future()
        )

    def connection_lost(self, exc: Exception | None) -> None:
        if not self.closed.done():
            self.closed.set_result(exc)


async def connect(
    login: Login, *, client: SSHClient | None = None, **options: object
) -> RemoteHost:
    """
    Connect to the login node. Keepalive follows `login`, and ~/.ssh/config is
    never read. `options` are passed on to `asyncssh.connect`, overriding these
    defaults. See `ezhpcy.login` for passwords and host keys.
    """
    client = client or SSHClient()
    defaults: dict[str, object] = {
        "username": login.user,
        "port": login.port,
        # Never apply the user's OpenSSH configuration to the login connection:
        # it may hold EzHPCy's own Host blocks, which point at running tunnels.
        "config": None,
        "keepalive_interval": login.keepalive_interval.total_seconds(),
        "keepalive_count_max": SERVER_ALIVE_COUNT_MAX,
    }
    conn = await asyncssh.connect(
        login.host, client_factory=lambda: client, **(defaults | options)
    )
    return RemoteHost(conn, login, client.closed)


async def ensure_directory(sftp: asyncssh.SFTPClient, path: PurePosixPath) -> None:
    """
    Create a directory and its parents, unless it exists already. That costs one
    round trip, where `SFTPClient.makedirs` tries every component from `/`.
    """
    if not await sftp.isdir(path):
        await sftp.makedirs(path, exist_ok=True)


def _command_failed(result: asyncssh.SSHCompletedProcess) -> asyncssh.ProcessError:
    """The error for a command that didn't exit with status 0."""
    stderr = str(result.stderr or "").strip()
    if result.exit_signal:
        status = f"signal {result.exit_signal[0]}"
    elif result.exit_status is None:
        # The connection went away before the command reported how it ended.
        status = "connection lost"
    else:
        status = str(result.exit_status)
    error = asyncssh.ProcessError(
        result.env,
        result.command,
        result.subsystem,
        result.exit_status,
        result.exit_signal,
        result.returncode,
        result.stdout or "",
        result.stderr or "",
    )
    # asyncssh replaces any reason given to ProcessError with its own, which
    # leaves out stderr. Users need stderr, e.g. when the Pixi install fails.
    error.reason = f"Remote command failed ({status}): {stderr}"
    error.args = (error.reason,)
    return error


class RemoteHost:
    """A connection to the login node."""

    def __init__(
        self,
        conn: asyncssh.SSHClientConnection,
        login: Login,
        closed: asyncio.Future[Exception | None],
    ) -> None:
        # The login node this connection is to.
        self.login = login
        self._conn = conn
        self._closed = closed

    @property
    def closed(self) -> asyncio.Future[Exception | None]:
        """
        Resolves when the connection ends: to the exception that ended it, or to
        `None` when it was closed on purpose.
        """
        return self._closed

    async def close(self) -> None:
        self._conn.close()
        await self._conn.wait_closed()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def run(self, args: list[str], *, stdin: str | None = None) -> str:
        """
        Run a command, safely quoted, and return its stdout. `stdin` is sent to
        the command, followed by end-of-file.

        Raises `asyncssh.ProcessError`, carrying stderr, unless the command exits
        with status 0; also when the connection is lost or a signal kills it.
        If the command can't be started, asyncssh's own error (such as
        `ConnectionLost` or `ChannelOpenError`) propagates.

        To bound how long it may take, wrap the call in `asyncio.timeout`.
        """
        # Bytes that aren't UTF-8 are replaced: asyncssh would otherwise drop
        # the whole connection, not just fail this command.
        process = await self._conn.create_process(
            shlex.join(args), input=stdin, errors="replace"
        )
        try:
            # Reads stdout and stderr together, so neither can fill up and
            # stall the command.
            result = await process.wait()
        finally:
            process.close()
        if result.exit_status != 0:
            raise _command_failed(result)
        return str(result.stdout)

    async def run_pixi(self, args: list[str], remote_state: RemoteState) -> str:
        """Run the pinned private Pixi with EzHPCy's XDG-resolved cache."""
        return await self.run(list(remote_state.pixi_command(*args)))

    async def run_login_shell(
        self, args: list[str], *, stdin: str | None = None
    ) -> str:
        """Run a safely quoted command through the remote Bash login shell."""
        return await self.run(["bash", "-lc", shlex.join(args)], stdin=stdin)

    async def start_login_shell(
        self, args: list[str]
    ) -> asyncssh.SSHClientProcess[bytes]:
        """
        Start a safely quoted command through the remote Bash login shell, with a
        pseudo-terminal, so its stdout carries stderr too. Reads and writes bytes.
        """
        return await self._conn.create_process(
            shlex.join(["bash", "-lc", shlex.join(args)]),
            term_type="vt100",
            term_size=(80, 24),  # asyncssh default seems to be (0, 0).
            encoding=None,
        )

    @asynccontextmanager
    async def sftp(self) -> AsyncGenerator[asyncssh.SFTPClient]:
        """Open an SFTP session."""
        async with self._conn.start_sftp_client() as sftp:
            yield sftp

    async def open_tcp(
        self, host: str, port: int
    ) -> tuple[asyncssh.SSHReader[bytes], asyncssh.SSHWriter[bytes]]:
        """Open a TCP connection from the login node (`direct-tcpip`)."""
        return await self._conn.open_connection(host, port)

    async def forward_local_port(
        self,
        host: str,
        port: int,
        *,
        on_connections: Callable[[int], object] | None = None,
    ) -> ForwardedPort:
        """
        Listen on a free loopback port, and forward each connection to it to
        `host:port` from the login node, over its own `direct-tcpip` channel.
        `on_connections` is called with the number of open connections whenever
        it changes.

        Closing the port refuses new connections, and leaves open ones to end by
        themselves, or with the login connection, which also closes the port.
        """
        return await ForwardedPort.open(
            self._conn, (host, port), on_connections, login_closed=self._closed
        )

    async def remote_state(self) -> RemoteState:
        """
        Discover the state of the remote host.

        Respects XDG directory specifications.
        """

        cmd = (
            'printf \'{"cache_dir":"%s","state_dir":"%s"}\' '
            '"${XDG_CACHE_HOME:-$HOME/.cache}" "${XDG_STATE_HOME:-$HOME/.local/state}"'
        )
        raw = (await self.run(["bash", "-lc", cmd])).strip()

        remote_state = RemoteState.model_validate_json(raw, strict=True)
        logger.debug(t"Discovered remote state: {remote_state:detail}")
        return remote_state


class ForwardedPort:
    """
    A loopback port whose connections are forwarded to `destination` (host, port)
    from the login node (see `RemoteHost.forward_local_port`).

    Forwarded as asyncssh's own `forward_local_port` does, with its
    `SSHForwarder` at either end, but on a listener of our own, as asyncssh's
    says nothing when a connection ends.

    Only required because we want to keep track of number of open connections.
    """

    def __init__(
        self,
        conn: asyncssh.SSHClientConnection,
        destination: tuple[str, int],
        on_connections: Callable[[int], object] | None,
    ) -> None:
        self.conn = conn
        self.destination = destination
        self.port = 0
        self._connections = 0
        self._on_connections = on_connections
        self._server: asyncio.Server | None = None
        # Kept, as the event loop keeps only weak references to tasks.
        self._channels_opening: set[asyncio.Task[None]] = set()

    @classmethod
    async def open(
        cls,
        conn: asyncssh.SSHClientConnection,
        destination: tuple[str, int],
        on_connections: Callable[[int], object] | None,
        *,
        login_closed: asyncio.Future[Exception | None],
    ) -> ForwardedPort:
        """Listen on a free loopback port, until the login connection closes."""
        forwarded = cls(conn, destination, on_connections)
        server = forwarded._server = await asyncio.get_running_loop().create_server(
            lambda: _Forwarder(forwarded), LOOPBACK_HOST, 0
        )
        login_closed.add_done_callback(lambda _closed: server.close())
        forwarded.port = server.sockets[0].getsockname()[1]
        return forwarded

    def close(self) -> None:
        """Refuse new connections; open ones stay open."""
        if self._server is not None:
            self._server.close()

    def start_opening(self, opening: Coroutine[object, object, None]) -> None:
        task = asyncio.create_task(opening)
        self._channels_opening.add(task)
        task.add_done_callback(self._channels_opening.discard)

    def connection_opened(self, client: str) -> None:
        self._count(+1)
        logger.debug(
            t"Forwarded connection opened: client={client:endpoint} "
            t"destination={self._endpoint:endpoint} open={self._connections:detail}"
        )

    def connection_closed(self, client: str) -> None:
        self._count(-1)
        logger.debug(
            t"Forwarded connection closed: client={client:endpoint} "
            t"open={self._connections:detail}"
        )

    @property
    def _endpoint(self) -> str:
        host, port = self.destination
        return f"{host}:{port}"

    def _count(self, change: int) -> None:
        self._connections += change
        if self._on_connections is not None:
            self._on_connections(self._connections)


class _Forwarder(asyncssh.SSHForwarder):
    """
    The local end of one forwarded connection, which, once its channel is open,
    exchanges data with the channel's end, an `SSHForwarder` too.
    """

    def __init__(self, forwarded: ForwardedPort) -> None:
        super().__init__()
        self._forwarded = forwarded
        self._client = ""
        self._closed = False

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        super().connection_made(transport)
        host, port = transport.get_extra_info("peername")[:2]
        self._client = f"{host}:{port}"
        # Until the channel is open, what the client sends waits in its socket.
        self.pause_reading()
        self._forwarded.connection_opened(self._client)
        self._forwarded.start_opening(self._open_channel(host, port))

    async def _open_channel(self, client_host: str, client_port: int) -> None:
        channel_end: asyncssh.SSHForwarder | None = None

        def session_factory() -> asyncssh.SSHTCPSession[bytes]:
            nonlocal channel_end
            channel_end = asyncssh.SSHForwarder(self)
            # The channel's session, as asyncssh's own forwarding makes it, though
            # `SSHForwarder` isn't typed as one.
            return cast(asyncssh.SSHTCPSession[bytes], channel_end)

        try:
            await self._forwarded.conn.create_connection(
                session_factory, *self._forwarded.destination, client_host, client_port
            )
        except (asyncssh.Error, OSError) as error:
            logger.debug(
                t"Could not forward connection: client={self._client:endpoint} "
                t"error={error!r}"
            )
            self.close()
            return
        if not self._closed:
            self.resume_reading()
        # The client hung up meanwhile.
        elif channel_end is not None:
            channel_end.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._forwarded.connection_closed(self._client)
        super().close()
