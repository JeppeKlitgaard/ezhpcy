"""
Generated OpenSSH configuration that users pull in with a single Include.

Everything lives in one runtime directory matched by one glob. Each running
tunnel writes ``active-<alias>.conf``, a Host block that points OpenSSH straight
at the tunnel's forwarded loopback port, once it serves, and removes it on exit.
There is no block while no tunnel runs. A tunnel killed before it could remove
its block leaves it behind, with its alias lock (`ezhpcy.tunnel.lock`) free, so
the next tunnel start, or `ezhpcy doctor`, removes it.

OpenSSH refuses to load any included file that others may write, and that
failure breaks every host rather than just ours, so files are written with an
explicit owner-only ACL and moved into place atomically.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
lazy import os
lazy import subprocess
lazy import uuid
lazy from collections.abc import Generator, Iterable

lazy from rich.text import Text

from ezhpcy.config import config, validate_alias
from ezhpcy.logging import get_logger
lazy from ezhpcy.console import console, error_console
lazy from ezhpcy.constants import (
    LOOPBACK_HOST,
    WINDOWS_CREATION_FLAGS,
    WORKER_HOST_ALIAS,
)
lazy from ezhpcy.permissions import (
    FilePermissionError,
    private_directory,
    restrict_to_current_user,
)
lazy from ezhpcy.provision_local import WorkerCredentials
lazy from ezhpcy.tunnel.lock import alias_locked
lazy from ezhpcy.utils import local_machine_id

logger = get_logger(__name__)

SSH_CONFIG_DIRECTORY_NAME = "ssh-config"
ACTIVE_FILE_PREFIX = "active-"
# Sorts after every `active-*.conf`, so a running tunnel's block comes first.
_PROBE_FILE_NAME = "doctor-probe.conf"
# Where `probe_hosts`' blocks point; `.invalid` never resolves.
PROBE_HOST_NAME = "ezhpcy-doctor-probe.invalid"
_SSH_RESOLVE_TIMEOUT_SECONDS = 10


def _config_path(path: Path) -> str:
    """Render an absolute path safely in OpenSSH configuration syntax."""
    value = path.expanduser().resolve().as_posix()
    return f'"{value.replace(chr(34), chr(92) + chr(34))}"'


def ssh_config_dir() -> Path:
    return config.local_file.runtime_dir / SSH_CONFIG_DIRECTORY_NAME


def include_directive() -> str:
    """Return the lines users add to their own OpenSSH configuration."""
    directory = ssh_config_dir().expanduser().resolve().as_posix()
    return (
        f'# Load EzHPCy static and dynamic SSH profiles\nInclude "{directory}/*.conf"'
    )


def echo_include_directive(*, err: bool = False) -> None:
    """Print the Include lines followed by a blank line, ready to copy verbatim."""
    # Unindented and unwrapped, even for long paths; the colour marks what to
    # copy and is dropped when output is not a terminal.
    (error_console if err else console).print(
        Text(include_directive() + "\n", style="bold cyan"), soft_wrap=True
    )


@dataclass(frozen=True)
class WorkerHost:
    """One OpenSSH Host alias that reaches a worker through a tunnel."""

    alias: str
    credentials: WorkerCredentials

    def __post_init__(self) -> None:
        validate_alias(self.alias)
        if not self.user or any(character.isspace() for character in self.user):
            raise ValueError(f"user {self.user!r} must be one non-whitespace token")

    @classmethod
    def for_endpoint(cls, alias: str, *, user: str, host: str) -> WorkerHost:
        return cls(alias, WorkerCredentials(local_machine_id(), user, host))

    @property
    def user(self) -> str:
        return self.credentials.login_user

    @property
    def identity_file(self) -> Path:
        return self.credentials.client_key

    def render(self, port: int) -> str:
        """The Host block that reaches the worker through the tunnel's `port`."""
        return "\n".join(
            (
                f"Host {self.alias}",
                f"    HostName {LOOPBACK_HOST}",
                f"    Port {port}",
                f"    User {self.user}",
                f"    IdentityFile {_config_path(self.identity_file)}",
                "    IdentitiesOnly yes",
                f"    UserKnownHostsFile {_config_path(self.credentials.known_hosts)}",
                f"    HostKeyAlias {WORKER_HOST_ALIAS}",
                "    StrictHostKeyChecking yes",
                # Keeps out a ProxyJump or ProxyCommand from the user's `Host *`.
                "    ProxyCommand none",
                "",
            )
        )


def _write_private(path: Path, content: str) -> None:
    """Atomically write a file that OpenSSH accepts as an included config."""
    directory = path.parent
    private_directory(directory)
    # The temporary name does not match the `*.conf` Include glob, so OpenSSH
    # never reads a file before its permissions are restricted.
    temporary = directory / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(content)
        restrict_to_current_user(temporary)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _active_path(alias: str) -> Path:
    return ssh_config_dir() / f"{ACTIVE_FILE_PREFIX}{validate_alias(alias)}.conf"


def write_active_host_config(host: WorkerHost, port: int) -> Path:
    """Write the Host block of a tunnel serving on `port`."""
    path = _active_path(host.alias)
    _write_private(path, host.render(port))
    return path


def publish_host(host: WorkerHost, port: int) -> None:
    """
    Expose a running tunnel's Host block and check that OpenSSH resolves it.
    Problems are warnings: the tunnel works without the block, through a
    hand-written one.
    """
    try:
        prune_stale_active_configs()
        write_active_host_config(host, port)
    except (OSError, FilePermissionError) as error:
        logger.warning(
            t"Could not write the SSH configuration for {host.alias:name}: {error}"
        )
        return
    problem = check_host_resolution(host, port)
    if problem is not None:
        logger.warning(
            t"{f'ssh {host.alias}':suggestion} will not reach this tunnel: "
            t"{problem}. If {'~/.ssh/config':path} does not include EzHPCy's SSH "
            t"profiles yet, copy the two highlighted lines below to its top, "
            t"before any Host or Match block:"
        )
        error_console.print()
        echo_include_directive(err=True)


def withdraw_host(host: WorkerHost) -> None:
    """Remove the Host block `publish_host` wrote; the alias lock makes it ours."""
    try:
        _active_path(host.alias).unlink(missing_ok=True)
    except OSError as error:
        logger.warning(
            t"Could not remove the SSH configuration for {host.alias:name}: {error}"
        )


def prune_stale_active_configs() -> list[Path]:
    """Remove the Host blocks of tunnels that no longer run; return those removed."""
    removed = []
    for path in sorted(ssh_config_dir().glob(f"{ACTIVE_FILE_PREFIX}*.conf")):
        alias = path.name.removeprefix(ACTIVE_FILE_PREFIX).removesuffix(".conf")
        try:
            live = alias_locked(alias)
        # Not a name a tunnel writes, so no tunnel's.
        except ValueError:
            live = False
        if not live:
            logger.debug(t"Removing stale SSH host configuration {path:path}.")

            path.unlink(missing_ok=True)
            removed.append(path)
    return removed


@contextmanager
def probe_hosts(aliases: Iterable[str]) -> Generator[None]:
    """
    For the block, give each alias a Host block whose HostName is
    `PROBE_HOST_NAME`, after every running tunnel's. An alias that `ssh -G`
    then resolves elsewhere is shadowed by other configuration, or the Include
    line is missing or misplaced.
    """
    path = ssh_config_dir() / _PROBE_FILE_NAME
    blocks = "".join(
        f"Host {validate_alias(alias)}\n    HostName {PROBE_HOST_NAME}\n"
        for alias in aliases
    )
    _write_private(
        path, f"# Written by `ezhpcy doctor` while it checks, then removed.\n{blocks}"
    )
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def resolve_alias(alias: str) -> dict[str, list[str]] | str | None:
    """
    Ask OpenSSH how it resolves `alias` (`ssh -G`): its options, lower-case,
    with every value in order; or the problem running it; or `None` when `ssh`
    is not on PATH.
    """
    try:
        result = subprocess.run(
            ["ssh", "-G", alias],
            capture_output=True,
            check=False,
            text=True,
            timeout=_SSH_RESOLVE_TIMEOUT_SECONDS,
            creationflags=WINDOWS_CREATION_FLAGS,
        )
    except FileNotFoundError:
        return None
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"could not run `ssh -G {alias}`: {error}"

    if result.returncode != 0:
        detail = " ".join(result.stderr.split())
        return detail or f"`ssh -G {alias}` exited with {result.returncode}"

    ssh_options: dict[str, list[str]] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition(" ")
        ssh_options.setdefault(key.lower(), []).append(value)
    return ssh_options


def check_host_resolution(host: WorkerHost, port: int) -> str | None:
    """
    Ask OpenSSH how it resolves a tunnel's alias and describe any mismatch.

    This catches a missing Include, an Include placed after a Host block (which
    scopes it to that block), an earlier hand-written block for the same alias,
    and included files with permissions OpenSSH refuses.
    """
    ssh_options = resolve_alias(host.alias)
    if ssh_options is None:
        logger.debug(
            t"Skipping SSH configuration check: {'ssh':suggestion} is not on PATH."
        )
        return None
    if isinstance(ssh_options, str):
        return ssh_options

    endpoint = ssh_options.get("hostname"), ssh_options.get("port")
    if endpoint != ([LOOPBACK_HOST], [str(port)]):
        return "OpenSSH does not resolve it to the ezhpcy-generated configuration"
    # `ssh -G` prints no `proxycommand` for `ProxyCommand none`.
    if ssh_options.get("proxycommand", ["none"]) != ["none"]:
        return f"OpenSSH resolves a ProxyCommand: {ssh_options['proxycommand'][0]}"
    if ssh_options.get("user") != [host.user]:
        return f"OpenSSH resolves a different user: {ssh_options.get('user')}"
    identity_files = ssh_options.get("identityfile", [])
    expected_identity = os.path.normcase(host.identity_file.resolve().as_posix())
    if not identity_files or os.path.normcase(identity_files[0]) != expected_identity:
        return "OpenSSH resolves a different IdentityFile"
    return None
