from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Protocol

from ezhpcy.scheduler.types import SchedulerType


class JobState(StrEnum):
    """Scheduler-neutral states that callers can make decisions from."""

    PENDING = "pending"
    RUNNING = "running"
    SUSPENDED = "suspended"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"

    @property
    def is_terminal(self) -> bool:
        return self in {JobState.SUCCEEDED, JobState.FAILED}


@dataclass(frozen=True, slots=True)
class JobSpec:
    """Portable settings for a job constrained to one execution host."""

    command: Sequence[str]
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
    environment: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        command = tuple(self.command)
        if not command or any(not part or "\0" in part for part in command):
            raise ValueError("command must contain non-empty, NUL-free arguments")
        if not self.name or "\0" in self.name:
            raise ValueError("name must not be empty or contain NUL")
        if self.time_limit is not None and self.time_limit <= timedelta(0):
            raise ValueError("time_limit must be positive")
        if self.memory_bytes is not None and self.memory_bytes <= 0:
            raise ValueError("memory_bytes must be positive")
        if self.cores <= 0:
            raise ValueError("cores must be positive")
        if self.gpus < 0:
            raise ValueError("gpus must not be negative")
        if self.queue is not None and (not self.queue or "\0" in self.queue):
            raise ValueError("queue must not be empty or contain NUL")

        environment = dict(self.environment)
        for key, value in environment.items():
            if (
                not key
                or not key.replace("_", "a").isalnum()
                or key[0].isdigit()
                or "\0" in value
            ):
                raise ValueError(f"invalid environment variable: {key!r}")

        object.__setattr__(self, "command", command)
        object.__setattr__(self, "environment", MappingProxyType(environment))


@dataclass(frozen=True, slots=True)
class JobInfo:
    """A scheduler-neutral snapshot of a job."""

    job_id: str
    state: JobState
    raw_state: str
    execution_hosts: tuple[str, ...] = ()
    exit_code: int | None = None

    @property
    def primary_host(self) -> str | None:
        """Return the first allocated host, if the job has started."""
        return self.execution_hosts[0] if self.execution_hosts else None


class RemoteProcess(Protocol):
    """A running remote scheduler command with non-blocking output access."""

    def recv_ready(self) -> bool: ...

    def recv(self, size: int) -> bytes: ...

    def recv_stderr_ready(self) -> bool: ...

    def recv_stderr(self, size: int) -> bytes: ...

    def exit_status_ready(self) -> bool: ...

    def recv_exit_status(self) -> int: ...

    def send(self, data: bytes | str) -> int: ...

    def close(self) -> None: ...


@dataclass(slots=True)
class InteractiveJob:
    """A scheduler job attached to a live interactive submission process."""

    job_id: str
    process: RemoteProcess
    submission_output: str
    submission_command: tuple[str, ...] = ()
    payload_starter: Callable[[], None] | None = None
    payload_started: bool = field(default=False, init=False)

    def read_available(self, size: int = 64 * 1024) -> bytes:
        output = bytearray()
        while self.process.recv_ready():
            output.extend(self.process.recv(size))
        while self.process.recv_stderr_ready():
            output.extend(self.process.recv_stderr(size))
        return bytes(output)

    def close(self) -> None:
        self.process.close()

    def start_payload(self) -> None:
        if self.payload_started:
            return
        if self.payload_starter is not None:
            self.payload_starter()
        self.payload_started = True


class SchedulerError(RuntimeError):
    """Base class for scheduler adapter failures."""


class SchedulerCommandError(SchedulerError):
    """A scheduler command failed to execute successfully."""


class SchedulerOutputError(SchedulerError):
    """A scheduler returned output that the adapter could not parse."""


class JobNotFoundError(SchedulerError):
    """The scheduler no longer has a record for a referenced job."""

    def __init__(self, job_id: str) -> None:
        super().__init__(f"scheduler job {job_id!r} was not found")
        self.job_id = job_id


class UnsupportedSchedulerError(SchedulerError):
    """No adapter is registered for the requested scheduler type."""

    def __init__(self, scheduler_type: SchedulerType) -> None:
        super().__init__(f"unsupported scheduler: {scheduler_type.value}")
        self.scheduler_type = scheduler_type


class Scheduler(ABC):
    """Common lifecycle boundary implemented by scheduler adapters."""

    @property
    @abstractmethod
    def scheduler_type(self) -> SchedulerType:
        """The scheduler represented by this adapter."""

    @abstractmethod
    def submit(self, spec: JobSpec) -> str:
        """Submit a job and return its scheduler-assigned identifier."""

    @abstractmethod
    def submit_interactive(self, spec: JobSpec) -> InteractiveJob:
        """Submit a job attached to a live interactive scheduler process."""

    @abstractmethod
    def inspect(self, job_id: str) -> JobInfo:
        """Return the current scheduler snapshot for a job."""

    @abstractmethod
    def cancel(self, job_id: str) -> None:
        """Request cancellation of a job."""
