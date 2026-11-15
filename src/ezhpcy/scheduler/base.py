import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar
lazy import asyncio
lazy import secrets
lazy import shlex
lazy from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
lazy from datetime import UTC, datetime, timedelta
lazy from pathlib import PurePosixPath
lazy from string.templatelib import Template

lazy import asyncssh

from ezhpcy.messages import MessageError
lazy from ezhpcy.scheduler.queues import Cluster, parse_scan, scan_command
lazy from ezhpcy.scheduler.types import SchedulerType
lazy from ezhpcy.ssh import CommandFailed, RemoteHost
lazy from ezhpcy.types import SiteConfig

# How much output to read from an interactive submission at a time.
_READ_BYTES = 64 * 1024
# The wrapper scan's output, among an adapter's commands.
_WRAPPERS = "wrappers"


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
        """Whether the job has ended."""
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


class SchedulerError(MessageError, RuntimeError):
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
                t"interactive shell did not accept the worker command: {error}"
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
        site: SiteConfig,
        *,
        interactive_submission_command: Sequence[str] | None = None,
    ) -> None:
        self._host = host
        self._site = site
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
                t"{self.scheduler_type} failed to start interactive submission: {error}"
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
        await self._run([self.cancel_executable, job_id], t"cancel job {job_id:detail}")

    @abstractmethod
    async def describe_queues(self, *, only_available_to_me: bool = False) -> Cluster:
        """
        Describe the cluster's hosts and queues as they are for the login user,
        with the site's wrappers (in its `wrapper_directories`) among the ways to
        submit to them. With `only_available_to_me`, only the queues that would
        run the user's jobs, and their hosts.
        """

    @property
    @abstractmethod
    def cancel_executable(self) -> str:
        """
        The command that cancels the job whose ID follows it, on the login node
        or on the job's own node: the worker cancels its job with it too.
        """

    # Adapter hooks

    @abstractmethod
    def _interactive_command(self, spec: JobSpec) -> list[str]:
        """The command that submits an interactive job and opens its shell."""

    # Shared by adapters

    async def _run(
        self, command: list[str], operation: Template, *, stdin: str | None = None
    ) -> str:
        """
        Run a command in the login shell and return its stdout. A failure's
        `__cause__` is the command's (`CommandFailed`), or asyncssh's error.
        """
        try:
            return await self._host.run_login_shell(command, stdin=stdin)
        except (CommandFailed, asyncssh.Error, OSError) as error:
            raise SchedulerError(
                t"{self.scheduler_type} failed to {operation}: {error}"
            ) from error

    async def _run_all(
        self, commands: Mapping[str, Sequence[str]], *, optional: Collection[str] = ()
    ) -> dict[str, str | None]:
        """
        Run `commands` at once, in one login shell, and return their stdout. A
        login shell can take longer to start than the commands take to run, so
        one is started for them all. An `optional` command that fails gives
        `None`, as one may where the site has nothing to list, such as no GPUs.
        """
        # Separates the commands' output, which can't contain it.
        marker = f"ezhpcy-{secrets.token_hex(16)}"
        keys = list(commands)
        script = [
            "directory=$(mktemp -d) || exit",
            """trap 'rm -rf "$directory"' EXIT""",
        ]
        for index, key in enumerate(keys):
            output = f'"$directory/{index}"'
            script.append(
                f"{{ {shlex.join(commands[key])}; echo $? > {output}.status; }}"
                f" < /dev/null > {output} 2> {output}.stderr &"
            )
        script.append("wait")
        for index in range(len(keys)):
            output = f'"$directory/{index}"'
            script.append(
                f'printf "\\n%s %s\\n" {marker} "$(cat {output}.status)"; cat {output};'
                f' printf "\\n%s\\n" {marker}-stderr; cat {output}.stderr'
            )
        output = await self._run(
            ["bash", "-c", "\n".join(script)], t"run {len(keys):detail} commands"
        )

        results: dict[str, str | None] = {}
        sections = output.split(f"\n{marker} ")[1:]
        if len(sections) != len(keys):
            raise SchedulerError(
                t"{self.scheduler_type} printed the output of {len(sections):detail} "
                t"of its {len(keys):detail} commands"
            )
        for key, section in zip(keys, sections, strict=True):
            status, _, rest = section.partition("\n")
            stdout, _, stderr = rest.partition(f"\n{marker}-stderr\n")
            if status == "0":
                results[key] = stdout
                continue
            command = shlex.join(commands[key])
            error = CommandFailed(
                command,
                exit_status=int(status) if status.isdigit() else None,
                exit_signal=None,
                stdout=stdout,
                stderr=stderr,
            )
            if key not in optional:
                raise SchedulerError(
                    t"{self.scheduler_type} failed to run {command:detail}: {error}"
                ) from error
            results[key] = None
        return results

    async def _describe_queues(
        self,
        commands: Mapping[str, Sequence[str]],
        *,
        optional: Collection[str],
        parse: Callable[..., Cluster],
        only_available_to_me: bool,
    ) -> Cluster:
        """Run an adapter's `commands` and the wrapper scan, and `parse` them."""
        if directories := self._site.wrapper_directories:
            commands = {**commands, _WRAPPERS: scan_command(directories)}
        generated_at = datetime.now(UTC)
        outputs = await self._run_all(commands, optional=optional)
        cluster = parse(
            outputs,
            user=self._host.login.user,
            scripts=parse_scan(outputs.pop(_WRAPPERS, None) or ""),
            generated_at=generated_at,
        )
        return cluster.only_available() if only_available_to_me else cluster

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
                            t"{self.scheduler_type} interactive submission exited "
                            t"before returning a job ID "
                            t"({process.exit_status:detail}): {decoded.strip()}"
                        )
                    output.extend(chunk)
        except TimeoutError:
            raise SchedulerError(
                t"{self.scheduler_type} interactive submission did not return a job "
                t"ID within {startup_timeout:detail:g} seconds: "
                t"{output.decode(errors='replace').strip()}"
            ) from None
