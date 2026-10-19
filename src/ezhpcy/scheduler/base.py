import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar
lazy import asyncio
lazy import shlex
lazy from collections.abc import Awaitable, Callable, Sequence
lazy from datetime import timedelta
lazy from pathlib import PurePosixPath

lazy import asyncssh

lazy from ezhpcy.scheduler.types import SchedulerType
lazy from ezhpcy.ssh import RemoteHost

# How much output to read from an interactive submission at a time.
_READ_BYTES = 64 * 1024


class JobState(StrEnum):
    """Scheduler-neutral states that callers can make decisions from."""

    PENDING = "pending"
    RUNNING = "running"
    SUSPENDED = "suspended"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    # Ended, but the scheduler no longer says how: it has dropped the job.
    ENDED = "ended"
    UNKNOWN = "unknown"

    @property
    def is_terminal(self) -> bool:
        return self in {JobState.SUCCEEDED, JobState.FAILED, JobState.ENDED}


@dataclass(frozen=True, slots=True)
class JobSpec:
    """A job to run on one execution host. The configuration validates it."""

    command: tuple[str, ...]
    name: str = "ezhpcy"
    time_limit: timedelta | None = None
    memory_bytes: int | None = None
    cores: int = 1
    gpus: int = 0
    exclusive: bool = False
    queue: str | None = None
    working_directory: PurePosixPath | None = None
    stdout_path: PurePosixPath | None = None
    stderr_path: PurePosixPath | None = None


@dataclass(frozen=True, slots=True)
class JobInfo:
    """A scheduler-neutral snapshot of a job."""

    state: JobState
    raw_state: str
    # Where the job runs, once it has started.
    host: str | None = None
    exit_code: int | None = None
    # Why the scheduler hasn't started it yet.
    pending_reason: str | None = None


class SchedulerError(RuntimeError):
    """A scheduler command failed, or its output made no sense."""


@dataclass(slots=True)
class InteractiveJob:
    """A job whose shell is an interactive submission on the login node."""

    job_id: str
    process: asyncssh.SSHClientProcess[bytes]
    submission_command: tuple[str, ...]
    # The line that `start_command` sends to the job's shell.
    shell_command: str
    # What the submission printed up to and including its job ID, which may run
    # on past the ID. `drain` passes it on first.
    early_output: bytes = b""

    async def start_command(self) -> None:
        """Start the job's command in its shell."""
        try:
            self.process.stdin.write(f"{self.shell_command}\n".encode())
            await self.process.stdin.drain()
        except (asyncssh.Error, OSError) as error:
            raise SchedulerError(
                f"interactive shell did not accept the worker command: {error}"
            ) from error

    async def drain(self, sink: Callable[[bytes], Awaitable[object]]) -> None:
        """
        Pass the process's output (stdout and stderr: it runs under a terminal)
        to `sink` until the process ends, starting with `early_output`. A chunk
        may end partway through a character. The next chunk is read once `sink`
        has taken the last.
        """
        if self.early_output:
            await sink(self.early_output)
        while output := await self.process.stdout.read(_READ_BYTES):
            await sink(output)

    def close(self) -> None:
        self.process.close()


class Scheduler(ABC):
    """
    Runs a scheduler's commands on the login node. Adapters build the commands
    and parse their output.
    """

    scheduler_type: ClassVar[SchedulerType]
    # Finds the job ID, as group `job_id`, in an interactive submission's output.
    interactive_job_id_pattern: ClassVar[re.Pattern[str]]

    def __init__(
        self,
        host: RemoteHost,
        *,
        interactive_submission_command: Sequence[str] | None = None,
    ) -> None:
        self._host = host
        self._interactive_submission_command = (
            tuple(interactive_submission_command)
            if interactive_submission_command is not None
            else None
        )

    @abstractmethod
    async def submit(self, spec: JobSpec) -> str:
        """Submit a batch job and return its scheduler-assigned identifier."""

    async def submit_interactive(
        self, spec: JobSpec, *, startup_timeout: float
    ) -> InteractiveJob:
        """
        Start an interactive submission and wait up to `startup_timeout` seconds
        for its job ID. The job's shell then waits for
        `InteractiveJob.start_command`.
        """
        command = (
            list(self._interactive_submission_command)
            if self._interactive_submission_command is not None
            else self._interactive_command(spec)
        )
        try:
            process = await self._host.start_login_shell(command)
        except (asyncssh.Error, OSError) as error:
            raise SchedulerError(
                f"{self.scheduler_type} failed to start interactive submission: {error}"
            ) from error

        try:
            job_id, early_output = await self._read_job_id(process, startup_timeout)
        except BaseException:
            process.close()
            raise
        return InteractiveJob(
            job_id=job_id,
            process=process,
            submission_command=tuple(command),
            shell_command=self._job_shell_command(spec),
            early_output=early_output,
        )

    @abstractmethod
    async def inspect(self, job_id: str) -> JobInfo:
        """Return the current scheduler snapshot for a job."""

    async def cancel(self, job_id: str) -> None:
        """Request cancellation of a job."""
        await self._run(self._cancel_command(job_id), f"cancel job {job_id!r}")

    # Adapter hooks

    @abstractmethod
    def _interactive_command(self, spec: JobSpec) -> list[str]:
        """The command that submits an interactive job and opens its shell."""

    @abstractmethod
    def _cancel_command(self, job_id: str) -> list[str]: ...

    # Shared by adapters

    async def _run(
        self, command: list[str], operation: str, *, stdin: str | None = None
    ) -> str:
        """
        Run a command in the login shell and return its stdout. A failure's
        `__cause__` is asyncssh's error.
        """
        try:
            return await self._host.run_login_shell(command, stdin=stdin)
        except (asyncssh.Error, OSError) as error:
            raise SchedulerError(
                f"{self.scheduler_type} failed to {operation}: {error}"
            ) from error

    @staticmethod
    def _job_shell_command(spec: JobSpec) -> str:
        command = shlex.join(["exec", *spec.command])
        if spec.working_directory is None:
            return command
        change_directory = shlex.join(["cd", str(spec.working_directory)])
        return f"{change_directory} && {command}"

    async def _read_job_id(
        self, process: asyncssh.SSHClientProcess[bytes], startup_timeout: float
    ) -> tuple[str, bytes]:
        """The job ID, and all the output read to find it."""
        output = bytearray()
        try:
            async with asyncio.timeout(startup_timeout):
                while True:
                    decoded = output.decode(errors="replace")
                    if match := self.interactive_job_id_pattern.search(decoded):
                        return match.group("job_id"), bytes(output)
                    chunk = await process.stdout.read(_READ_BYTES)
                    if not chunk:
                        await process.wait_closed()
                        raise SchedulerError(
                            f"{self.scheduler_type} interactive submission exited "
                            f"before returning a job ID ({process.exit_status}): "
                            f"{decoded.strip()}"
                        )
                    output.extend(chunk)
        except TimeoutError:
            raise SchedulerError(
                f"{self.scheduler_type} interactive submission did not return a job "
                f"ID within {startup_timeout:g} seconds: "
                f"{output.decode(errors='replace').strip()}"
            ) from None
