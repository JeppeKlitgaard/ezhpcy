"""The tunnel's side of the worker's control directory (see `ezhpcy.worker`)."""

import logging
lazy import contextlib
lazy from pathlib import PurePosixPath

lazy import asyncssh

lazy from ezhpcy.ssh import ensure_directory
lazy from ezhpcy.worker import WorkerError

logger = logging.getLogger(__name__)

_LEASE_FILE_RETAIN_COUNT = 2


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
                logger.debug("Could not remove stale worker lease %s: %r", stale, error)
        return self._sequence

    async def read_ready_port(self) -> int | None:
        """The port the worker's sshd listens on, once the worker has said so."""
        fields = await self._read_record("ready")
        if fields is None:
            return None
        if len(fields) != 4 or fields[:3] != ["v1", self._token, "ready"]:
            raise WorkerError("worker wrote an invalid ready record")
        try:
            port = int(fields[3])
        except ValueError as error:
            raise WorkerError("worker reported a non-numeric SSH port") from error
        if port not in self._ports:
            raise WorkerError(f"worker reported unexpected SSH port {port}")
        return port

    async def read_failure(self) -> str | None:
        """Why the worker couldn't start sshd, if it has said so."""
        fields = await self._read_record("failed")
        if fields is None:
            return None
        if len(fields) < 3 or fields[:3] != ["v1", self._token, "failed"]:
            return "worker wrote an invalid failure record"
        return " ".join(fields[3:]) or "worker startup failed"

    async def close(self) -> None:
        """
        Remove the directory and everything in it. It is this run's alone, and may
        hold more than the tunnel tracks: the worker's records, and a lease that
        was being written when the heartbeats were cancelled.
        """
        await self._sftp.rmtree(self.directory, ignore_errors=True)

    async def _read_record(self, name: str) -> list[str] | None:
        """The fields of one of the worker's files, or None if it doesn't exist."""
        try:
            # Bytes that aren't UTF-8 are replaced rather than raise, so a garbled
            # record reads as an invalid one.
            async with self._sftp.open(self.directory / name, errors="replace") as file:
                text = await file.read()
        except asyncssh.SFTPNoSuchFile:
            return None
        return str(text).split()
