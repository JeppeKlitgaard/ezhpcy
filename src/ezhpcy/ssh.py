"""
SSH connections to hosts: the login node's, and the worker's made through it.

Covers connecting to the login node, quoting remote commands, login shells and
pixi helpers, forwarding loopback ports to destinations that the connection's
far end reaches, and remote loopback ports to destinations on this machine.
What is attached to a connection is closed before it (`RemoteHost.attachments`).
"""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import PurePosixPath
lazy import asyncio
lazy import shlex
lazy from collections.abc import AsyncGenerator, Callable, Coroutine
lazy from types import TracebackType
lazy from typing import Self, cast

import asyncssh

from ezhpcy.logging import get_logger
from ezhpcy.messages import MessageError
lazy from ezhpcy.attachments import Attachments
lazy from ezhpcy.constants import LOOPBACK_HOST, WORKER_HOST_ALIAS
lazy from ezhpcy.process import ProcessWatcher
lazy from ezhpcy.provision_local import WorkerCredentials
lazy from ezhpcy.types import Login, RemoteState

logger = get_logger(__name__)

# Keepalive probes that may go unanswered before the connection counts as lost,
# as OpenSSH's ServerAliveCountMax.
SERVER_ALIVE_COUNT_MAX = 3


@dataclass(frozen=True, slots=True)
class TCPDestination:
    """A TCP port, reached over a `direct-tcpip` channel."""

    host: str
    port: int

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(frozen=True, slots=True)
class SocketDestination:
    """A Unix socket, reached over a `direct-streamlocal@openssh.com` channel."""

    path: PurePosixPath

    def __str__(self) -> str:
        return str(self.path)


type Destination = TCPDestination | SocketDestination


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


class CommandFailed(MessageError, RuntimeError):
    """
    A remote command didn't exit with status 0: it failed, a signal ended it, or
    the connection was lost before it said how it ended. The message ends with
    its stderr, which says why, e.g. when the Pixi install fails.
    """

    def __init__(
        self,
        command: str,
        *,
        exit_status: int | None,
        exit_signal: str | None,
        stdout: str,
        stderr: str,
    ) -> None:
        if exit_signal is not None:
            status = f"signal {exit_signal}"
        elif exit_status is None:
            status = "connection lost"
        else:
            status = str(exit_status)
        super().__init__(t"Remote command failed ({status:detail}): {stderr.strip()}")
        # As run, quoted.
        self.command = command
        # None if a signal ended it, or the connection was lost first.
        self.exit_status = exit_status
        # The signal's name, if one ended it.
        self.exit_signal = exit_signal
        self.stdout = stdout
        self.stderr = stderr


def _command_failed(result: asyncssh.SSHCompletedProcess) -> CommandFailed:
    """The error for a command that didn't exit with status 0."""
    return CommandFailed(
        str(result.command),
        exit_status=result.exit_status,
        exit_signal=result.exit_signal[0] if result.exit_signal else None,
        stdout=str(result.stdout or ""),
        stderr=str(result.stderr or ""),
    )


class RemoteHost:
    """
    A connection to a host: the login node, or the worker's sshd through it.

    What is attached to it (`attachments`: processes, forwarded ports) is closed,
    newest first, when it is closed, before the connection.
    """

    def __init__(
        self,
        conn: asyncssh.SSHClientConnection,
        login: Login,
        closed: asyncio.Future[Exception | None],
    ) -> None:
        # The login node this connection is to, or through.
        self.login = login
        self._conn = conn
        self._closed = closed
        self.attachments = Attachments()
        # Watches the processes started on the host, once one is (see
        # `ezhpcy.process`).
        self.process_watcher: ProcessWatcher | None = None

    @property
    def closed(self) -> asyncio.Future[Exception | None]:
        """
        Resolves when the connection ends: to the exception that ended it, or to
        `None` when it was closed on purpose.
        """
        return self._closed

    async def close(self) -> None:
        """Close what is attached to the connection, newest first, then it."""
        await self.attachments.close()
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

        Raises `CommandFailed`, carrying stderr, unless the command exits with
        status 0; also when the connection is lost or a signal kills it.
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

    async def open_unix(
        self, path: PurePosixPath
    ) -> tuple[asyncssh.SSHReader[bytes], asyncssh.SSHWriter[bytes]]:
        """
        Open a connection to a Unix socket on the remote host
        (`direct-streamlocal@openssh.com`).
        """
        return await self._conn.open_unix_connection(str(path))

    async def open(
        self, destination: Destination
    ) -> tuple[asyncssh.SSHReader[bytes], asyncssh.SSHWriter[bytes]]:
        """Open a connection to `destination`, by its type of channel."""
        match destination:
            case TCPDestination(host=host, port=port):
                return await self.open_tcp(host, port)
            case SocketDestination(path=path):
                return await self.open_unix(path)

    async def forward_local_port(
        self,
        destination: Destination,
        *,
        local_port: int = 0,
        on_connections: Callable[[int], object] | None = None,
    ) -> ForwardedPort:
        """
        Listen on a loopback port, `local_port` or by default a free one, and
        forward each connection to it to `destination` from the remote host, over
        a channel of its own. `on_connections` is called with the number of open
        connections whenever it changes.

        Closing the port refuses new connections, and leaves open ones to end by
        themselves, or with the connection, which also closes the port.
        """
        return await ForwardedPort.open(
            self._conn,
            destination,
            on_connections,
            local_port=local_port,
            login_closed=self._closed,
        )

    async def forward_remote_port(
        self,
        remote_port: int,
        destination: Destination,
        *,
        on_connections: Callable[[int], object] | None = None,
    ) -> RemoteForwardedPort:
        """
        Have the remote host listen on its loopback port `remote_port` (0: a free
        one), and forward each connection to it to `destination` from this
        machine, over a channel of its own. `on_connections` is called with the
        number of open connections whenever it changes.

        Raises `asyncssh.ChannelListenError` if the host refuses to listen.
        Closing the port refuses new connections, and leaves open ones to end by
        themselves, or with the connection, which also closes the port.
        """
        return await RemoteForwardedPort.open(
            self._conn, remote_port, destination, on_connections
        )

    async def connect_worker(
        self, host: str, port: int, credentials: WorkerCredentials
    ) -> RemoteHost:
        """
        Connect to the worker's sshd at `host:port`, through this connection (the
        inner connection), as `ssh <alias>` does through a tunnel: with the worker
        client key, checking the pinned host key under `WORKER_HOST_ALIAS`.
        Keepalive follows this connection's.
        """
        client = SSHClient()
        conn = await self._conn.connect_ssh(
            host,
            port,
            client_factory=lambda: client,
            username=credentials.login_user,
            client_keys=[str(credentials.client_key)],
            known_hosts=str(credentials.known_hosts),
            host_key_alias=WORKER_HOST_ALIAS,
            # Only the worker's key is offered, as `IdentitiesOnly yes` does.
            agent_path=None,
            config=None,
            keepalive_interval=self.login.keepalive_interval.total_seconds(),
            keepalive_count_max=SERVER_ALIVE_COUNT_MAX,
        )
        # The worker runs nothing that needs a password, so the login connection's
        # isn't passed on.
        return RemoteHost(conn, self.login, client.closed)

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
    A loopback port whose connections are forwarded to `destination` from the
    connection's far end (see `RemoteHost.forward_local_port`).

    Forwarded as asyncssh's own `forward_local_port` does, with its
    `SSHForwarder` at either end, but on a listener of our own, as asyncssh's
    says nothing when a connection ends.

    Only required because we want to keep track of number of open connections.
    """

    def __init__(
        self,
        conn: asyncssh.SSHClientConnection,
        destination: Destination,
        on_connections: Callable[[int], object] | None,
    ) -> None:
        self.conn = conn
        self.destination = destination
        self.port = 0
        self.connections = _Connections(on_connections)
        self._server: asyncio.Server | None = None
        # Kept, as the event loop keeps only weak references to tasks.
        self._channels_opening: set[asyncio.Task[None]] = set()

    @classmethod
    async def open(
        cls,
        conn: asyncssh.SSHClientConnection,
        destination: Destination,
        on_connections: Callable[[int], object] | None,
        *,
        local_port: int = 0,
        login_closed: asyncio.Future[Exception | None],
    ) -> ForwardedPort:
        """
        Listen on loopback port `local_port` (0: a free one), until the connection
        closes.
        """
        forwarded = cls(conn, destination, on_connections)
        server = forwarded._server = await asyncio.get_running_loop().create_server(
            lambda: _Forwarder(forwarded), LOOPBACK_HOST, local_port
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
        self.connections.opened(client, self.destination)

    def connection_closed(self, client: str) -> None:
        self.connections.closed(client)


class RemoteForwardedPort:
    """
    A loopback port on the remote host whose connections are forwarded to
    `destination` from this machine (see `RemoteHost.forward_remote_port`).

    Forwarded as asyncssh's own `forward_remote_port` does, with its
    `SSHForwarder` at either end, but counting the open connections.
    """

    def __init__(
        self, destination: Destination, on_connections: Callable[[int], object] | None
    ) -> None:
        self.destination = destination
        self.port = 0
        self.connections = _Connections(on_connections)
        self._listener: asyncssh.SSHListener | None = None

    @classmethod
    async def open(
        cls,
        conn: asyncssh.SSHClientConnection,
        remote_port: int,
        destination: Destination,
        on_connections: Callable[[int], object] | None,
    ) -> RemoteForwardedPort:
        forwarded = cls(destination, on_connections)
        # Loopback, explicitly: OpenSSH's sshd binds every address for "" (with
        # `GatewayPorts yes`), and the worker's binds loopback anyway.
        listener = forwarded._listener = await conn.create_server(
            forwarded._accept, LOOPBACK_HOST, remote_port
        )
        forwarded.port = listener.get_port()
        return forwarded

    def close(self) -> None:
        """Refuse new connections; open ones stay open."""
        if self._listener is not None:
            self._listener.close()

    async def _accept(
        self, client_host: str, client_port: int
    ) -> asyncssh.SSHTCPSession[bytes]:
        """Connect to the destination for a new connection on the remote port."""
        client = f"{client_host}:{client_port}"
        loop = asyncio.get_running_loop()
        try:
            match self.destination:
                case TCPDestination(host=host, port=port):
                    _, local_end = await loop.create_connection(
                        lambda: _CountedForwarder(self.connections, client), host, port
                    )
                case SocketDestination(path=path):
                    _, local_end = await loop.create_unix_connection(
                        lambda: _CountedForwarder(self.connections, client), str(path)
                    )
        except OSError as error:
            logger.debug(
                t"Could not forward connection: client={client:endpoint} "
                t"destination={self.destination!s:endpoint} error={error!r}"
            )
            raise asyncssh.ChannelOpenError(
                asyncssh.OPEN_CONNECT_FAILED, str(error)
            ) from None
        self.connections.opened(client, self.destination)
        # The channel's end, as asyncssh's own `forward_connection` makes it.
        return cast(asyncssh.SSHTCPSession[bytes], asyncssh.SSHForwarder(local_end))


class _Connections:
    """
    How many connections are open through a forwarded port, logged and passed
    to `on_change` as they open and close.
    """

    def __init__(self, on_change: Callable[[int], object] | None) -> None:
        self.count = 0
        self._on_change = on_change

    def opened(self, client: str, destination: Destination) -> None:
        self._change(+1)
        logger.debug(
            t"Forwarded connection opened: client={client:endpoint} "
            t"destination={destination!s:endpoint} open={self.count:detail}"
        )

    def closed(self, client: str) -> None:
        self._change(-1)
        logger.debug(
            t"Forwarded connection closed: client={client:endpoint} "
            t"open={self.count:detail}"
        )

    def _change(self, change: int) -> None:
        self.count += change
        if self._on_change is not None:
            self._on_change(self.count)


class _CountedForwarder(asyncssh.SSHForwarder):
    """
    This machine's end of one connection to a remote forwarded port, which
    counts it closed when it closes.
    """

    def __init__(self, connections: _Connections, client: str) -> None:
        super().__init__()
        self._connections = connections
        self._client = client
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._connections.closed(self._client)
        super().close()


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

        def new_channel_end() -> asyncssh.SSHForwarder:
            nonlocal channel_end
            channel_end = asyncssh.SSHForwarder(self)
            return channel_end

        # The channel's session, as asyncssh's own forwarders make it (its
        # `SSHLocalForwarder`), though `SSHForwarder` isn't typed as one.
        conn = self._forwarded.conn
        try:
            match self._forwarded.destination:
                case TCPDestination(host=host, port=port):
                    await conn.create_connection(
                        lambda: cast(asyncssh.SSHTCPSession[bytes], new_channel_end()),
                        host,
                        port,
                        client_host,
                        client_port,
                    )
                case SocketDestination(path=path):
                    await conn.create_unix_connection(
                        lambda: cast(asyncssh.SSHUNIXSession[bytes], new_channel_end()),
                        str(path),
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
