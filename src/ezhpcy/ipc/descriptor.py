"""Secure publication and discovery of tunnel runtime descriptors."""

from dataclasses import dataclass, field
lazy import base64
lazy import contextlib
lazy import json
lazy import os
lazy import stat
lazy import uuid
lazy from pathlib import Path
lazy from typing import Self

lazy from ezhpcy.config import PROFILE_NAME_PATTERN, config
lazy from ezhpcy.ipc import LOOPBACK_HOST, IPCError

DESCRIPTOR_VERSION = 2
_INVALID = "tunnel runtime information is invalid; restart the tunnel"


def descriptor_path(alias: str) -> Path:
    """Return where the tunnel serving an SSH host alias publishes its descriptor."""
    if PROFILE_NAME_PATTERN.fullmatch(alias) is None:
        raise ValueError(f"invalid host alias {alias!r}")
    return config.local_file.runtime_dir / "descriptors" / f"{alias}.json"


def tunnel_not_running(alias: str) -> IPCError:
    return IPCError(f"no tunnel is running for `{alias}`; start `ezhpcy tunnel`")


@dataclass(frozen=True)
class TunnelDescriptor:
    """How a proxy finds, and authenticates to, the tunnel serving an SSH host alias."""

    alias: str
    port: int
    authkey: bytes = field(repr=False)
    # Tells this tunnel's descriptor and Host block apart from those of a tunnel
    # that has since replaced it under the same alias.
    instance_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    # Published by the tunnel and read back by each proxy, which OpenSSH starts
    # from a fixed ProxyCommand line and so cannot be given its own flags.
    debug: bool = False

    def __post_init__(self) -> None:
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("tunnel IPC port is invalid")
        if len(self.authkey) < 32:
            raise ValueError("tunnel authkey must contain at least 32 random bytes")

    @property
    def path(self) -> Path:
        return descriptor_path(self.alias)

    @classmethod
    def load(cls, alias: str) -> Self:
        """
        Read the descriptor published for `alias`.

        Raises `ValueError` for an alias that isn't a safe file name, and
        `IPCError` if no tunnel is running or its descriptor is unusable.
        """
        path = descriptor_path(alias)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload["version"] != DESCRIPTOR_VERSION:
                raise ValueError("unsupported runtime descriptor version")
            if payload["host"] != LOOPBACK_HOST:
                raise ValueError("tunnel IPC address must use IPv4 loopback")
            if not isinstance(payload["instance_id"], str) or not isinstance(
                payload["debug"], bool
            ):
                raise TypeError("runtime descriptor values have invalid types")
            return cls(
                alias,
                port=payload["port"],
                authkey=base64.b64decode(payload["authkey"], validate=True),
                instance_id=payload["instance_id"],
                debug=payload["debug"],
            )
        except FileNotFoundError as error:
            raise tunnel_not_running(alias) from error
        # `OSError` for an unreadable file; `ValueError` for invalid JSON or
        # base64; `KeyError` and `TypeError` for a payload of the wrong shape.
        except (OSError, KeyError, TypeError, ValueError) as error:
            raise IPCError(_INVALID) from error

    def publish(self) -> None:
        """Write the descriptor atomically, readable by the current user only."""
        content = json.dumps(
            {
                "version": DESCRIPTOR_VERSION,
                "host": LOOPBACK_HOST,
                "port": self.port,
                "authkey": base64.b64encode(self.authkey).decode("ascii"),
                "instance_id": self.instance_id,
                "debug": self.debug,
            },
            separators=(",", ":"),
        )
        path = self.path
        directory_fd = _prepare_runtime_directory(path.parent)
        # On POSIX, names are relative to the audited directory's descriptor, so
        # the directory can't be swapped between the checks and the write.
        # Elsewhere `directory_fd` is None and the names are full paths.
        directory = Path() if directory_fd is not None else path.parent
        temporary = directory / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        try:
            file = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_fd,
            )
            with os.fdopen(file, "w", encoding="utf-8") as stream:
                stream.write(content)
            # Readers see the old descriptor or the new one, never part of one.
            os.replace(
                temporary,
                directory / path.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory_fd)
            raise
        finally:
            if directory_fd is not None:
                os.close(directory_fd)

    def remove(self) -> None:
        """Remove the descriptor, unless another tunnel has replaced it since."""
        try:
            if TunnelDescriptor.load(self.alias).instance_id == self.instance_id:
                self.path.unlink(missing_ok=True)
        except IPCError, OSError:
            pass


def _prepare_runtime_directory(directory: Path) -> int | None:
    """Create a private runtime directory and pin its audited POSIX inode."""
    try:
        directory.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        pass
    except OSError as error:
        raise IPCError("could not create the tunnel runtime directory") from error

    if os.name != "posix":
        # Since Python 3.14, mkdir with mode 0700 applies an owner-and-
        # administrators-only ACL on Windows. Files then inherit that ACL.
        return None

    try:
        metadata = directory.lstat()
    except OSError as error:
        raise IPCError("could not inspect the tunnel runtime directory") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise IPCError("tunnel runtime directory must not be a symbolic link")
    if not stat.S_ISDIR(metadata.st_mode):
        raise IPCError("tunnel runtime path is not a directory")
    if metadata.st_uid != os.getuid():
        raise IPCError("tunnel runtime directory is not owned by the current user")

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        directory_fd = os.open(directory, flags)
    except OSError as error:
        raise IPCError(
            "could not securely open the tunnel runtime directory"
        ) from error

    try:
        metadata = os.fstat(directory_fd)
        if not stat.S_ISDIR(metadata.st_mode):
            raise IPCError("tunnel runtime path is not a directory")
        if metadata.st_uid != os.getuid():
            raise IPCError("tunnel runtime directory is not owned by the current user")
        try:
            os.fchmod(directory_fd, 0o700)
        except OSError as error:
            raise IPCError(
                "could not restrict the tunnel runtime directory permissions"
            ) from error
        if stat.S_IMODE(os.fstat(directory_fd).st_mode) != 0o700:
            raise IPCError("tunnel runtime directory permissions are not private")
    except BaseException:
        os.close(directory_fd)
        raise
    return directory_fd
