"""
The worker job's command, and the control directory it shares with the tunnel.

The tunnel and the worker talk through files in a directory on the cluster's
shared filesystem, which the tunnel reaches over SFTP:

- The tunnel renews a lease (`lease.<sequence>`) every heartbeat interval. The
  worker stops sshd once no newer lease has appeared within the heartbeat
  timeout, so a worker never outlives its tunnel by long.
- The worker writes `ready` with the port sshd listens on, or `failed` with the
  reason, and `stopped` with its exit status when it ends.

Every file names the run's token, so one from another run is never taken for
this run's.
"""

import logging
lazy import contextlib
lazy import math
lazy import secrets
lazy import shlex
lazy from pathlib import PurePosixPath

lazy import asyncssh

lazy from ezhpcy.constants import OPENSSH_MATCHSPEC
lazy from ezhpcy.tunnel import TunnelError
lazy from ezhpcy.tunnel.sshd import retrying_sshd_command
lazy from ezhpcy.types import RemoteState

logger = logging.getLogger(__name__)

_FIRST_DYNAMIC_PORT = 49152
_LAST_DYNAMIC_PORT = 65535
_LEASE_FILE_RETAIN_COUNT = 2
_SCRIPT_DELIMITER = "EZHPCY_WORKER_SCRIPT"


def select_worker_ports(count: int) -> tuple[int, ...]:
    """`count` distinct random ports from the dynamic range, for sshd to try in turn."""
    ports: list[int] = []
    while len(ports) < count:
        port = _FIRST_DYNAMIC_PORT + secrets.randbelow(
            _LAST_DYNAMIC_PORT - _FIRST_DYNAMIC_PORT + 1
        )
        if port not in ports:
            ports.append(port)
    return tuple(ports)


def worker_client_alive(
    heartbeat_interval_seconds: float, heartbeat_timeout_seconds: int
) -> tuple[int, int]:
    """
    Bound how long the worker serves an SSH client that stopped answering.

    A connection that was open when the login session died must end even if
    the lease watchdog cannot signal its handler. The probes reuse the
    heartbeat cadence and give up within the heartbeat timeout.
    """
    interval_seconds = max(1, math.ceil(heartbeat_interval_seconds))
    count_max = max(1, heartbeat_timeout_seconds // interval_seconds)
    return interval_seconds, count_max


def worker_sshd_command(
    remote_state: RemoteState,
    *,
    ports: tuple[int, ...],
    heartbeat_token: str,
    heartbeat_timeout_seconds: int,
    heartbeat_debug: bool,
    control_dir: PurePosixPath,
    read_script_from_stdin: bool,
    sshd_arguments: list[str],
) -> tuple[str, ...]:
    """The worker job's command: sshd from the cached Pixi environment."""
    return remote_state.pixi_command(
        "exec",
        f"--spec={OPENSSH_MATCHSPEC}",
        *retrying_sshd_command(
            sshd_arguments,
            ports,
            heartbeat_token=heartbeat_token,
            heartbeat_timeout_seconds=heartbeat_timeout_seconds,
            heartbeat_debug=heartbeat_debug,
            heartbeat_log_dir=remote_state.worker_logs_dir(),
            control_dir=control_dir,
            read_script_from_stdin=read_script_from_stdin,
        ),
    )


def worker_job_script(command: tuple[str, ...], worker_script: str) -> str:
    """Wrap the worker shell program in a batch script fed to LSF over stdin."""
    if f"\n{_SCRIPT_DELIMITER}\n" in worker_script:
        raise ValueError("worker script conflicts with its here-document delimiter")
    return (
        "#!/usr/bin/env bash\n"
        f"exec {shlex.join(command)} <<'{_SCRIPT_DELIMITER}'\n"
        f"{worker_script.rstrip()}\n"
        f"{_SCRIPT_DELIMITER}\n"
    )


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
        await self._sftp.makedirs(root, exist_ok=True)
        await self._sftp.chmod(root, 0o700)
        await self._sftp.makedirs(
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
            raise TunnelError("worker wrote an invalid ready record")
        try:
            port = int(fields[3])
        except ValueError as error:
            raise TunnelError("worker reported a non-numeric SSH port") from error
        if port not in self._ports:
            raise TunnelError(f"worker reported unexpected SSH port {port}")
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
