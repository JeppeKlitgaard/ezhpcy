"""The tunnel's side of the worker's control directory (see `ezhpcy.worker`)."""

from dataclasses import dataclass
from typing import Literal
lazy import contextlib
lazy from pathlib import PurePosixPath

lazy import asyncssh

from ezhpcy.logging import get_logger
lazy from ezhpcy.ssh import ensure_directory
lazy from ezhpcy.worker import WorkerError

logger = get_logger(__name__)

_LEASE_FILE_RETAIN_COUNT = 2
# The job's record, for a later client to find it by: its ID, scheduler and
# submission mode, and once ready, its sshd's port (JSON).
JOB_RECORD_NAME = "job.json"


@dataclass(frozen=True, slots=True)
class WorkerReady:
    """
    sshd listens on `port`. With an app, the app has started, and listens on
    `app`: the run directory's socket (`"socket"`), or a TCP port on 127.0.0.1.
    """

    port: int
    app: int | Literal["socket"] | None = None


@dataclass(frozen=True, slots=True)
class WorkerFailed:
    """sshd or the app couldn't start, for `reason`."""

    reason: str

    @property
    def what(self) -> str:
        """What couldn't start, as messages name it."""
        return "app" if self.reason.startswith("app_") else "worker SSH daemon"


@dataclass(frozen=True, slots=True)
class WorkerStopped:
    """`process` has exited with `exit_status`, and the worker with it."""

    process: Literal["sshd", "app"]
    exit_status: int

    @property
    def what(self) -> str:
        """The process, as messages name it."""
        return "worker SSH daemon" if self.process == "sshd" else "app"


type WorkerStatus = WorkerReady | WorkerFailed | WorkerStopped


class WorkerControl:
    """
    The tunnel's side of one worker's control directory.

    `publish_lease` must not run concurrently with itself or with `close`, nor
    `write_record` with itself for the same file, or with `close`; the other
    methods only read.
    """

    def __init__(
        self,
        sftp: asyncssh.SFTPClient,
        directory: PurePosixPath,
        token: str,
        *,
        ports: tuple[int, ...],
        app_ports: tuple[int, ...] = (),
    ) -> None:
        self._sftp = sftp
        self.directory = directory
        self._token = token
        self._ports = frozenset(ports)
        self._app_ports = frozenset(app_ports)
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
        await self.write_record(name, f"{self._token} {self._sequence}\n")
        lease = self.directory / name
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

    async def write_record(self, name: str, content: str) -> None:
        """
        Write `content` to the file `name` in the directory, replacing it whole:
        a reader never sees it half written.
        """
        temporary = self.directory / f".{name}.tmp"
        try:
            async with self._sftp.open(
                temporary, "w", attrs=asyncssh.SFTPAttrs(permissions=0o600)
            ) as file:
                await file.write(content)
            await self._sftp.posix_rename(temporary, self.directory / name)
        except asyncssh.Error, OSError:
            with contextlib.suppress(asyncssh.Error, OSError):
                await self._sftp.remove(temporary)
            raise

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
            raise WorkerError(t"worker wrote an invalid status record")
        match fields[1:]:
            case ["ready", port]:
                return WorkerReady(self._ready_port(port, self._ports, "SSH"))
            case ["ready", port, "socket"]:
                return WorkerReady(self._ready_port(port, self._ports, "SSH"), "socket")
            case ["ready", port, app_port]:
                return WorkerReady(
                    self._ready_port(port, self._ports, "SSH"),
                    self._ready_port(app_port, self._app_ports, "app"),
                )
            case ["failed", *reason]:
                return WorkerFailed(" ".join(reason) or "worker startup failed")
            case ["stopped", "sshd" | "app" as process, exit_status] if (
                exit_status.isdigit()
            ):
                return WorkerStopped(process, int(exit_status))
            case _:
                raise WorkerError(t"worker wrote an invalid status record")

    def _ready_port(self, field: str, expected: frozenset[int], what: str) -> int:
        try:
            port = int(field)
        except ValueError as error:
            raise WorkerError(t"worker reported a non-numeric {what} port") from error
        if port not in expected:
            raise WorkerError(t"worker reported unexpected {what} port {port:detail}")
        return port

    async def close(self) -> None:
        """
        Remove the directory and everything in it. It is this run's alone, and may
        hold more than the tunnel tracks: the worker's status, and a lease that
        was being written when the heartbeats were cancelled.
        """
        await self._sftp.rmtree(self.directory, ignore_errors=True)
