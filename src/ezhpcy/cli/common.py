import os
import shlex
from pathlib import Path
from typing import Annotated

import keyring
import typer
from keyring.errors import KeyringError
from pydantic import BaseModel, ValidationError

from ezhpcy.cli.utils.bad_parameter import RichBadParameter
from ezhpcy.cli.utils.options_group import attach_hook
from ezhpcy.cli.utils.resolve import resolve_forbidden_none
from ezhpcy.config import ProfilePasswordSourceError, config
from ezhpcy.constants import PACKAGE_NAME
from ezhpcy.scheduler.types import SchedulerType
from ezhpcy.types import (
    ConnectionInfo,
    ResolvedProfileConfig,
    ResourcesConfig,
    SchedulerConfig,
    SubmissionMode,
    TimingsConfig,
)

KEYRING_SERVICE_NAME = PACKAGE_NAME

DEBUG_ENV_VAR = "EZHPCY_DEBUG"
HOST_ENV_VAR = "EZHPCY_HOST"
USER_ENV_VAR = "EZHPCY_USER"
PROFILE_ENV_VAR = "EZHPCY_PROFILE"
PASSWORD_ENV_VAR = "EZHPCY_PASSWORD"
PASSWORD_FILE_ENV_VAR = "EZHPCY_PASSWORD_FILE"
PASSWORD_FD_ENV_VAR = "EZHPCY_PASSWORD_FD"
PASSWORD_KEYRING_ENV_VAR = "EZHPCY_PASSWORD_KEYRING"

CONNECTION_PANEL = "Connection Options"
SCHEDULER_PANEL = "Scheduler Options"
RESOURCES_PANEL = "Resource Options"
TIMINGS_PANEL = "Timing Options"

ProfileArg = Annotated[
    str,
    typer.Argument(
        help=(
            "Configured EzHPCy profile to use. Run `ezhpcy list-profiles` to see "
            "available profiles."
        ),
        envvar=PROFILE_ENV_VAR,
    ),
]
OptionalProfileArg = Annotated[
    str | None,
    typer.Argument(
        help=(
            "Optional configured EzHPCy profile. When omitted, provide enough "
            "options to form a complete configuration. Run `ezhpcy list-profiles` "
            "to see available profiles."
        ),
        envvar=PROFILE_ENV_VAR,
    ),
]
AliasArg = Annotated[
    str,
    typer.Argument(
        help=(
            "SSH host alias of a running tunnel; usually its profile name "
            "(see `ezhpcy list-profiles`)."
        ),
    ),
]
AliasOpt = Annotated[
    str | None,
    typer.Option(
        "--alias",
        help=(
            "SSH host alias for this tunnel; defaults to the profile name, "
            "or to a stable generated name without a profile."
        ),
    ),
]
HostOpt = Annotated[
    str | None,
    typer.Option(
        "--host",
        "-h",
        help="Login node address.",
        envvar=HOST_ENV_VAR,
        rich_help_panel=CONNECTION_PANEL,
    ),
]
UserOpt = Annotated[
    str | None,
    typer.Option(
        "--user",
        "-u",
        help="Username for the login node.",
        envvar=USER_ENV_VAR,
        rich_help_panel=CONNECTION_PANEL,
    ),
]
PasswordOpt = Annotated[
    str | None,
    typer.Option(
        "--password",
        help=(
            "Password for the login node. "
            "Note: Specifying this is potentially a security risk."
        ),
        envvar=PASSWORD_ENV_VAR,
        rich_help_panel=CONNECTION_PANEL,
    ),
]
PasswordFileOpt = Annotated[
    Path | None,
    typer.Option(
        "--password-file",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
        help="Read the password from a UTF-8 file.",
        envvar=PASSWORD_FILE_ENV_VAR,
        rich_help_panel=CONNECTION_PANEL,
    ),
]
PasswordFdOpt = Annotated[
    int | None,
    typer.Option(
        "--password-fd",
        min=0,
        help="Read the password from an already-open file descriptor.",
        envvar=PASSWORD_FD_ENV_VAR,
        rich_help_panel=CONNECTION_PANEL,
    ),
]
PasswordKeyringOpt = Annotated[
    bool,
    typer.Option(
        "--password-keyring",
        help=(
            "Read the password from the system keyring service "
            f"'{KEYRING_SERVICE_NAME}' under USER@HOST."
        ),
        envvar=PASSWORD_KEYRING_ENV_VAR,
        rich_help_panel=CONNECTION_PANEL,
    ),
]
SchedulerOpt = Annotated[
    SchedulerType | None,
    typer.Option(
        "--scheduler",
        case_sensitive=False,
        help="Scheduler used to allocate the compute node.",
        rich_help_panel=SCHEDULER_PANEL,
    ),
]
SubmissionModeOpt = Annotated[
    SubmissionMode | None,
    typer.Option(
        "--submission-mode",
        case_sensitive=False,
        help="Use an interactive shell or an ordinary batch scheduler job.",
        rich_help_panel=SCHEDULER_PANEL,
    ),
]
QueueOpt = Annotated[
    str | None,
    typer.Option(
        "--queue",
        "-q",
        help="Scheduler queue for the worker job.",
        rich_help_panel=RESOURCES_PANEL,
    ),
]
CoresOpt = Annotated[
    int | None,
    typer.Option(
        "--cores",
        "-n",
        min=1,
        help="Scheduler CPU cores reserved on the worker host.",
        rich_help_panel=RESOURCES_PANEL,
    ),
]
GpusOpt = Annotated[
    int | None,
    typer.Option(
        "--gpus",
        min=0,
        help="Number of GPUs reserved on the worker host.",
        rich_help_panel=RESOURCES_PANEL,
    ),
]
ExclusiveOpt = Annotated[
    bool | None,
    typer.Option(
        "--exclusive/--shared",
        help="Reserve the worker host exclusively.",
        rich_help_panel=RESOURCES_PANEL,
    ),
]
TimeLimitOpt = Annotated[
    str | None,
    typer.Option(
        "--time-limit",
        metavar="H:MM",
        help="Optional worker lifetime override.",
        rich_help_panel=RESOURCES_PANEL,
    ),
]
MemoryOpt = Annotated[
    str | None,
    typer.Option(
        "--memory",
        metavar="SIZE",
        help=(
            "Optional total worker memory parsed as a Pydantic byte size. "
            "Bare values are bytes; SI (GB) and IEC (GiB) units differ."
        ),
        rich_help_panel=RESOURCES_PANEL,
    ),
]
QueueTimeoutOpt = Annotated[
    float | None,
    typer.Option(
        "--queue-timeout",
        min=1,
        help="Maximum time to wait for the scheduler allocation.",
        rich_help_panel=TIMINGS_PANEL,
    ),
]
StartupTimeoutOpt = Annotated[
    float | None,
    typer.Option(
        "--startup-timeout",
        min=1,
        help="Maximum time to wait for worker SSH after allocation.",
        rich_help_panel=TIMINGS_PANEL,
    ),
]
JobPollIntervalOpt = Annotated[
    float | None,
    typer.Option(
        "--job-poll-interval",
        min=0.1,
        help="Interval between scheduler and worker-readiness startup checks.",
        rich_help_panel=TIMINGS_PANEL,
    ),
]
JobMonitorIntervalOpt = Annotated[
    float | None,
    typer.Option(
        "--job-monitor-interval",
        min=0.1,
        help="Interval between scheduler checks after the tunnel is ready.",
        rich_help_panel=TIMINGS_PANEL,
    ),
]
WorkerHeartbeatIntervalOpt = Annotated[
    float | None,
    typer.Option(
        "--worker-heartbeat-interval",
        min=1,
        help="Interval between worker lease heartbeats.",
        rich_help_panel=TIMINGS_PANEL,
    ),
]
WorkerHeartbeatTimeoutOpt = Annotated[
    float | None,
    typer.Option(
        "--worker-heartbeat-timeout",
        min=1,
        help="Maximum time the worker may go without a lease heartbeat.",
        rich_help_panel=TIMINGS_PANEL,
    ),
]
InteractiveSubmissionCommandOpt = Annotated[
    str | None,
    typer.Option(
        "--interactive-submission-command",
        metavar="COMMAND",
        help=(
            "Replace the scheduler-generated interactive submission command. "
            "The value is parsed into arguments using shell-style quoting, but "
            "is not executed through a shell."
        ),
        rich_help_panel=SCHEDULER_PANEL,
    ),
]


def _without_trailing_line_endings(value: str) -> str:
    """Remove line endings commonly added by secret files and pipes."""
    return value.rstrip("\r\n")


def _read_password_file(path: Path) -> str:
    try:
        return _without_trailing_line_endings(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as error:
        raise RichBadParameter(
            f"could not read password file {path}: {error}",
            param_hint="--password-file",
        ) from error


def _read_password_fd(fd: int) -> str:
    try:
        # Read through a duplicate so EzHPCy never closes a descriptor owned by
        # its caller. The duplicate intentionally shares the original offset.
        with os.fdopen(os.dup(fd), encoding="utf-8") as password_stream:
            return _without_trailing_line_endings(password_stream.read())
    except (OSError, UnicodeError) as error:
        raise RichBadParameter(
            f"could not read password file descriptor {fd}: {error}",
            param_hint="--password-fd",
        ) from error


def _read_password_keyring(*, user: str, host: str) -> str:
    account = f"{user}@{host}"
    try:
        password = keyring.get_password(KEYRING_SERVICE_NAME, account)
    except KeyringError as error:
        raise RichBadParameter(
            f"could not read password from the system keyring: {error}",
            param_hint="--password-keyring",
        ) from error

    if password is None:
        raise RichBadParameter(
            "no password was found in the system keyring for "
            f"service '{KEYRING_SERVICE_NAME}' and account '{account}'",
            param_hint="--password-keyring",
        )
    return password


def _parse_interactive_submission_command(value: str | None) -> list[str] | None:
    if value is None:
        return None
    try:
        command = shlex.split(value)
    except ValueError as error:
        raise RichBadParameter(
            f"invalid shell-style quoting: {error}",
            param_hint="--interactive-submission-command",
        ) from error
    if not command:
        raise RichBadParameter(
            "command must not be empty",
            param_hint="--interactive-submission-command",
        )
    return command


def resolve_password(
    *,
    password: str | None,
    password_file: Path | None,
    password_fd: int | None,
    password_keyring: bool,
    user: str,
    host: str,
    config_password_file: Path | None = None,
    config_password_fd: int | None = None,
    config_password_keyring: bool = False,
) -> str | None:
    """Resolve an explicit password source, then the profile's source."""
    explicit_sources = [
        name
        for name, selected in (
            ("--password", password is not None),
            ("--password-file", password_file is not None),
            ("--password-fd", password_fd is not None),
            ("--password-keyring", password_keyring),
        )
        if selected
    ]
    if len(explicit_sources) > 1:
        raise RichBadParameter(
            "password source options are mutually exclusive: "
            + ", ".join(explicit_sources)
        )

    if password is not None:
        return password
    if password_file is not None:
        return _read_password_file(password_file)
    if password_fd is not None:
        return _read_password_fd(password_fd)
    if password_keyring:
        return _read_password_keyring(user=user, host=host)

    configured_sources = [
        name
        for name, selected in (
            ("password_file", config_password_file is not None),
            ("password_fd", config_password_fd is not None),
            ("password_keyring", config_password_keyring),
        )
        if selected
    ]
    if len(configured_sources) > 1:
        raise RichBadParameter(
            "profile password source options are mutually exclusive: "
            + ", ".join(configured_sources)
        )
    if config_password_file is not None:
        return _read_password_file(config_password_file)
    if config_password_fd is not None:
        return _read_password_fd(config_password_fd)
    if config_password_keyring:
        return _read_password_keyring(user=user, host=host)
    return None


def resolve_profile_config(profile: str | None) -> ResolvedProfileConfig:
    """Resolve the selected profile, or the defaults when none is selected."""
    if profile is None:
        return ResolvedProfileConfig()
    try:
        return config.resolve_profile(profile)
    except ProfilePasswordSourceError as error:
        raise RichBadParameter(error.rich_message()) from error
    except ValueError as error:
        raise RichBadParameter(str(error), param_hint="PROFILE") from error


def _with_cli_values[SubConfigT: BaseModel](
    sub_config: SubConfigT, **cli_values: object
) -> SubConfigT:
    """Apply the CLI values that were given on top of a resolved sub-config."""
    values = {
        **sub_config.model_dump(exclude_unset=True),
        **{name: value for name, value in cli_values.items() if value is not None},
    }
    try:
        return type(sub_config).model_validate(values)
    except ValidationError as error:
        raise RichBadParameter(str(error)) from error


def connection_from_cli(
    *,
    profile: OptionalProfileArg = None,
    user: UserOpt = None,
    password: PasswordOpt = None,
    password_file: PasswordFileOpt = None,
    password_fd: PasswordFdOpt = None,
    password_keyring: PasswordKeyringOpt = False,
    host: HostOpt = None,
) -> ConnectionInfo:
    configured = resolve_profile_config(profile).connection
    config_prefix = f"profile.{profile}.connection" if profile is not None else None
    user = resolve_forbidden_none(
        cli_value=user,
        config_value=configured.user,
        name="user",
        cli_param="--user",
        config_param=f"{config_prefix}.user" if config_prefix else "a profile",
    )
    resolved_host = resolve_forbidden_none(
        cli_value=host,
        config_value=str(configured.host) if configured.host else None,
        name="host",
        cli_param="--host",
        config_param=f"{config_prefix}.host" if config_prefix else "a profile",
    )
    resolved_password = resolve_password(
        password=password,
        password_file=password_file,
        password_fd=password_fd,
        password_keyring=password_keyring,
        user=user,
        host=resolved_host,
        config_password_file=configured.password_file,
        config_password_fd=configured.password_fd,
        config_password_keyring=configured.password_keyring,
    )
    try:
        return ConnectionInfo.model_validate(
            {
                "user": user,
                "password": resolved_password,
                "host": resolved_host,
                "password_prompt": configured.password_prompt,
                "ssh_keepalive_interval_seconds": (
                    configured.ssh_keepalive_interval_seconds
                ),
            }
        )
    except ValidationError as error:
        raise RichBadParameter(str(error)) from error


with_connection = attach_hook(connection_from_cli, hook_output_kwarg="connection")


def scheduler_from_cli(
    *,
    profile: OptionalProfileArg = None,
    scheduler_type: SchedulerOpt = None,
    submission_mode: SubmissionModeOpt = None,
    interactive_submission_command: InteractiveSubmissionCommandOpt = None,
) -> SchedulerConfig:
    return _with_cli_values(
        resolve_profile_config(profile).scheduler,
        type=scheduler_type,
        submission_mode=submission_mode,
        interactive_submission_command=_parse_interactive_submission_command(
            interactive_submission_command
        ),
    )


with_scheduler = attach_hook(scheduler_from_cli, hook_output_kwarg="scheduler")


def resources_from_cli(
    *,
    profile: OptionalProfileArg = None,
    queue: QueueOpt = None,
    cores: CoresOpt = None,
    gpus: GpusOpt = None,
    exclusive: ExclusiveOpt = None,
    time_limit: TimeLimitOpt = None,
    memory: MemoryOpt = None,
) -> ResourcesConfig:
    return _with_cli_values(
        resolve_profile_config(profile).resources,
        queue=queue,
        cores=cores,
        gpus=gpus,
        exclusive=exclusive,
        time_limit=time_limit,
        memory=memory,
    )


with_resources = attach_hook(resources_from_cli, hook_output_kwarg="resources")


def timings_from_cli(
    *,
    profile: OptionalProfileArg = None,
    queue_timeout_seconds: QueueTimeoutOpt = None,
    startup_timeout_seconds: StartupTimeoutOpt = None,
    job_poll_interval_seconds: JobPollIntervalOpt = None,
    job_monitor_interval_seconds: JobMonitorIntervalOpt = None,
    worker_heartbeat_interval_seconds: WorkerHeartbeatIntervalOpt = None,
    worker_heartbeat_timeout_seconds: WorkerHeartbeatTimeoutOpt = None,
) -> TimingsConfig:
    return _with_cli_values(
        resolve_profile_config(profile).timings,
        queue_timeout_seconds=queue_timeout_seconds,
        worker_startup_timeout_seconds=startup_timeout_seconds,
        job_poll_interval_seconds=job_poll_interval_seconds,
        job_monitor_interval_seconds=job_monitor_interval_seconds,
        worker_heartbeat_interval_seconds=worker_heartbeat_interval_seconds,
        worker_heartbeat_timeout_seconds=worker_heartbeat_timeout_seconds,
    )


with_timings = attach_hook(timings_from_cli, hook_output_kwarg="timings")


def direct_connection_info_from_options(
    *,
    host: HostOpt = None,
    user: UserOpt = None,
    password: PasswordOpt = None,
    password_file: PasswordFileOpt = None,
    password_fd: PasswordFdOpt = None,
    password_keyring: PasswordKeyringOpt = False,
) -> ConnectionInfo:
    if user is None:
        raise RichBadParameter("user must be set via --user", param_hint="--user")
    if host is None:
        raise RichBadParameter("host must be set via --host", param_hint="--host")
    return ConnectionInfo(
        user=user,
        host=host,
        password=resolve_password(
            password=password,
            password_file=password_file,
            password_fd=password_fd,
            password_keyring=password_keyring,
            user=user,
            host=host,
        ),
    )


with_direct_connection_options = attach_hook(
    direct_connection_info_from_options, hook_output_kwarg="conn_info"
)
