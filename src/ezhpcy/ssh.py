"""
The login-node connection.

Covers connecting to login node, quoting remote commands, login shells and pixi helpers.
"""

from contextlib import asynccontextmanager
lazy import asyncio
lazy import shlex
lazy from collections.abc import AsyncGenerator, Callable
lazy from pathlib import PurePosixPath
lazy from types import TracebackType
lazy from typing import Self

import asyncssh

from ezhpcy.logging import get_logger
lazy from ezhpcy.constants import LOOPBACK_HOST
lazy from ezhpcy.types import ConnectionInfo, RemoteState

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
    conn_info: ConnectionInfo, *, client: SSHClient | None = None, **options: object
) -> RemoteHost:
    """
    Connect to the login node. Keepalive follows `conn_info`, and ~/.ssh/config
    is never read. `options` are passed on to `asyncssh.connect`, overriding
    these defaults.
    """
    client = client or SSHClient()
    defaults: dict[str, object] = {
        "username": conn_info.user,
        # Never apply the user's OpenSSH configuration to the login connection:
        # it may hold EzHPCy's own Host blocks, which point at running tunnels.
        "config": None,
        "keepalive_interval": conn_info.ssh_keepalive_interval_seconds,
        "keepalive_count_max": SERVER_ALIVE_COUNT_MAX,
    }
    conn = await asyncssh.connect(
        str(conn_info.host), client_factory=lambda: client, **(defaults | options)
    )
    return RemoteHost(conn, conn_info, client.closed)


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
        conn_info: ConnectionInfo,
        closed: asyncio.Future[Exception | None],
    ) -> None:
        self.conn_info = conn_info
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
        accept_handler: Callable[[str, int], bool] | None = None,
    ) -> asyncssh.SSHListener:
        """
        Listen on a free loopback port, and forward each connection to it to
        `host:port` from the login node, over its own `direct-tcpip` channel.
        `accept_handler` is called with each client's address; a false return
        refuses the connection. The port is `listener.get_port()`.
        """
        return await self._conn.forward_local_port(
            LOOPBACK_HOST, 0, host, port, accept_handler=accept_handler
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
