"""The tunnel's side of the worker's control directory (see `ezhpcy.worker`)."""

from dataclasses import dataclass
lazy import contextlib
lazy from pathlib import PurePosixPath

lazy import asyncssh

from ezhpcy.logging import get_logger
lazy from ezhpcy.ssh import ensure_directory
lazy from ezhpcy.worker import WorkerError

logger = get_logger(__name__)

_LEASE_FILE_RETAIN_COUNT = 2


@dataclass(frozen=True, slots=True)
class WorkerReady:
    """sshd listens on `port`."""

    port: int


@dataclass(frozen=True, slots=True)
class WorkerFailed:
    """sshd couldn't start, for `reason`."""

    reason: str


@dataclass(frozen=True, slots=True)
class WorkerStopped:
    """sshd has exited, and the worker with it, with `exit_status`."""

    exit_status: int


type WorkerStatus = WorkerReady | WorkerFailed | WorkerStopped


class WorkerControl:
    """
    The tunnel's side of one worker's control directory.

    `publish_lease` must not run concurrently with itself or with `close`; the
    other methods only read.
    """

    def __init__(
        self,
        sftp: asyncssh.SFTPClient,
        directory: PurePosixPath,
        token: str,
        *,
        ports: tuple[int, ...],
    ) -> None:
        self._sftp = sftp
        self.directory = directory
        self._token = token
        self._ports = frozenset(ports)
        self._sequence = 0
        self._leases: list[PurePosixPath] = []

    async def create(self) -> None:
        """Create the directory, in a parent that only the user can enter."""
        root = self.directory.parent
        await ensure_directory(self._sftp, root)
        await self._sftp.chmod(root, 0o700)
        await self._sftp.mkdir(
            self.directory, attrs=asyncssh.SFTPAttrs(permissions=0o700)
        )

    async def publish_lease(self) -> int:
        """Write the next lease, keeping only the latest few; return its sequence."""
        self._sequence += 1
        name = f"lease.{self._sequence:020d}"
        temporary = self.directory / f".{name}.tmp"
        lease = self.directory / name
        try:
            async with self._sftp.open(
                temporary, "w", attrs=asyncssh.SFTPAttrs(permissions=0o600)
            ) as file:
                await file.write(f"{self._token} {self._sequence}\n")
            # The worker never sees a lease half written.
            await self._sftp.posix_rename(temporary, lease)
        except asyncssh.Error, OSError:
            with contextlib.suppress(asyncssh.Error, OSError):
                await self._sftp.remove(temporary)
            raise
        self._leases.append(lease)
        while len(self._leases) > _LEASE_FILE_RETAIN_COUNT:
            stale = self._leases.pop(0)
            try:
                await self._sftp.remove(stale)
            except asyncssh.SFTPError as error:
                logger.debug(
                    t"Could not remove stale worker lease {stale:path}: {error!r}"
                )
        return self._sequence

    async def read_status(self) -> WorkerStatus | None:
        """The worker's latest status, or None until it has written one."""
        try:
            # Bytes that aren't UTF-8 are replaced rather than raise, so a garbled
            # record reads as an invalid one.
            async with self._sftp.open(
                self.directory / "status", errors="replace"
            ) as file:
                fields = str(await file.read()).split()
        except asyncssh.SFTPNoSuchFile:
            return None
        if fields[:1] != [self._token]:
            raise WorkerError("worker wrote an invalid status record")
        match fields[1:]:
            case ["ready", port]:
                return WorkerReady(self._ready_port(port))
            case ["failed", *reason]:
                return WorkerFailed(" ".join(reason) or "worker startup failed")
            case ["stopped", exit_status] if exit_status.isdigit():
                return WorkerStopped(int(exit_status))
            case _:
                raise WorkerError("worker wrote an invalid status record")

    def _ready_port(self, field: str) -> int:
        try:
            port = int(field)
        except ValueError as error:
            raise WorkerError("worker reported a non-numeric SSH port") from error
        if port not in self._ports:
            raise WorkerError(f"worker reported unexpected SSH port {port}")
        return port

    async def close(self) -> None:
        """
        Remove the directory and everything in it. It is this run's alone, and may
        hold more than the tunnel tracks: the worker's status, and a lease that
        was being written when the heartbeats were cancelled.
        """
        await self._sftp.rmtree(self.directory, ignore_errors=True)
