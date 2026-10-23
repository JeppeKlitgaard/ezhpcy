"""
An in-process SSH server for tests, built on asyncssh.

The server runs on its own event loop in a background thread. Cyclopts runs an
`async def` command with `asyncio.run`, so a synchronous test calling `invoke`
can't share an event loop with the server; a separate thread serves both
synchronous tests and `async def` tests. Fakes registered with `command` run on
the server's loop, in that thread: coordinate with the test through `threading`
primitives (awaited with `asyncio.to_thread`), not asyncio ones.

Clients reach the server through a TCP relay, so a test can make the network
go silent (`blackhole`) or drop every connection (`drop`).
"""

import asyncio
import functools
import posixpath
import shlex
import threading
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Self

import asyncssh

_HOST = "127.0.0.1"
# How long starting or stopping the server may take.
_TIMEOUT_SECONDS = 10
_RELAY_CHUNK_BYTES = 64 * 1024


@functools.cache
def _generate_key(_name: str) -> asyncssh.SSHKey:
    """Generate a key once per test session; `_name` tells the cached keys apart."""
    return asyncssh.generate_private_key("ssh-ed25519")


@dataclass(frozen=True)
class KbdintPrompt:
    """One prompt of a keyboard-interactive challenge, and the answer it accepts."""

    prompt: str
    answer: str
    echo: bool = False


@dataclass
class Auth:
    """
    The authentication methods the server offers. A method is offered only when
    it is set, so a test switches one off with `None` (or `[]` for public keys).
    """

    # Public keys accepted for `SSHTestServer.user`.
    client_keys: list[asyncssh.SSHKey] = field(default_factory=list)
    password: str | None = None
    # One challenge with all these prompts; every answer must match.
    kbdint: list[KbdintPrompt] | None = None


@dataclass(frozen=True)
class Invocation:
    """A command a client ran, as a fake sees it."""

    argv: list[str]
    process: asyncssh.SSHServerProcess[str]

    @property
    def term_type(self) -> str | None:
        """The terminal type if the client asked for a PTY."""
        return self.process.get_terminal_type()

    @property
    def stdin(self) -> asyncssh.SSHReader[str]:
        return self.process.stdin

    @property
    def stdout(self) -> asyncssh.SSHWriter[str]:
        return self.process.stdout

    @property
    def stderr(self) -> asyncssh.SSHWriter[str]:
        """Under a PTY, stdout: a terminal merges the two, as on a real server."""
        return self.process.stdout if self.term_type else self.process.stderr


# Returns the exit status, `None` for 0. A fake may also end the process itself,
# e.g. with `process.exit_with_signal`; then the return value is ignored.
type Handler = Callable[[Invocation], Awaitable[int | None] | int | None]
type StreamHandler = Callable[
    [asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]
]


def reply(*, stdout: str = "", stderr: str = "", exit_status: int = 0) -> Handler:
    """A fake that prints fixed output and exits."""

    def handler(invocation: Invocation) -> int:
        invocation.stdout.write(stdout)
        invocation.stderr.write(stderr)
        return exit_status

    return handler


class SSHTestServer:
    """
    An SSH server on 127.0.0.1 that runs no real commands, so it works on Windows.

    - Authentication: `auth` (public key for `client_key` only, by default).
      `auth_attempts` records each `(method, accepted)`.
    - Commands: fakes registered with `command`, matched on the command's words.
      `commands_run` records the words of every command started, in order.
    - SFTP: rooted in a local directory (`local_path` maps a remote path into it),
      with relative paths starting at `home`.
    - `direct-tcpip`: only to ports allowed with `allow_forwarding`, such as those
      of the stand-ins `listen` starts.
    - Connection loss: `blackhole` and `drop`.

    Use it as a context manager (or `start` and `stop`). Stopping raises the first
    exception a fake raised, so a broken fake fails the test instead of only
    exiting 1.
    """

    user = "alice"
    home = PurePosixPath("/home/alice")

    def __init__(self, root: Path, *, host_key: asyncssh.SSHKey | None = None) -> None:
        self.sftp_root = root
        self.host_key = host_key or _generate_key("host")
        # Not in any agent or ~/.ssh, so only tests that pass it can use it.
        self.client_key = _generate_key("client")
        self.auth = Auth(client_keys=[self.client_key])
        self.auth_attempts: list[tuple[str, bool]] = []
        self.commands_run: list[list[str]] = []
        self.errors: list[Exception] = []
        self._commands: dict[tuple[str, ...], Handler] = {}
        self._forwardable: set[tuple[str, int]] = set()
        self._listeners: list[asyncio.Server] = []
        self._connections: set[asyncssh.SSHServerConnection] = set()
        self._links: set[tuple[asyncio.StreamWriter, asyncio.StreamWriter]] = set()
        self._blackholed = False
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="ssh-test-server", daemon=True
        )
        self.host = _HOST
        self.port = 0

    # Configuration by tests

    def command(self, prefix: str, handler: Handler) -> None:
        """
        Run `handler` for every command whose words start with `prefix`'s words
        (split like a shell would). The longest matching prefix wins, so `""`
        catches everything else, including a shell without a command. A command
        that matches nothing fails like an unknown command in a shell (exit 127).
        """
        self._commands[tuple(shlex.split(prefix))] = handler

    def allow_forwarding(self, port: int, host: str = _HOST) -> None:
        """Let clients open `direct-tcpip` channels to `host:port`."""
        self._forwardable.add((host, port))

    def listen(self, handler: StreamHandler) -> int:
        """
        Serve TCP connections to a port on 127.0.0.1 with `handler`, on the server's
        thread, as a stand-in for something only the login node can reach (such as
        the worker's sshd). Forwarding to it is allowed. Returns the port.
        """
        listener = self._run(asyncio.start_server(handler, _HOST, 0))
        self._listeners.append(listener)
        port = listener.sockets[0].getsockname()[1]
        self.allow_forwarding(port)
        return port

    def local_path(self, remote_path: str | PurePosixPath) -> Path:
        """Where a remote path lives under `sftp_root`."""
        remote = posixpath.normpath(posixpath.join(str(self.home), remote_path))
        return self.sftp_root.joinpath(*PurePosixPath(remote).parts[1:])

    @property
    def known_hosts_line(self) -> str:
        """The server's line in a known hosts file."""
        public_key = self.host_key.export_public_key("openssh").decode().split()
        return f"[{self.host}]:{self.port} {public_key[0]} {public_key[1]}\n"

    @property
    def known_hosts(self) -> asyncssh.SSHKnownHosts:
        return asyncssh.import_known_hosts(self.known_hosts_line)

    # Connection loss

    def blackhole(self) -> None:
        """
        Stop delivering traffic in both directions, as if the network were
        unplugged: the connections stay open, but nothing (keepalive replies
        included) arrives any more.
        """
        self._loop.call_soon_threadsafe(setattr, self, "_blackholed", True)

    def drop(self) -> None:
        """Reset every client connection, as if the server went away."""
        self._loop.call_soon_threadsafe(self._abort_links)

    # Clients

    async def connect(self, **options: object) -> asyncssh.SSHClientConnection:
        """
        Connect as `user` with `client_key`, trusting the host key, from the
        caller's event loop. Ignores the developer's agent, keys and
        ~/.ssh/config; `options` override any `asyncssh.connect` option.
        """
        defaults: dict[str, object] = {
            "username": self.user,
            "known_hosts": self.known_hosts,
            "client_keys": [self.client_key],
            "agent_path": None,
            "config": None,
        }
        return await asyncssh.connect(self.host, self.port, **(defaults | options))

    # Lifecycle

    def start(self) -> None:
        self.local_path(self.home).mkdir(parents=True)
        self._thread.start()
        self._run(self._start())

    def stop(self) -> None:
        """Stop the server, then raise the first exception a fake raised."""
        try:
            self._run(self._stop())
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(_TIMEOUT_SECONDS)
            self._loop.close()
        if self.errors:
            raise self.errors[0]

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()

    def _run[T](self, coroutine: Awaitable[T]) -> T:
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result(
            _TIMEOUT_SECONDS
        )

    async def _start(self) -> None:
        self._acceptor = await asyncssh.listen(
            _HOST,
            0,
            server_factory=lambda: _Server(self),
            server_host_keys=[self.host_key],
            process_factory=self._run_command,
            sftp_factory=lambda channel: _SFTPServer(
                channel, self.sftp_root, self.home
            ),
            # Fakes see input as sent, without asyncssh's line editing or echo.
            line_editor=False,
        )
        self._relay = await asyncio.start_server(self._accept_relay, _HOST, 0)
        self.port = self._relay.sockets[0].getsockname()[1]

    async def _stop(self) -> None:
        self._relay.close()
        self._acceptor.close()
        for listener in self._listeners:
            listener.close()
            listener.close_clients()
        self._abort_links()
        for connection in self._connections:
            connection.abort()
        # Fakes still running, relays and asyncssh's own tasks.
        tasks = asyncio.all_tasks() - {asyncio.current_task()}
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._relay.wait_closed()
        await self._acceptor.wait_closed()
        for listener in self._listeners:
            await listener.wait_closed()
        await self._loop.shutdown_default_executor()

    # Commands

    async def _run_command(self, process: asyncssh.SSHServerProcess[str]) -> None:
        argv = shlex.split(process.command or "")
        self.commands_run.append(argv)
        handler = self._handler(argv)
        try:
            if handler is None:
                name = argv[0] if argv else "(shell)"
                process.stderr.write(f"bash: {name}: command not found\n")
                exit_status = 127
            else:
                result = handler(Invocation(argv, process))
                exit_status = await result if isinstance(result, Awaitable) else result
        # The client went away while the fake was talking to it.
        except BrokenPipeError, ConnectionError, asyncssh.DisconnectError:
            exit_status = 1
        # ruff: ignore[BLE001]  # Reported when the server stops, failing the test
        except Exception as error:
            self.errors.append(error)
            process.stderr.write(f"fake for {shlex.join(argv)!r} raised {error!r}\n")
            exit_status = 1
        process.exit(exit_status or 0)

    def _handler(self, argv: list[str]) -> Handler | None:
        prefixes = (
            prefix for prefix in self._commands if tuple(argv[: len(prefix)]) == prefix
        )
        longest = max(prefixes, key=len, default=None)
        return None if longest is None else self._commands[longest]

    # Relay

    async def _accept_relay(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        server_reader, server_writer = await asyncio.open_connection(
            _HOST, self._acceptor.get_port()
        )
        link = (client_writer, server_writer)
        self._links.add(link)
        pipes = [
            asyncio.create_task(self._pipe(client_reader, server_writer)),
            asyncio.create_task(self._pipe(server_reader, client_writer)),
        ]
        try:
            # When either side closes, close the other one too.
            await asyncio.wait(pipes, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pipe in pipes:
                pipe.cancel()
            self._links.discard(link)
            for writer in link:
                writer.close()

    async def _pipe(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        while data := await reader.read(_RELAY_CHUNK_BYTES):
            if not self._blackholed:
                writer.write(data)
                await writer.drain()

    def _abort_links(self) -> None:
        for link in self._links:
            for writer in link:
                writer.transport.abort()


class _Server(asyncssh.SSHServer):
    def __init__(self, test_server: SSHTestServer) -> None:
        self._test_server = test_server
        self._auth = test_server.auth
        self._keys_offered: set[bytes] = set()

    def connection_made(self, conn: asyncssh.SSHServerConnection) -> None:
        self._conn = conn
        self._test_server._connections.add(conn)

    def connection_lost(self, exc: Exception | None) -> None:
        self._test_server._connections.discard(self._conn)

    def begin_auth(self, username: str) -> bool:
        # Always ask for authentication.
        return True

    def public_key_auth_supported(self) -> bool:
        return bool(self._auth.client_keys)

    def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        accepted = username == self._test_server.user and any(
            key.public_data == client_key.public_data
            for client_key in self._auth.client_keys
        )
        # asyncssh asks twice per key: when the client offers it, then to check
        # its signature. Record it once.
        if key.public_data not in self._keys_offered:
            self._keys_offered.add(key.public_data)
            self._test_server.auth_attempts.append(("publickey", accepted))
        return accepted

    def password_auth_supported(self) -> bool:
        return self._auth.password is not None

    def validate_password(self, username: str, password: str) -> bool:
        accepted = (
            username == self._test_server.user and password == self._auth.password
        )
        self._test_server.auth_attempts.append(("password", accepted))
        return accepted

    def kbdint_auth_supported(self) -> bool:
        return self._auth.kbdint is not None

    def get_kbdint_challenge(
        self, username: str, lang: str, submethods: str
    ) -> tuple[str, str, str, list[tuple[str, bool]]]:
        prompts = [(prompt.prompt, prompt.echo) for prompt in self._auth.kbdint or ()]
        return "", "", "", prompts

    def validate_kbdint_response(self, username: str, responses: Iterable[str]) -> bool:
        answers = [prompt.answer for prompt in self._auth.kbdint or ()]
        accepted = username == self._test_server.user and list(responses) == answers
        self._test_server.auth_attempts.append(("keyboard-interactive", accepted))
        return accepted

    def connection_requested(
        self, dest_host: str, dest_port: int, orig_host: str, orig_port: int
    ) -> bool:
        if (dest_host, dest_port) not in self._test_server._forwardable:
            raise asyncssh.ChannelOpenError(
                asyncssh.OPEN_ADMINISTRATIVELY_PROHIBITED,
                f"{dest_host}:{dest_port} is not allowed by the test",
            )
        # asyncssh opens the TCP connection and forwards it.
        return True


class _SFTPServer(asyncssh.SFTPServer):
    """Chrooted in `root`, with relative paths starting at `home`, as on a real
    server (asyncssh's chroot starts them at `/`)."""

    def __init__(
        self, channel: asyncssh.SSHServerChannel, root: Path, home: PurePosixPath
    ) -> None:
        super().__init__(channel, chroot=str(root).encode())
        self._home = str(home).encode()

    def map_path(self, path: bytes) -> bytes:
        return super().map_path(posixpath.join(self._home, path))
