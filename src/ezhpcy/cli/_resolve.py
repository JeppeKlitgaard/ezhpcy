import shlex

from cyclopts.exceptions import (
    STYLE_NAME,
    STYLE_OFFENDING_VALUE,
    STYLE_SUGGESTION,
    STYLE_VALID_CHOICE,
)
from pydantic import BaseModel, ValidationError
from rich.markup import escape

from ezhpcy.cli._errors import CliUsageError
from ezhpcy.cli._options import (
    ConnectionOptions,
    ResourceOptions,
    SchedulerOptions,
    TimingOptions,
)
from ezhpcy.cli._password import resolve_password
from ezhpcy.cli.utils.bad_parameter import RichBadParameter
from ezhpcy.config import ProfilePasswordSourceError, config
from ezhpcy.types import (
    ConnectionConfig,
    ConnectionInfo,
    ResolvedProfileConfig,
    ResourcesConfig,
    SchedulerConfig,
    TimingsConfig,
)


def resolve_forbidden_none[T](
    *,
    cli_value: T | None,
    config_value: T | None,
    name: str,
    cli_param: str,
    config_param: str,
    hint: str | None = None,
) -> T:
    value = cli_value if cli_value is not None else config_value
    if value is None:
        raise RichBadParameter(
            f"[bold purple]{name}[/bold purple] must be set via CLI ([bold green]{cli_param}[/bold green]) or config ([bold green]{config_param}[/bold green])"
            + (f". {hint}" if hint else "")
        )
    return value


_LIST_PROFILES_HINT = (
    "Run [bold green]ezhpcy list-profiles[/bold green] to see available profiles."
)


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


def resolve_profile_config(profile: str | None) -> ResolvedProfileConfig:
    """Resolve the selected profile, or the defaults when none is selected."""
    if profile is None:
        return ResolvedProfileConfig()
    if profile not in config.profile:
        # Worded and styled like Cyclopts' error for an invalid choice.
        message = (
            f'Invalid value "[{STYLE_OFFENDING_VALUE}]{escape(profile)}[/]" '
            f"for [{STYLE_NAME}]PROFILE[/]."
        )
        if config.profile:
            choices = ", ".join(
                f'[{STYLE_VALID_CHOICE}]"{escape(name)}"[/]' for name in config.profile
            )
            message += f" Choose from: {choices}."
        else:
            message += (
                " No profiles are configured; run "
                f"[{STYLE_SUGGESTION}]ezhpcy config load <preset>[/] to start from one."
            )
        raise CliUsageError(message)
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
    options: ConnectionOptions,
    configured: ConnectionConfig,
    *,
    profile: str | None,
) -> ConnectionInfo:
    """Resolve the login connection from the CLI options and `profile`'s config."""
    config_prefix = f"profile.{profile}.connection" if profile is not None else None
    # Without a profile, the likely fix is to give one.
    hint = _LIST_PROFILES_HINT if profile is None else None
    user = resolve_forbidden_none(
        cli_value=options.user,
        config_value=configured.user,
        name="user",
        cli_param="--user",
        config_param=f"{config_prefix}.user" if config_prefix else "a profile",
        hint=hint,
    )
    resolved_host = resolve_forbidden_none(
        cli_value=options.host,
        config_value=str(configured.host) if configured.host else None,
        name="host",
        cli_param="--host",
        config_param=f"{config_prefix}.host" if config_prefix else "a profile",
        hint=hint,
    )
    resolved_password = resolve_password(
        password=options.password,
        password_file=options.password_file,
        password_fd=options.password_fd,
        password_keyring=options.password_keyring,
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


def scheduler_from_cli(
    options: SchedulerOptions, configured: SchedulerConfig
) -> SchedulerConfig:
    return _with_cli_values(
        configured,
        type=options.scheduler_type,
        submission_mode=options.submission_mode,
        interactive_submission_command=_parse_interactive_submission_command(
            options.interactive_submission_command
        ),
    )


def resources_from_cli(
    options: ResourceOptions, configured: ResourcesConfig
) -> ResourcesConfig:
    return _with_cli_values(
        configured,
        queue=options.queue,
        cores=options.cores,
        gpus=options.gpus,
        exclusive=options.exclusive,
        time_limit=options.time_limit,
        memory=options.memory,
    )


def timings_from_cli(
    options: TimingOptions, configured: TimingsConfig
) -> TimingsConfig:
    return _with_cli_values(
        configured,
        queue_timeout_seconds=options.queue_timeout_seconds,
        worker_startup_timeout_seconds=options.startup_timeout_seconds,
        job_poll_interval_seconds=options.job_poll_interval_seconds,
        job_monitor_interval_seconds=options.job_monitor_interval_seconds,
        worker_heartbeat_interval_seconds=options.worker_heartbeat_interval_seconds,
        worker_heartbeat_timeout_seconds=options.worker_heartbeat_timeout_seconds,
    )


def direct_connection_from_cli(options: ConnectionOptions) -> ConnectionInfo:
    """Resolve a connection from the CLI options alone, without a profile."""
    if options.user is None:
        raise RichBadParameter("user must be set via --user", param_hint="--user")
    if options.host is None:
        raise RichBadParameter("host must be set via --host", param_hint="--host")
    return ConnectionInfo(
        user=options.user,
        host=options.host,
        password=resolve_password(
            password=options.password,
            password_file=options.password_file,
            password_fd=options.password_fd,
            password_keyring=options.password_keyring,
            user=options.user,
            host=options.host,
        ),
    )
