from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
lazy import os

from cyclopts import Group, Parameter, validators

from ezhpcy.constants import PACKAGE_NAME
from ezhpcy.scheduler.types import SchedulerType
from ezhpcy.types import SubmissionMode

KEYRING_SERVICE_NAME = PACKAGE_NAME

DEBUG_ENV_VAR = "EZHPCY_DEBUG"
HOST_ENV_VAR = "EZHPCY_HOST"
USER_ENV_VAR = "EZHPCY_USER"
PROFILE_ENV_VAR = "EZHPCY_PROFILE"
PASSWORD_ENV_VAR = "EZHPCY_PASSWORD"
PASSWORD_FILE_ENV_VAR = "EZHPCY_PASSWORD_FILE"
PASSWORD_FD_ENV_VAR = "EZHPCY_PASSWORD_FD"
PASSWORD_KEYRING_ENV_VAR = "EZHPCY_PASSWORD_KEYRING"

CONNECTION_PANEL = Group("Connection Options", sort_key=1)
SCHEDULER_PANEL = Group("Scheduler Options", sort_key=2)
RESOURCES_PANEL = Group("Resource Options", sort_key=3)
TIMINGS_PANEL = Group("Timing Options", sort_key=4)


def _readable(_type: type, value: Path | None) -> None:
    """Reject a path the current user can't read."""
    if value is not None and not os.access(value, os.R_OK):
        raise ValueError(f'"{value}" is not readable.')


OptionalProfileArg = Annotated[
    str | None,
    Parameter(
        help=(
            "Optional configured EzHPCy profile. When omitted, provide enough "
            "options to form a complete configuration. Run `ezhpcy list-profiles` "
            "to see available profiles."
        ),
        env_var=PROFILE_ENV_VAR,
    ),
]
AliasArg = Annotated[
    str,
    Parameter(
        help=(
            "SSH host alias of a running tunnel; usually its profile name "
            "(see `ezhpcy list-profiles`)."
        ),
    ),
]
AliasOpt = Annotated[
    str | None,
    Parameter(
        name="--alias",
        help=(
            "SSH host alias for this tunnel; defaults to the profile name, "
            "or to a stable generated name without a profile."
        ),
    ),
]
HostOpt = Annotated[
    str | None,
    Parameter(
        name=["--host", "-h"],
        help="Login node address.",
        env_var=HOST_ENV_VAR,
        group=CONNECTION_PANEL,
    ),
]
UserOpt = Annotated[
    str | None,
    Parameter(
        name=["--user", "-u"],
        help="Username for the login node.",
        env_var=USER_ENV_VAR,
        group=CONNECTION_PANEL,
    ),
]
PasswordOpt = Annotated[
    str | None,
    Parameter(
        name="--password",
        help=(
            "Password for the login node. "
            "Note: Specifying this is potentially a security risk."
        ),
        env_var=PASSWORD_ENV_VAR,
        group=CONNECTION_PANEL,
    ),
]
PasswordFileOpt = Annotated[
    Path | None,
    Parameter(
        name="--password-file",
        validator=(validators.Path(exists=True, dir_okay=False), _readable),
        help="Read the password from a UTF-8 file.",
        env_var=PASSWORD_FILE_ENV_VAR,
        group=CONNECTION_PANEL,
    ),
]
PasswordFdOpt = Annotated[
    int | None,
    Parameter(
        name="--password-fd",
        validator=validators.Number(gte=0),
        help="Read the password from an already-open file descriptor.",
        env_var=PASSWORD_FD_ENV_VAR,
        group=CONNECTION_PANEL,
    ),
]
PasswordKeyringOpt = Annotated[
    bool,
    Parameter(
        name="--password-keyring",
        help=(
            "Read the password from the system keyring service "
            f"'{KEYRING_SERVICE_NAME}' under USER@HOST."
        ),
        env_var=PASSWORD_KEYRING_ENV_VAR,
        group=CONNECTION_PANEL,
    ),
]
SchedulerOpt = Annotated[
    SchedulerType | None,
    Parameter(
        name="--scheduler",
        help="Scheduler used to allocate the compute node.",
        group=SCHEDULER_PANEL,
    ),
]
SubmissionModeOpt = Annotated[
    SubmissionMode | None,
    Parameter(
        name="--submission-mode",
        help="Use an interactive shell or an ordinary batch scheduler job.",
        group=SCHEDULER_PANEL,
    ),
]
QueueOpt = Annotated[
    str | None,
    Parameter(
        name=["--queue", "-q"],
        help="Scheduler queue for the worker job.",
        group=RESOURCES_PANEL,
    ),
]
CoresOpt = Annotated[
    int | None,
    Parameter(
        name=["--cores", "-n"],
        validator=validators.Number(gte=1),
        help="Scheduler CPU cores reserved on the worker host.",
        group=RESOURCES_PANEL,
    ),
]
GpusOpt = Annotated[
    int | None,
    Parameter(
        name="--gpus",
        validator=validators.Number(gte=0),
        help="Number of GPUs reserved on the worker host.",
        group=RESOURCES_PANEL,
    ),
]
ExclusiveOpt = Annotated[
    bool | None,
    Parameter(
        name="--exclusive",
        negative="--shared",
        help="Reserve the worker host exclusively.",
        group=RESOURCES_PANEL,
    ),
]
TimeLimitOpt = Annotated[
    str | None,
    Parameter(
        name="--time-limit",
        metavar="H:MM",
        help="Optional worker lifetime override.",
        group=RESOURCES_PANEL,
    ),
]
MemoryOpt = Annotated[
    str | None,
    Parameter(
        name="--memory",
        metavar="SIZE",
        help=(
            "Optional total worker memory parsed as a Pydantic byte size. "
            "Bare values are bytes; SI (GB) and IEC (GiB) units differ."
        ),
        group=RESOURCES_PANEL,
    ),
]
QueueTimeoutOpt = Annotated[
    float | None,
    Parameter(
        name="--queue-timeout",
        validator=validators.Number(gte=1),
        help="Maximum time to wait for the scheduler allocation.",
        group=TIMINGS_PANEL,
    ),
]
StartupTimeoutOpt = Annotated[
    float | None,
    Parameter(
        name="--startup-timeout",
        validator=validators.Number(gte=1),
        help="Maximum time to wait for worker SSH after allocation.",
        group=TIMINGS_PANEL,
    ),
]
JobPollIntervalOpt = Annotated[
    float | None,
    Parameter(
        name="--job-poll-interval",
        validator=validators.Number(gte=0.1),
        help="Interval between scheduler and worker-readiness startup checks.",
        group=TIMINGS_PANEL,
    ),
]
JobMonitorIntervalOpt = Annotated[
    float | None,
    Parameter(
        name="--job-monitor-interval",
        validator=validators.Number(gte=0.1),
        help="Interval between scheduler checks after the tunnel is ready.",
        group=TIMINGS_PANEL,
    ),
]
WorkerHeartbeatIntervalOpt = Annotated[
    float | None,
    Parameter(
        name="--worker-heartbeat-interval",
        validator=validators.Number(gte=1),
        help="Interval between worker lease heartbeats.",
        group=TIMINGS_PANEL,
    ),
]
WorkerHeartbeatTimeoutOpt = Annotated[
    float | None,
    Parameter(
        name="--worker-heartbeat-timeout",
        validator=validators.Number(gte=1),
        help="Maximum time the worker may go without a lease heartbeat.",
        group=TIMINGS_PANEL,
    ),
]
InteractiveSubmissionCommandOpt = Annotated[
    str | None,
    Parameter(
        name="--interactive-submission-command",
        metavar="COMMAND",
        help=(
            "Replace the scheduler-generated interactive submission command. "
            "The value is parsed into arguments using shell-style quoting, but "
            "is not executed through a shell."
        ),
        group=SCHEDULER_PANEL,
    ),
]


# One dataclass per option panel. `name="*"` flattens its fields into top-level
# options of the command that takes it.
#
# Commands give a default instance, since Cyclopts passes `None`
# when none of the fields were given.
# They're frozen, so the default is safe to share
# ruff's B008 rule does not detect this, so we locally ignore it at call-sites.
# See: https://github.com/astral-sh/ruff/issues/29071
# Note: Should remove ignore from all call-sites when this is fixed
#
# They're dataclasses rather than Pydantic models on purpose:
# Cyclopts hands a model the raw strings and shows Pydantic's own, uglier error
@Parameter(name="*")
@dataclass(frozen=True, kw_only=True)
class ConnectionOptions:
    host: HostOpt = None
    user: UserOpt = None
    password: PasswordOpt = None
    password_file: PasswordFileOpt = None
    password_fd: PasswordFdOpt = None
    password_keyring: PasswordKeyringOpt = False


@Parameter(name="*")
@dataclass(frozen=True, kw_only=True)
class SchedulerOptions:
    scheduler_type: SchedulerOpt = None
    submission_mode: SubmissionModeOpt = None
    interactive_submission_command: InteractiveSubmissionCommandOpt = None


@Parameter(name="*")
@dataclass(frozen=True, kw_only=True)
class ResourceOptions:
    queue: QueueOpt = None
    cores: CoresOpt = None
    gpus: GpusOpt = None
    exclusive: ExclusiveOpt = None
    time_limit: TimeLimitOpt = None
    memory: MemoryOpt = None


@Parameter(name="*")
@dataclass(frozen=True, kw_only=True)
class TimingOptions:
    queue_timeout_seconds: QueueTimeoutOpt = None
    startup_timeout_seconds: StartupTimeoutOpt = None
    job_poll_interval_seconds: JobPollIntervalOpt = None
    job_monitor_interval_seconds: JobMonitorIntervalOpt = None
    worker_heartbeat_interval_seconds: WorkerHeartbeatIntervalOpt = None
    worker_heartbeat_timeout_seconds: WorkerHeartbeatTimeoutOpt = None
