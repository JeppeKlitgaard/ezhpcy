"""
From a command's PROFILE argument and options to the configuration models it
runs with (`Login`, `JobConfig`), through its `Config`, rendering their
`ConfigError`s as usage errors that name the options and settings at fault.
"""

lazy import shlex
lazy from collections.abc import Mapping

lazy from pydantic import SecretStr

lazy from ezhpcy.api import Config
lazy from ezhpcy.cli._errors import CliUsageError, sentence
lazy from ezhpcy.cli._options import (
    ConnectionOptions,
    ResourceOptions,
    SchedulerOptions,
    TimingOptions,
    given_options,
    option_flags,
)
lazy from ezhpcy.logging import get_logger, label_log_file
lazy from ezhpcy.login import read_password
lazy from ezhpcy.types import (
    ConfigError,
    JobConfig,
    Login,
    MissingSetting,
    SubmissionCommandError,
    validated,
)

LIST_PROFILES_HINT = (
    t" Run {'ezhpcy list-profiles':suggestion} to see available profiles."
)


def check_profile_name(config: Config, profile_name: str) -> None:
    """Report a PROFILE argument that names no profile of `config`."""
    if profile_name in config.profile:
        return
    # Worded like Cyclopts' error for an invalid choice.
    if config.profile:
        message = t"Choose from: {list(config.profile):choice}."
    else:
        message = (
            t"No profiles are configured; run "
            t"{'ezhpcy config load <preset>':suggestion} to start from one."
        )
    raise CliUsageError(message, param_hint="PROFILE", value=profile_name)


def select_profile(config: Config, profile_name: str | None) -> None:
    """Check the PROFILE argument, if given, and label the log file with it."""
    if profile_name is not None:
        check_profile_name(config, profile_name)
        # Profile names are safe as file names (`validate_alias`).
        label_log_file(profile_name)


def login_from_cli(
    config: Config, options: ConnectionOptions, *, profile_name: str | None
) -> tuple[Login, SecretStr | None]:
    """
    The login node, from `profile_name`'s settings (or the top-level ones) and
    the CLI's options on top, and the password the CLI gave, if it gave one.
    A password source on the CLI replaces the configured ones.
    """
    changes: dict[str, object] = {
        name: value
        for name, value in (("user", options.user), ("host", options.host))
        if value is not None
    }
    if (
        options.password_file is not None
        or options.password_fd is not None
        or (options.password_keyring)
    ):
        changes |= {
            "password_file": options.password_file,
            "password_fd": options.password_fd,
            "password_keyring": options.password_keyring,
        }
    try:
        login = config.resolve_login(profile_name, **changes)
    except ConfigError as error:
        raise login_usage_error(error, options, profile_name=profile_name) from error
    except ValueError as error:
        raise CliUsageError(
            t"{sentence(str(error))}", param_hint="PROFILE", value=profile_name
        ) from error
    password = SecretStr(options.password) if options.password is not None else None
    return login, password


def direct_login_from_cli(
    options: ConnectionOptions,
) -> tuple[Login, SecretStr | None]:
    """
    A login node from the CLI options alone, without a profile, and its
    password from the CLI's sources, if any. Reads them: it blocks.
    """
    if options.user is None:
        raise CliUsageError(t"{'user':name} must be set via {'--user':name}.")
    if options.host is None:
        raise CliUsageError(t"{'host':name} must be set via {'--host':name}.")
    try:
        login = validated(
            Login,
            {
                "user": options.user,
                "host": options.host,
                "password_file": options.password_file,
                "password_fd": options.password_fd,
                "password_keyring": options.password_keyring,
            },
        )
        if options.password is not None:
            return login, SecretStr(options.password)
        return login, read_password(login)
    except ConfigError as error:
        raise login_usage_error(error, options, profile_name=None) from error


def login_usage_error(
    error: ConfigError, options: ConnectionOptions, *, profile_name: str | None
) -> CliUsageError:
    """
    `error`, from resolving a login or from connecting, as a usage error that
    names the option or the setting at fault.
    """
    given = {
        name: flag
        for name, (flag, value) in given_options(options).items()
        # `--password-keyring` is a flag, always given.
        if value is not False
    }
    prefix = (
        "connection" if profile_name is None else f"profile.{profile_name}.connection"
    )
    return _usage_error(
        error,
        option_flags(ConnectionOptions),
        given,
        prefix=prefix,
        profile_name=profile_name,
    )


def job_from_cli(
    config: Config,
    profile_name: str | None,
    *,
    scheduler_options: SchedulerOptions,
    resource_options: ResourceOptions,
    timing_options: TimingOptions,
    auto_provision: bool | None,
    app_resources: Mapping[str, object] | None = None,
) -> JobConfig:
    """
    The worker job, from `profile_name`'s settings (or the top-level ones),
    `app_resources` (what the apps it runs need) over them, and the CLI's
    options on top. An interactive submission command decides the resources
    instead, so then `app_resources` are left out, with a warning.
    """
    sections = {
        "scheduler": scheduler_options,
        "resources": resource_options,
        "timings": timing_options,
    }
    given = {section: given_options(options) for section, options in sections.items()}
    changes = {
        section: {name: value for name, (_, value) in options.items()}
        for section, options in given.items()
    }
    try:
        job = config.resolve_job(profile_name, **changes, auto_provision=auto_provision)
        if not app_resources:
            return job
        if job.scheduler.interactive_submission_command is not None:
            get_logger(__name__).warning(
                t"Ignoring the apps' resources: "
                t"{'scheduler.interactive_submission_command':name} decides them."
            )
            return job
        changes["resources"] = {**app_resources, **changes["resources"]}
        return config.resolve_job(
            profile_name, **changes, auto_provision=auto_provision
        )
    except ConfigError as error:
        given_flags = {
            f"{section}.{name}": flag
            for section, options in given.items()
            for name, (flag, _) in options.items()
        }
        if isinstance(error, SubmissionCommandError):
            raise _submission_command_error(
                config, error, given, given_flags, profile_name=profile_name
            ) from error
        flags = {
            f"{section}.{name}": flag
            for section, options in sections.items()
            for name, flag in option_flags(type(options)).items()
        }
        raise _usage_error(
            error,
            flags,
            given_flags,
            prefix=f"profile.{profile_name}" if profile_name is not None else None,
            profile_name=profile_name,
        ) from error


def _usage_error(
    error: ConfigError,
    flags: Mapping[str, str],
    given: Mapping[str, str],
    *,
    prefix: str | None,
    profile_name: str | None,
) -> CliUsageError:
    """
    `error` as a usage error. A missing setting names its option (`flags`, by
    setting) and its key in the profile; any other names the option that gave
    the setting (`given`), or else its key in the configuration file, under
    `prefix`.
    """
    if isinstance(error, MissingSetting):
        assert error.setting is not None
        hint = LIST_PROFILES_HINT if profile_name is None else t""
        source = t"{error.key:name}" if profile_name is not None else t"a profile"
        return CliUsageError(
            t"{error.setting:name} must be set via CLI "
            t"({flags[error.setting]:name}) or config ({source})." + hint
        )
    if error.setting is None:
        return CliUsageError(t"{sentence(error.message)}")
    setting = error.setting
    param_hint = given.get(setting) or (f"{prefix}.{setting}" if prefix else setting)
    return CliUsageError(
        t"{sentence(error.detail)}", param_hint=param_hint, value=error.value
    )


def _submission_command_error(
    config: Config,
    error: SubmissionCommandError,
    given: Mapping[str, Mapping[str, tuple[str, object]]],
    flags: Mapping[str, str],
    *,
    profile_name: str | None,
) -> CliUsageError:
    """
    An interactive submission command that the other settings contradict, named
    as the user gave it: on the CLI, or in the profile.
    """
    setting = "scheduler.interactive_submission_command"
    if (cli := given["scheduler"].get("interactive_submission_command")) is not None:
        source, command = cli
    else:
        source = f"profile.{profile_name}.{setting}"
        command = config.resolve_profile(
            profile_name
        ).scheduler.interactive_submission_command
    assert isinstance(command, list | tuple)
    value = shlex.join(command)
    if not error.conflicts:
        return CliUsageError(
            t"It requires {'scheduler.submission_mode':name} to be "
            t"{'interactive':choice}.",
            param_hint=source,
            value=value,
        )
    conflicts = [
        t"{f'profile.{profile_name}.{conflict}':name}"
        for conflict in error.conflicts
        if conflict not in flags
    ] + [
        t"{flags[conflict]:name}={given[section][name][1]}"
        for conflict in error.conflicts
        if conflict in flags
        for section, name in [conflict.split(".", 1)]
    ]
    return CliUsageError(
        t"It replaces the scheduler-generated request and cannot be combined "
        t"with submission options: {conflicts}.",
        param_hint=source,
        value=value,
    )
