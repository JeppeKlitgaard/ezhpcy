lazy import shlex
lazy from collections.abc import Mapping
lazy from string.templatelib import Template

lazy from pydantic import BaseModel, ValidationError

lazy from ezhpcy.cli._errors import CliUsageError, sentence
lazy from ezhpcy.cli._options import (
    ConnectionOptions,
    ResourceOptions,
    SchedulerOptions,
    TimingOptions,
)
lazy from ezhpcy.cli._password import resolve_password
lazy from ezhpcy.config import ProfilePasswordSourceError, config
lazy from ezhpcy.types import (
    ConnectionConfig,
    ConnectionInfo,
    ResolvedProfileConfig,
    ResourcesConfig,
    SchedulerConfig,
    TimingsConfig,
)

LIST_PROFILES_HINT = (
    t" Run {'ezhpcy list-profiles':suggestion} to see available profiles."
)


def missing_setting_error(
    name: str,
    *,
    cli_param: str,
    config_param: str | None,
    hint: Template = t"",
) -> CliUsageError:
    """Report a setting that neither the CLI nor the profile (if any) gave."""
    config_source = t"{config_param:name}" if config_param else t"a profile"
    return CliUsageError(
        t"{name:name} must be set via CLI ({cli_param:name}) "
        t"or config ({config_source})." + hint
    )


def resolve_forbidden_none[T](
    *,
    cli_value: T | None,
    config_value: T | None,
    name: str,
    cli_param: str,
    config_param: str | None,
    hint: Template = t"",
) -> T:
    value = cli_value if cli_value is not None else config_value
    if value is None:
        raise missing_setting_error(
            name, cli_param=cli_param, config_param=config_param, hint=hint
        )
    return value


def _validation_usage_error(
    error: ValidationError, names: Mapping[str, str]
) -> CliUsageError:
    """Report the first of Pydantic's errors, under the name the user knows."""
    details = error.errors(include_url=False)[0]
    # Pydantic prefixes a validator's own message with "Value error, ".
    message = (
        str(details["ctx"]["error"])
        if details["type"] == "value_error"
        else details["msg"]
    )
    message = t"{sentence(message)}"
    if not details["loc"]:
        return CliUsageError(message)
    field = ".".join(map(str, details["loc"]))
    return CliUsageError(
        message, param_hint=names.get(field, field), value=details["input"]
    )


def _parse_interactive_submission_command(value: str | None) -> list[str] | None:
    if value is None:
        return None
    try:
        command = shlex.split(value)
    except ValueError as error:
        raise CliUsageError(
            t"Could not split it with shell-style quoting: {sentence(str(error))}",
            param_hint="--interactive-submission-command",
            value=value,
        ) from error
    if not command:
        raise CliUsageError(
            t"The command must not be empty.",
            param_hint="--interactive-submission-command",
            value=value,
        )
    return command


def resolve_profile_config(profile: str | None) -> ResolvedProfileConfig:
    """Resolve the selected profile, or the defaults when none is selected."""
    if profile is None:
        return ResolvedProfileConfig()
    if profile not in config.profile:
        # Worded like Cyclopts' error for an invalid choice.
        if config.profile:
            message = t"Choose from: {list(config.profile):choice}."
        else:
            message = (
                t"No profiles are configured; run "
                t"{'ezhpcy config load <preset>':suggestion} to start from one."
            )
        raise CliUsageError(message, param_hint="PROFILE", value=profile)
    try:
        return config.resolve_profile(profile)
    except ProfilePasswordSourceError as error:
        raise CliUsageError(sentence(error.message)) from error
    except ValueError as error:
        raise CliUsageError(
            t"{sentence(str(error))}", param_hint="PROFILE", value=profile
        ) from error


def _with_cli_values[SubConfigT: BaseModel](
    sub_config: SubConfigT, **cli_values: tuple[str, object]
) -> SubConfigT:
    """Apply the CLI values that were given on top of a resolved sub-config.

    Each value comes with the option that gave it, for error messages.
    """
    values = {
        **sub_config.model_dump(exclude_unset=True),
        **{name: value for name, (_, value) in cli_values.items() if value is not None},
    }
    try:
        return type(sub_config).model_validate(values)
    except ValidationError as error:
        options = {name: option for name, (option, _) in cli_values.items()}
        raise _validation_usage_error(error, options) from error


def connection_from_cli(
    options: ConnectionOptions,
    configured: ConnectionConfig,
    *,
    profile: str | None,
) -> ConnectionInfo:
    """Resolve the login connection from the CLI options and `profile`'s config."""
    config_prefix = f"profile.{profile}.connection" if profile is not None else None
    # Without a profile, the likely fix is to give one.
    hint = LIST_PROFILES_HINT if profile is None else t""
    user = resolve_forbidden_none(
        cli_value=options.user,
        config_value=configured.user,
        name="user",
        cli_param="--user",
        config_param=f"{config_prefix}.user" if config_prefix else None,
        hint=hint,
    )
    resolved_host = resolve_forbidden_none(
        cli_value=options.host,
        config_value=str(configured.host) if configured.host else None,
        name="host",
        cli_param="--host",
        config_param=f"{config_prefix}.host" if config_prefix else None,
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
        config_prefix=config_prefix or "connection",
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
        # The profile's host was validated when the configuration was loaded.
        raise _validation_usage_error(
            error, {"user": "--user", "host": "--host"}
        ) from error


def scheduler_from_cli(
    options: SchedulerOptions, configured: SchedulerConfig
) -> SchedulerConfig:
    return _with_cli_values(
        configured,
        type=("--scheduler", options.scheduler_type),
        submission_mode=("--submission-mode", options.submission_mode),
        interactive_submission_command=(
            "--interactive-submission-command",
            _parse_interactive_submission_command(
                options.interactive_submission_command
            ),
        ),
    )


def resources_from_cli(
    options: ResourceOptions, configured: ResourcesConfig
) -> ResourcesConfig:
    return _with_cli_values(
        configured,
        queue=("--queue", options.queue),
        cores=("--cores", options.cores),
        gpus=("--gpus", options.gpus),
        exclusive=("--exclusive/--shared", options.exclusive),
        time_limit=("--time-limit", options.time_limit),
        memory=("--memory", options.memory),
    )


def timings_from_cli(
    options: TimingOptions, configured: TimingsConfig
) -> TimingsConfig:
    return _with_cli_values(
        configured,
        queue_timeout_seconds=("--queue-timeout", options.queue_timeout_seconds),
        worker_startup_timeout_seconds=(
            "--startup-timeout",
            options.startup_timeout_seconds,
        ),
        job_poll_interval_seconds=(
            "--job-poll-interval",
            options.job_poll_interval_seconds,
        ),
        job_monitor_interval_seconds=(
            "--job-monitor-interval",
            options.job_monitor_interval_seconds,
        ),
        worker_heartbeat_interval_seconds=(
            "--worker-heartbeat-interval",
            options.worker_heartbeat_interval_seconds,
        ),
        worker_heartbeat_timeout_seconds=(
            "--worker-heartbeat-timeout",
            options.worker_heartbeat_timeout_seconds,
        ),
    )


def direct_connection_from_cli(options: ConnectionOptions) -> ConnectionInfo:
    """Resolve a connection from the CLI options alone, without a profile."""
    if options.user is None:
        raise CliUsageError(t"{'user':name} must be set via {'--user':name}.")
    if options.host is None:
        raise CliUsageError(t"{'host':name} must be set via {'--host':name}.")
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
