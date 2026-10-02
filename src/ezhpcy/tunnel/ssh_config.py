"""
Generated OpenSSH configuration that users pull in with a single Include.

Everything lives in one runtime directory matched by one glob:

- ``profiles.conf`` holds a Host block per configured profile. It is rewritten
  from the configuration whenever a tunnel starts, so profile hosts stay known
  to OpenSSH (and VS Code) between tunnels.
- ``active-<alias>.conf`` holds the Host block of one running tunnel whose block
  is not already in ``profiles.conf``: a tunnel without a profile, one with a
  custom ``--alias``, or a profile tunnel whose ``--user``/``--host`` override
  changes the block. The tunnel writes it once its descriptor is published and
  removes it on exit. These files sort before ``profiles.conf``, and OpenSSH
  keeps the first value it obtains, so an overriding block wins over the
  profile block with the same alias.

OpenSSH refuses to load any included file that others may write, and that
failure breaks every host rather than just ours, so files are written with an
explicit owner-only ACL and moved into place atomically.
"""

import logging
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from ezhpcy.config import PROFILE_NAME_PATTERN, Config, config
from ezhpcy.constants import (
    WINDOWS_CREATION_FLAGS,
    WORKER_CLIENT_KEY_NAME,
    WORKER_HOST_ALIAS,
)
from ezhpcy.ipc import descriptor_path
from ezhpcy.ipc.common import IPCError
from ezhpcy.ipc.runtime import load_runtime_descriptor
from ezhpcy.permissions import restrict_to_current_user
from ezhpcy.utils import local_machine_id

logger = logging.getLogger(__name__)

SSH_CONFIG_DIRECTORY_NAME = "ssh-config"
PROFILES_FILE_NAME = "profiles.conf"
ACTIVE_FILE_PREFIX = "active-"
_INSTANCE_MARKER = "# ezhpcy-instance: "
_SSH_RESOLVE_TIMEOUT_SECONDS = 10


def _config_path(path: Path) -> str:
    """Render an absolute path safely in OpenSSH configuration syntax."""
    value = path.expanduser().resolve().as_posix()
    return f'"{value.replace(chr(34), chr(92) + chr(34))}"'


def validate_alias(alias: str) -> str:
    if PROFILE_NAME_PATTERN.fullmatch(alias) is None:
        raise ValueError(
            f"host alias {alias!r} may contain only letters, digits, '.', '_', "
            "and '-', and must start with a letter or digit"
        )
    return alias


def ssh_config_dir() -> Path:
    return config.local_file.runtime_dir / SSH_CONFIG_DIRECTORY_NAME


def include_directive() -> str:
    """Return the lines users add to their own OpenSSH configuration."""
    directory = ssh_config_dir().expanduser().resolve().as_posix()
    return (
        f'# Load EzHPCy static and dynamic SSH profiles\nInclude "{directory}/*.conf"'
    )


@dataclass(frozen=True)
class WorkerHost:
    """One OpenSSH Host alias that reaches a worker through a tunnel."""

    alias: str
    user: str
    ssh_dir: Path
    python_executable: Path = Path(sys.executable)

    def __post_init__(self) -> None:
        validate_alias(self.alias)
        if not self.user or any(character.isspace() for character in self.user):
            raise ValueError(f"user {self.user!r} must be one non-whitespace token")

    @classmethod
    def for_endpoint(cls, alias: str, *, user: str, host: str) -> WorkerHost:
        ssh_dir = config.local_file.ssh_dir(local_machine_id(), user=user, host=host)
        return cls(alias=alias, user=user, ssh_dir=ssh_dir)

    @property
    def identity_file(self) -> Path:
        return self.ssh_dir / WORKER_CLIENT_KEY_NAME

    @property
    def proxy_command(self) -> str:
        return (
            f"{_config_path(self.python_executable)} -m ezhpcy.cli proxy {self.alias}"
        )

    def render(self) -> str:
        known_hosts = self.ssh_dir / "worker_known_hosts"
        return "\n".join(
            (
                f"Host {self.alias}",
                f"    HostName {self.alias}",
                f"    User {self.user}",
                f"    IdentityFile {_config_path(self.identity_file)}",
                "    IdentitiesOnly yes",
                f"    UserKnownHostsFile {_config_path(known_hosts)}",
                f"    HostKeyAlias {WORKER_HOST_ALIAS}",
                "    StrictHostKeyChecking yes",
                f"    ProxyCommand {self.proxy_command}",
                "",
            )
        )


def profile_hosts(source: Config = config) -> list[WorkerHost]:
    """Return a Host for every profile that defines both a user and a host."""
    hosts = []
    for name in sorted(source.profile):
        resolved = source.resolve_profile(name, validate_password_source=False)
        if resolved.user is None or resolved.host is None:
            logger.debug(
                "Not generating an SSH host for profile %r: no user/host.", name
            )
            continue
        try:
            hosts.append(
                WorkerHost.for_endpoint(
                    name, user=resolved.user, host=str(resolved.host)
                )
            )
        except ValueError as error:
            logger.warning("Not generating an SSH host for profile %r: %s", name, error)
    return hosts


def _write_private(path: Path, content: str) -> None:
    """Atomically write a file that OpenSSH accepts as an included config."""
    directory = path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(directory, 0o700)
    # The temporary name does not match the `*.conf` Include glob, so OpenSSH
    # never reads a file before its permissions are restricted.
    temporary = directory / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(content)
        restrict_to_current_user(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_profiles_config(hosts: list[WorkerHost] | None = None) -> Path:
    """Regenerate the Host blocks for all configured profiles."""
    if hosts is None:
        hosts = profile_hosts()
    path = ssh_config_dir() / PROFILES_FILE_NAME
    header = (
        "# Generated by ezhpcy from its profiles; manual changes are overwritten.\n"
        "# Running tunnels write active-*.conf files, which take precedence.\n\n"
    )
    _write_private(path, header + "\n".join(host.render() for host in hosts))
    return path


def _active_path(alias: str) -> Path:
    return ssh_config_dir() / f"{ACTIVE_FILE_PREFIX}{validate_alias(alias)}.conf"


def _active_instance_id(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8") as file:
            first_line = file.readline().rstrip("\n")
    except OSError, UnicodeDecodeError:
        return None
    if not first_line.startswith(_INSTANCE_MARKER):
        return None
    return first_line.removeprefix(_INSTANCE_MARKER)


def write_active_host_config(host: WorkerHost, *, instance_id: str) -> Path:
    """Publish the Host block of a running tunnel, tagged with its instance."""
    path = _active_path(host.alias)
    _write_private(path, f"{_INSTANCE_MARKER}{instance_id}\n{host.render()}")
    return path


def remove_active_host_config(alias: str, *, instance_id: str) -> None:
    """Remove a tunnel's Host block unless a newer tunnel has replaced it."""
    path = _active_path(alias)
    if _active_instance_id(path) == instance_id:
        path.unlink(missing_ok=True)


def prune_stale_active_configs() -> None:
    """Remove Host blocks left behind by tunnels that are no longer running."""
    for path in ssh_config_dir().glob(f"{ACTIVE_FILE_PREFIX}*.conf"):
        alias = path.name.removeprefix(ACTIVE_FILE_PREFIX).removesuffix(".conf")
        try:
            live_instance_id = load_runtime_descriptor(
                descriptor_path(alias)
            ).instance_id
        except IPCError, ValueError:
            live_instance_id = None
        # A hard-killed tunnel leaves its descriptor behind as well, so this
        # only catches the cases where the descriptor was removed or replaced.
        if live_instance_id is None or _active_instance_id(path) != live_instance_id:
            logger.debug("Removing stale SSH host configuration %s.", path)
            path.unlink(missing_ok=True)


def check_host_resolution(host: WorkerHost) -> str | None:
    """
    Ask OpenSSH how it resolves an alias and describe any mismatch.

    This catches a missing Include, an Include placed after a Host block (which
    scopes it to that block), an earlier hand-written block for the same alias,
    and included files with permissions OpenSSH refuses.
    """
    try:
        result = subprocess.run(
            ["ssh", "-G", host.alias],
            capture_output=True,
            check=False,
            text=True,
            timeout=_SSH_RESOLVE_TIMEOUT_SECONDS,
            creationflags=WINDOWS_CREATION_FLAGS,
        )
    except FileNotFoundError:
        logger.debug("Skipping SSH configuration check: `ssh` is not on PATH.")
        return None
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"could not run `ssh -G {host.alias}`: {error}"

    if result.returncode != 0:
        detail = " ".join(result.stderr.split())
        return detail or f"`ssh -G {host.alias}` exited with {result.returncode}"

    resolved: dict[str, list[str]] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition(" ")
        resolved.setdefault(key.lower(), []).append(value)

    if resolved.get("proxycommand") != [host.proxy_command]:
        return "OpenSSH does not resolve it to the ezhpcy-generated configuration"
    if resolved.get("user") != [host.user]:
        return f"OpenSSH resolves a different user: {resolved.get('user')}"
    identity_files = resolved.get("identityfile", [])
    expected_identity = os.path.normcase(host.identity_file.resolve().as_posix())
    if not identity_files or os.path.normcase(identity_files[0]) != expected_identity:
        return "OpenSSH resolves a different IdentityFile"
    return None
