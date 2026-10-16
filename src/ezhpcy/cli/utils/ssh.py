"""
Establishes SSH connection with potential interactive elements through CLI.

Agent and key authentication come first. Only if they fail, the profile has no
password, prompting is allowed and the server offers `password` or
`keyboard-interactive`, the user is asked for a password and EzHPCy connects
again with it, up to `PASSWORD_ATTEMPTS` times. asyncssh answers a
keyboard-interactive prompt that asks for a password with it.

Host keys are checked against ~/.ssh/known_hosts and EzHPCy's own known hosts
file. For an unknown host the user is asked to accept its key, which is appended
to EzHPCy's file, and EzHPCy connects again; ~/.ssh/known_hosts is only read. A
host known with another key is refused without asking.

The user is never asked anything during authentication itself, so a prompt can
block the event loop and the server's login timeout doesn't run while the user types.
"""

import logging
lazy import asyncio
lazy from collections.abc import Awaitable
lazy from pathlib import Path

lazy import asyncssh
lazy from rich.prompt import Confirm, Prompt

from ezhpcy import ssh
lazy from ezhpcy.cli._output import print_error, print_warning, rich_text
lazy from ezhpcy.cli.utils.prompt import ask
lazy from ezhpcy.config import config
lazy from ezhpcy.console import error_console
lazy from ezhpcy.types import ConnectionInfo

logger = logging.getLogger(__name__)

PASSWORD_ATTEMPTS = 3
_DEFAULT_PORT = 22
# Reorder algorithms to match OpenSSH client defaults
# asyncssh defaults to prefering RSA
_UNKNOWN_HOST_KEY_ALGS = (
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
    "rsa-sha2-512",
    "rsa-sha2-256",
)


async def connect_interactive(
    conn_info: ConnectionInfo,
    *,
    known_hosts_file: Path | None = None,
    **options: object,
) -> ssh.RemoteHost:
    """
    Connect to the login node, prompting for a password and for an unknown host
    key as needed.

    Use the result with `async with` to close it.

    `known_hosts_file` is EzHPCy's own known hosts file (by default
    `config.local_file.login_known_hosts_file`). `options` are passed on to
    `ezhpcy.ssh.connect`, e.g. `port`.

    Raises `asyncssh.Error` (such as `PermissionDenied` or `HostKeyNotVerifiable`)
    or `OSError` when it can't connect, and `KeyboardInterrupt` when the user
    pressed Ctrl+C at a prompt.
    """
    known_hosts_file = known_hosts_file or config.local_file.login_known_hosts_file
    host = str(conn_info.host)
    port = options.get("port", _DEFAULT_PORT)
    assert isinstance(port, int)
    known_hosts = await asyncio.to_thread(_KnownHosts.load, known_hosts_file)
    password = conn_info.password
    password_attempts = 0
    while True:
        client = _InteractiveClient(known_hosts)
        connect_options: dict[str, object] = {"known_hosts": known_hosts.combined()}
        if not known_hosts.files_knowing(host, port):
            connect_options["server_host_key_algs"] = list(_UNKNOWN_HOST_KEY_ALGS)
        if password is not None:
            connect_options["password"] = password
        try:
            return await ssh.connect(
                conn_info, client=client, **(connect_options | options)
            )
        except asyncssh.HostKeyNotVerifiable:
            if client.changed_host_key is not None:
                raise client.changed_host_key from None
            if client.unknown_host_key is None:
                raise
            line = await _accept_host_key(*client.unknown_host_key, known_hosts_file)
            known_hosts.add(known_hosts_file, line)
        except asyncssh.PermissionDenied:
            # A password from the profile is tried once, and never prompted for.
            if (
                conn_info.password is not None
                or not conn_info.password_prompt
                or not client.password_offered
                or password_attempts == PASSWORD_ATTEMPTS
            ):
                raise
            if password_attempts:
                print_error(
                    t"Invalid password. Try again or press Ctrl+C to abort.",
                    label=f"Error [{password_attempts}/{PASSWORD_ATTEMPTS}]",
                )
            password_attempts += 1
            password = ask(
                Prompt.ask,
                rich_text(t"Enter password for {conn_info.user}@{host}"),
                password=True,
                console=error_console,
            )


async def connect_or_exit(conn_info: ConnectionInfo) -> ssh.RemoteHost:
    """
    `connect_interactive` for a command: if it can't connect, print
    `Error: Could not connect to user@host: …` and exit with status 1.
    """
    try:
        return await connect_interactive(conn_info)
    except (asyncssh.Error, OSError) as error:
        logger.debug("Could not connect.", exc_info=True)
        target = f"{conn_info.user}@{conn_info.host}"
        print_error(t"Could not connect to {target:name}: {error}")
        raise SystemExit(1) from error


class _KnownHosts:
    """The known hosts lines asyncssh can read, by file."""

    def __init__(self, lines: dict[Path, list[str]]) -> None:
        self.lines = lines

    @classmethod
    def load(cls, known_hosts_file: Path) -> _KnownHosts:
        """
        Read ~/.ssh/known_hosts and EzHPCy's `known_hosts_file` line by line.
        asyncssh rejects a whole file over one line it can't read (paramiko
        skipped such lines), so a bad line is skipped with a warning.
        """
        lines: dict[Path, list[str]] = {}
        for file in (Path("~/.ssh/known_hosts").expanduser(), known_hosts_file):
            try:
                text = file.read_text(encoding="utf-8")
            # NotADirectoryError: a parent is a file (Windows reports that as
            # FileNotFoundError).
            except FileNotFoundError, NotADirectoryError:
                logger.debug("No known hosts file: file=%s", file)
                continue
            except (OSError, UnicodeDecodeError) as error:
                logger.warning("Could not read known hosts file %s: %s", file, error)
                continue
            lines[file] = []
            for number, line in enumerate(text.splitlines(), start=1):
                try:
                    asyncssh.import_known_hosts(line)
                except ValueError as error:
                    logger.warning(
                        "Skipping line %d of known hosts file %s: %s",
                        number,
                        file,
                        error,
                    )
                    continue
                lines[file].append(line)
            logger.debug(
                "Loaded known hosts file: file=%s lines=%d", file, len(lines[file])
            )
        return cls(lines)

    def add(self, file: Path, line: str) -> None:
        """Trust a line written to `file`, even if writing it failed."""
        self.lines.setdefault(file, []).append(line)

    def combined(self) -> asyncssh.SSHKnownHosts:
        # Never empty or None: asyncssh would read ~/.ssh/known_hosts itself, or
        # trust any key.
        return asyncssh.import_known_hosts(
            "\n".join(line for lines in self.lines.values() for line in lines)
        )

    def files_knowing(self, host: str, port: int) -> list[Path]:
        """
        The files with a key (or a CA key) for `host` by name. A host known only by
        its IP address counts as unknown, as in OpenSSH.
        """
        return [file for file, lines in self.lines.items() if _knows(lines, host, port)]


def _knows(lines: list[str], host: str, port: int) -> bool:
    host_keys, ca_keys, *_ = asyncssh.import_known_hosts("\n".join(lines)).match(
        host, "", None if port == _DEFAULT_PORT else port
    )
    return bool(host_keys or ca_keys)


class _InteractiveClient(ssh.SSHClient):
    """
    Records what `connect_interactive` needs to know after a failed attempt: host
    keys it couldn't trust, and whether the server offered password or
    keyboard-interactive authentication.
    """

    def __init__(self, known_hosts: _KnownHosts) -> None:
        super().__init__()
        self._known_hosts = known_hosts
        self.unknown_host_key: tuple[str, int, asyncssh.SSHKey] | None = None
        self.changed_host_key: asyncssh.HostKeyNotVerifiable | None = None
        self.password_offered = False

    # Host keys

    def validate_host_public_key(
        self, host: str, addr: str, port: int, key: asyncssh.SSHKey
    ) -> bool:
        # asyncssh asks only about a key it doesn't trust. The answer is needed in
        # the middle of key exchange, so the key is refused here, and
        # `connect_interactive` asks the user and reconnects.
        files = self._known_hosts.files_knowing(host, port)
        if not files:
            self.unknown_host_key = (host, port, key)
            return False
        self.changed_host_key = asyncssh.HostKeyNotVerifiable(
            f"The {key.get_algorithm()} host key of {host} "
            f"({key.get_fingerprint()}) does not match the key for it in "
            f"{files[0]}. If the host's key was changed on purpose, remove the "
            "old key from that file."
        )
        return False

    # Authentication: only record that the server offered password or
    # keyboard-interactive authentication, and keep asyncssh's defaults. asyncssh
    # calls these only for methods the server offers, and for `password` only once
    # a password given to `connect` has been tried. Its defaults give no further
    # password, and answer a single keyboard-interactive password prompt with the
    # password given to `connect`, once; `connect_interactive` asks for a password
    # and connects again.

    def password_auth_requested(self) -> str | Awaitable[str | None] | None:
        self.password_offered = True
        return super().password_auth_requested()

    def kbdint_auth_requested(self) -> str | Awaitable[str | None] | None:
        self.password_offered = True
        return super().kbdint_auth_requested()


async def _accept_host_key(
    host: str, port: int, key: asyncssh.SSHKey, known_hosts_file: Path
) -> str:
    """
    Ask whether to trust an unknown host key and return its known hosts line.
    An accepted key is appended to `known_hosts_file`; if that fails, the key is
    only trusted for this connection.
    """
    print_warning(t"The host key for {host:name} is not in any known hosts file.")
    error_console.print(rich_text(t"Key type: {key.get_algorithm()}"))
    error_console.print(rich_text(t"Key fingerprint: {key.get_fingerprint()}"))
    accepted = ask(
        Confirm.ask,
        "Do you want to accept this host key?: ",
        console=error_console,
        default=False,
        case_sensitive=False,
    )
    if not accepted:
        raise asyncssh.HostKeyNotVerifiable(f"Host key for {host} rejected by user.")

    pattern = host if port == _DEFAULT_PORT else f"[{host}]:{port}"
    key_type, key_data, *_ = key.export_public_key("openssh").decode().split()
    line = f"{pattern} {key_type} {key_data}"
    try:
        await asyncio.to_thread(_append_line, known_hosts_file, line)
    except OSError as error:
        # The key is still trusted for this connection; it is only not
        # remembered, so the next connection prompts again.
        logger.warning(
            "Could not save the host key for %s to %s: %s",
            host,
            known_hosts_file,
            error,
        )
        return line
    logger.debug(
        "Saved host key: host=%s type=%s fingerprint=%s file=%s",
        host,
        key_type,
        key.get_fingerprint(),
        known_hosts_file,
    )
    error_console.print(
        rich_text(t"Host key for {host} saved to {known_hosts_file:path}.")
    )
    return line


def _append_line(file: Path, line: str) -> None:
    """Append a line, leaving the rest of the file as it is."""
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open("a+b") as handle:
        # A last line without a newline would run into the new one.
        handle.seek(0, 2)
        if handle.tell():
            handle.seek(-1, 2)
            if handle.read(1) != b"\n":
                handle.write(b"\n")
        handle.write(f"{line}\n".encode())
