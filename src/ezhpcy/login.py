"""
Connecting to a login node, with a password and host keys as needed.

Agent and key authentication come first. Only if they fail, no password was
given or read from the `Login`'s sources, prompting is allowed, and the server
offers `password` or `keyboard-interactive`, is the caller's
`Prompts.ask_password` awaited, and EzHPCy connects again with its answer, up to
`PASSWORD_ATTEMPTS` times. asyncssh answers a keyboard-interactive prompt that
asks for a password with it. A password given or read is tried once, and never
asked for.

Host keys are checked against ~/.ssh/known_hosts and EzHPCy's own known hosts
file. For an unknown host the caller's `Prompts.accept_host_key` is awaited; a
key it accepts is appended to EzHPCy's file, and EzHPCy connects again;
~/.ssh/known_hosts is only read. A host known with another key is refused
without asking.

Nothing is asked during authentication itself, so a prompt may block the event
loop, and the server's login timeout doesn't run while the user types. Without
`Prompts`, what would need an answer fails with `ConnectionFailed` instead.
"""

from dataclasses import dataclass
lazy import asyncio
lazy import getpass
lazy import os
lazy from collections.abc import Awaitable, Callable
lazy from pathlib import Path

lazy import asyncssh
lazy import keyring
lazy from keyring.errors import KeyringError
lazy from pydantic import SecretStr

from ezhpcy import ssh
from ezhpcy.logging import get_logger
from ezhpcy.messages import MessageError
lazy from ezhpcy.config import config
lazy from ezhpcy.constants import KEYRING_SERVICE_NAME
lazy from ezhpcy.types import ConfigError, Login

logger = get_logger(__name__)

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


@dataclass(frozen=True, slots=True)
class Prompts:
    """
    How to ask the user while connecting, for a caller that can.

    - `ask_password(user, host, attempt)`: the login node's password, for the
      `attempt`th time (from 1): from the second on, the last one was wrong.
    - `accept_host_key(host, port, key_type, fingerprint)`: whether to trust a
      host key that no known hosts file has; `fingerprint` is SHA256's, as
      OpenSSH shows it.

    Either may raise to stop connecting, e.g. `KeyboardInterrupt` at a terminal.
    """

    ask_password: Callable[[str, str, int], Awaitable[str]]
    accept_host_key: Callable[[str, int, str, str], Awaitable[bool]]


class ConnectionFailed(MessageError, RuntimeError):
    """Could not connect to the login node; the cause, whose message this is, says why."""

    def __init__(self, cause: Exception) -> None:
        super().__init__(
            cause.message if isinstance(cause, MessageError) else t"{cause}"
        )
        self.__cause__ = cause


def terminal_prompts() -> Prompts:
    """
    Prompts for a script at a terminal: the password with `getpass`, and the host
    key with `input`, each in a thread.
    """

    async def ask_password(user: str, host: str, attempt: int) -> str:
        again = "Wrong password. " if attempt > 1 else ""
        return await asyncio.to_thread(
            getpass.getpass, f"{again}Password for {user}@{host}: "
        )

    async def accept_host_key(
        host: str, port: int, key_type: str, fingerprint: str
    ) -> bool:
        where = host if port == _DEFAULT_PORT else f"{host}:{port}"
        answer = await asyncio.to_thread(
            input,
            f"The {key_type} host key of {where} is not known; its fingerprint is "
            f"{fingerprint}. Trust it? [y/N] ",
        )
        return answer.strip().casefold() in {"y", "yes"}

    return Prompts(ask_password=ask_password, accept_host_key=accept_host_key)


async def connect(
    login: Login,
    *,
    prompts: Prompts | None = None,
    password: SecretStr | None = None,
    known_hosts_file: Path | None = None,
    **options: object,
) -> ssh.RemoteHost:
    """
    Connect to the login node, with `password`, or one read from the `login`'s
    sources, and asking `prompts` as needed (see the module).

    Use the result with `async with` to close it.

    `known_hosts_file` is EzHPCy's own known hosts file (by default
    `config.local_file.login_known_hosts_file`). `options` are passed on to
    `ezhpcy.ssh.connect`.

    Raises `ConnectionFailed` when it can't connect, `ConfigError` when a
    password source can't be read, and whatever a prompt raises.
    """
    known_hosts_file = known_hosts_file or config.local_file.login_known_hosts_file
    if password is None:
        password = await asyncio.to_thread(read_password, login)
    known_hosts = await asyncio.to_thread(_KnownHosts.load, known_hosts_file)
    given = password
    password_attempts = 0
    try:
        while True:
            client = _LoginClient(known_hosts)
            connect_options: dict[str, object] = {"known_hosts": known_hosts.combined()}
            if not known_hosts.files_knowing(login.host, login.port):
                connect_options["server_host_key_algs"] = list(_UNKNOWN_HOST_KEY_ALGS)
            if password is not None:
                connect_options["password"] = password.get_secret_value()
            try:
                return await ssh.connect(
                    login, client=client, **(connect_options | options)
                )
            except asyncssh.HostKeyNotVerifiable:
                if client.changed_host_key is not None:
                    raise client.changed_host_key from None
                if client.unknown_host_key is None:
                    raise
                line = await _accept_host_key(
                    prompts, *client.unknown_host_key, known_hosts_file
                )
                known_hosts.add(known_hosts_file, line)
            except asyncssh.PermissionDenied:
                # A password given or read is tried once, and never asked for.
                if (
                    given is not None
                    or not login.password_prompt
                    or not client.password_offered
                    or password_attempts == PASSWORD_ATTEMPTS
                ):
                    raise
                if prompts is None:
                    raise asyncssh.PermissionDenied(
                        f"The login node asks for {login.user}'s password, and none "
                        "was given, nor a prompt to ask for it."
                    ) from None
                password_attempts += 1
                password = SecretStr(
                    await prompts.ask_password(
                        login.user, login.host, password_attempts
                    )
                )
    except (asyncssh.Error, OSError) as error:
        raise ConnectionFailed(error) from error


def read_password(login: Login) -> SecretStr | None:
    """
    The password from the first of the `login`'s sources that has one, or None
    to leave it to the prompt. Blocks: call it in a thread.

    Raises `ConfigError` naming the source when it can't be read, and for a
    keyring without the password when prompting isn't allowed.
    """
    if login.password_fd is not None:
        return SecretStr(_read_password_fd(login.password_fd))
    if login.password_file is not None:
        return SecretStr(_read_password_file(login.password_file))
    if login.password_keyring:
        password = _read_password_keyring(login)
        return SecretStr(password) if password is not None else None
    return None


def _without_trailing_line_endings(value: str) -> str:
    """Remove line endings commonly added by secret files and pipes."""
    return value.rstrip("\r\n")


def _read_password_file(path: Path) -> str:
    try:
        return _without_trailing_line_endings(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as error:
        raise ConfigError.of_setting(
            "password_file",
            t"Could not read the file: {error}.",
            value=path,
        ) from error


def _read_password_fd(fd: int) -> str:
    try:
        # Read through a duplicate so EzHPCy never closes a descriptor owned by
        # its caller. The duplicate intentionally shares the original offset.
        with os.fdopen(os.dup(fd), encoding="utf-8") as password_stream:
            return _without_trailing_line_endings(password_stream.read())
    except (OSError, UnicodeError) as error:
        raise ConfigError.of_setting(
            "password_fd",
            t"Could not read the file descriptor: {error}.",
            value=fd,
        ) from error


def _read_password_keyring(login: Login) -> str | None:
    """Read the password from the keyring, or `None` to prompt for it instead."""
    account = f"{login.user}@{login.host}"
    try:
        password = keyring.get_password(KEYRING_SERVICE_NAME, account)
    except KeyringError as error:
        message = t"Could not read the password from the system keyring: {error}."
    else:
        if password is not None:
            return password
        message = (
            t"No password was found in the system keyring for service "
            t"{KEYRING_SERVICE_NAME:name} and account {account:name}. "
            t"Store one with {'ezhpcy keyring set':suggestion}."
        )

    if not login.password_prompt:
        raise ConfigError(message)
    logger.warning(message + t" The password is asked for if needed.")
    return None


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
                logger.debug(t"No known hosts file: file={file:path}")
                continue
            except (OSError, UnicodeDecodeError) as error:
                logger.warning(t"Could not read known hosts file {file:path}: {error}")
                continue
            lines[file] = []
            for number, line in enumerate(text.splitlines(), start=1):
                try:
                    asyncssh.import_known_hosts(line)
                except ValueError as error:
                    logger.warning(
                        t"Skipping line {number:detail} of known hosts file "
                        t"{file:path}: {error}"
                    )
                    continue
                lines[file].append(line)
            logger.debug(
                t"Loaded known hosts file: file={file:path} "
                t"lines={len(lines[file]):detail}"
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


class _LoginClient(ssh.SSHClient):
    """
    Records what `connect` needs to know after a failed attempt: host keys it
    couldn't trust, and whether the server offered password or
    keyboard-interactive authentication.
    """

    def __init__(self, known_hosts: _KnownHosts) -> None:
        super().__init__()
        self._known_hosts = known_hosts
        self.unknown_host_key: tuple[str, int, asyncssh.SSHKey] | None = None
        self.changed_host_key: asyncssh.HostKeyNotVerifiable | None = None
        self.password_offered = False

    def validate_host_public_key(
        self, host: str, addr: str, port: int, key: asyncssh.SSHKey
    ) -> bool:
        # asyncssh asks only about a key it doesn't trust. The answer is needed in
        # the middle of key exchange, so the key is refused here, and `connect`
        # asks the caller and reconnects.
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
    # password given to `connect`, once; `connect` asks for a password and
    # connects again.

    def password_auth_requested(self) -> str | Awaitable[str | None] | None:
        self.password_offered = True
        return super().password_auth_requested()

    def kbdint_auth_requested(self) -> str | Awaitable[str | None] | None:
        self.password_offered = True
        return super().kbdint_auth_requested()


async def _accept_host_key(
    prompts: Prompts | None,
    host: str,
    port: int,
    key: asyncssh.SSHKey,
    known_hosts_file: Path,
) -> str:
    """
    Ask whether to trust an unknown host key and return its known hosts line.
    An accepted key is appended to `known_hosts_file`; if that fails, the key is
    only trusted for this connection.
    """
    if prompts is None:
        raise asyncssh.HostKeyNotVerifiable(
            f"The {key.get_algorithm()} host key of {host} "
            f"({key.get_fingerprint()}) is not in any known hosts file, and there "
            "is no prompt to accept it."
        )
    accepted = await prompts.accept_host_key(
        host, port, key.get_algorithm(), key.get_fingerprint()
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
        # remembered, so the next connection asks again.
        logger.warning(
            t"Could not save the host key for {host:detail} to "
            t"{known_hosts_file:path}: {error}"
        )
        return line
    logger.debug(
        t"Saved host key: host={host:detail} type={key_type:detail} "
        t"fingerprint={key.get_fingerprint():detail} file={known_hosts_file:path}"
    )
    logger.info(t"Host key for {host} saved to {known_hosts_file:path}.")
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
